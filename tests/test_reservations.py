"""Integration tests against an isolated PostgreSQL service (never production)."""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import psycopg

from bot import Choice, Config, Journal, ReservationRejected


URL = os.environ.get("BOT_TEST_DB_URL", "")
TEST_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS bot_private;
CREATE TABLE IF NOT EXISTS bot_private.bot_trades (
    mode TEXT NOT NULL, ticker TEXT NOT NULL, day TEXT NOT NULL,
    created TEXT NOT NULL, side TEXT NOT NULL, count INTEGER NOT NULL,
    price TEXT NOT NULL, fee TEXT NOT NULL, reserved TEXT NOT NULL,
    model_probability REAL NOT NULL, edge REAL NOT NULL,
    status TEXT NOT NULL, order_id TEXT, result TEXT, estimated_pnl TEXT,
    PRIMARY KEY (mode, ticker)
);
"""


@unittest.skipUnless(URL, "BOT_TEST_DB_URL is required for PostgreSQL tests")
class ReservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(URL) as db:
            db.execute(TEST_SCHEMA)

    def setUp(self):
        # Fresh identifiers make each test independent of all other tests.
        self.mode = "test_" + uuid.uuid4().hex
        self.day = "2099-01-01"
        self.cfg = replace(Config.from_env(), mode=self.mode,
                           max_order_dollars=Decimal("5"),
                           max_daily_spend=Decimal("5"), max_trades=8)
        self.choice = Choice("YES", Decimal("0.60"), 5, Decimal("0.02"),
                             Decimal("3.02"), 0.7, 0.15, Decimal("0.02"))

    def tearDown(self):
        with psycopg.connect(URL) as db:
            db.execute("DELETE FROM bot_private.bot_trades WHERE mode=%s",
                       (self.mode,))

    def reserve(self, ticker, cfg=None, choice=None, barrier=None):
        with psycopg.connect(URL) as db:
            journal = Journal.__new__(Journal)
            journal.db = db
            if barrier:
                barrier.wait(timeout=15)
            journal.reserve(cfg or self.cfg, {"ticker": ticker},
                            choice or self.choice, self.day)

    def totals(self):
        with psycopg.connect(URL) as db:
            return db.execute(
                """SELECT COUNT(*), COALESCE(SUM(reserved::numeric), 0)
                   FROM bot_private.bot_trades WHERE mode=%s AND day=%s""",
                (self.mode, self.day)
            ).fetchone()

    def test_concurrent_reservations_respect_daily_spending(self):
        barrier = Barrier(2)
        def attempt(ticker):
            try:
                self.reserve(ticker, barrier=barrier)
                return "accepted"
            except ReservationRejected:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, "A"), pool.submit(attempt, "B")]
            results = [future.result(timeout=20) for future in futures]
        self.assertCountEqual(results, ["accepted", "rejected"])
        count, spent = self.totals()
        self.assertEqual(count, 1)
        self.assertEqual(spent, Decimal("3.0200"))

    def test_concurrent_reservations_respect_trade_count(self):
        cfg = replace(self.cfg, max_daily_spend=Decimal("20"),
                      max_trades=1)
        barrier = Barrier(2)
        def attempt(ticker):
            try:
                self.reserve(ticker, cfg=cfg, barrier=barrier)
                return "accepted"
            except ReservationRejected:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, "C"), pool.submit(attempt, "D")]
            results = [future.result(timeout=20) for future in futures]
        self.assertCountEqual(results, ["accepted", "rejected"])
        self.assertEqual(self.totals()[0], 1)

    def test_rejects_order_above_per_order_cap(self):
        expensive = replace(self.choice, cost=Decimal("5.01"))
        with self.assertRaises(ReservationRejected):
            self.reserve("OVER_CAP", choice=expensive)
        self.assertEqual(self.totals()[0], 0)

    def test_duplicate_ticker_is_rejected_by_primary_key(self):
        # Leave ample daily budget so the PRIMARY KEY, not the spend cap,
        # is the expected reason the second reservation fails.
        cfg = replace(self.cfg, max_daily_spend=Decimal("20"))
        self.reserve("DUP", cfg=cfg)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.reserve("DUP", cfg=cfg)
        self.assertEqual(self.totals()[0], 1)


if __name__ == "__main__":
    unittest.main()
