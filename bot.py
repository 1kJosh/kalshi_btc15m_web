#!/usr/bin/env python3
"""Kalshi KXBTC15M research bot; PAPER by default, live opt-in required.

This is a rule-based proof of concept, NOT a validated profitable strategy.
Market settlement is CF Benchmarks' 60-second BRTI average, while the default
price reference is Coinbase spot. That basis mismatch can reverse a result.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import logging
import math
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / '.env')
LOG = logging.getLogger('kalshi-btc15m')
SERIES = 'KXBTC15M'
KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
UTC = dt.timezone.utc
ET = ZoneInfo('America/New_York')
CENT = Decimal('0.01')


class UncertainLiveOrderError(RuntimeError):
    """Submission failed or execution ambiguous: stop immediately, never auto-retry."""



def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))


def money(x: Decimal) -> str:
    return f'{x.quantize(Decimal("0.0001")):.4f}'


def fee_estimate(count: int, price: Decimal, multiplier: Decimal) -> Decimal:
    """Approximate taker fee; actual exchange rounding/waivers can differ."""
    raw = Decimal('0.07') * multiplier * count * price * (Decimal('1') - price)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class Config:
    mode: str
    interval: int
    max_order_dollars: Decimal
    max_daily_spend: Decimal
    max_contracts: int
    max_trades: int
    min_edge: float
    annual_vol: float
    min_gap: float
    max_spread: Decimal
    earliest_entry_elapsed: int
    latest_entry_remaining: int
    confirm_proxy_live: bool

    @classmethod
    def from_env(cls):
        mode = os.getenv('MODE', 'paper').strip().lower()
        if mode not in {'paper', 'live'}:
            raise ValueError('MODE must be paper or live')
        if mode == 'live':
            if os.getenv('ENABLE_LIVE') != 'true' or os.getenv('LIVE_CONFIRM') != 'I_ACCEPT_REAL_MONEY_RISK':
                raise ValueError('Live trading locked. See README: ENABLE_LIVE=true and LIVE_CONFIRM required.')
        cfg = cls(mode, int(os.getenv('POLL_SECONDS', '20')),
                  Decimal(os.getenv('MAX_ORDER_DOLLARS', '5')),
                  Decimal(os.getenv('MAX_DAILY_SPEND', '20')),
                  int(os.getenv('MAX_CONTRACTS', '5')),
                  int(os.getenv('MAX_TRADES_PER_DAY', '8')),
                  env_float('MIN_EDGE', 0.12), env_float('ANNUAL_VOLATILITY', 0.65),
                  env_float('MIN_SPOT_GAP_DOLLARS', 100),
                  Decimal(os.getenv('MAX_SPREAD_DOLLARS', '0.05')),
                  int(os.getenv('MIN_ELAPSED_SECONDS', '240')),
                  int(os.getenv('MIN_REMAINING_SECONDS', '180')),
                  os.getenv('CONFIRM_COINBASE_BRTI_BASIS_RISK') == 'yes')
        if not 10 <= cfg.interval <= 300:
            raise ValueError('POLL_SECONDS must be 10-300')
        if cfg.max_order_dollars <= 0 or cfg.max_daily_spend <= 0 or cfg.max_contracts <= 0 or cfg.max_trades <= 0:
            raise ValueError('Caps must be positive')
        if not 0.01 <= cfg.min_edge <= 0.5 or not 0.10 <= cfg.annual_vol <= 2.0:
            raise ValueError('Check MIN_EDGE and ANNUAL_VOLATILITY')
        if cfg.mode == 'live' and not cfg.confirm_proxy_live:
            raise ValueError('Live mode uses Coinbase proxy for BRTI. Set CONFIRM_COINBASE_BRTI_BASIS_RISK=yes to explicitly accept this mismatch.')
        return cfg


class API:
    def __init__(self, live: bool):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': 'KXBTC15M-ResearchBot/1.0'})
        self.live = live
        self.key_id = os.getenv('KALSHI_KEY_ID', '').strip()
        self.private_key = None
        if live:
            filepath = os.getenv('KALSHI_PRIVATE_KEY_PATH', '').strip()
            if not self.key_id or not filepath:
                raise ValueError('Missing KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PATH')
            self.private_key = serialization.load_pem_private_key(
                Path(filepath).expanduser().read_bytes(), password=None)

    def headers(self, verb: str, path: str) -> dict:
        if not self.live:
            raise RuntimeError('Authentication unavailable in paper mode')
        millis = str(int(time.time() * 1000))
        message = f'{millis}{verb}{"/trade-api/v2" + path}'.encode()
        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                     salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        return {'KALSHI-ACCESS-KEY': self.key_id,
                'KALSHI-ACCESS-TIMESTAMP': millis,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}

    def request(self, verb: str, path: str, *, params=None, payload=None, auth=False) -> dict:
        head = self.headers(verb, path) if auth else {}
        # Never retry POSTs automatically: an ambiguous network failure could have filled an order.
        r = self.s.request(verb, KALSHI + path, params=params, json=payload,
                           headers=head, timeout=(4, 9))
        r.raise_for_status()
        return r.json()

    def current_market(self, now: dt.datetime) -> dict | None:
        data = self.request('GET', '/markets', params={'series_ticker': SERIES, 'status': 'open', 'limit': 100})
        candidates = []
        for m in data.get('markets', []):
            try:
                if not m.get('ticker', '').startswith(SERIES + '-'):
                    continue
                opened, closes = iso_time(m['open_time']), iso_time(m['close_time'])
                if opened <= now < closes:
                    candidates.append((closes, m))
            except (KeyError, TypeError, ValueError):
                continue
        return min(candidates, key=lambda x: x[0])[1] if candidates else None

    def series_fee_multiplier(self) -> Decimal:
        series = self.request('GET', f'/series/{SERIES}').get('series', {})
        if series.get('fee_type') != 'quadratic':
            raise ValueError('Unrecognized series fee model; skipping trading')
        multiplier = series.get('fee_multiplier')
        if multiplier is None:
            raise ValueError('Missing series fee multiplier; skipping trading')
        return Decimal(str(multiplier))

    def orderbook(self, ticker: str) -> dict:
        return self.request('GET', f'/markets/{ticker}/orderbook').get('orderbook_fp', {})

    def market(self, ticker: str) -> dict:
        return self.request('GET', f'/markets/{ticker}').get('market', {})

    def spot(self) -> Decimal:
        r = self.s.get('https://api.coinbase.com/v2/prices/BTC-USD/spot', timeout=(4, 9))
        r.raise_for_status()
        data = r.json()['data']
        if data.get('base') != 'BTC' or data.get('currency') != 'USD':
            raise ValueError('Unexpected Coinbase spot response')
        price = Decimal(data['amount'])
        if price <= 1000:
            raise ValueError('Invalid BTC spot quote')
        return price

    def account_safe(self, ticker: str, needed: Decimal) -> None:
        bal = self.request('GET', '/portfolio/balance', auth=True)
        available = Decimal(str(bal['balance'])) / 100
        if available < needed + Decimal('2.00'):
            raise ValueError('Insufficient available cash (including $2 safety buffer)')
        pos = self.request('GET', '/portfolio/positions', params={'ticker': ticker}, auth=True)
        if any(p.get('ticker') == ticker and Decimal(p.get('position_fp', '0')) != 0
               for p in pos.get('market_positions', [])):
            raise ValueError('Existing Kalshi position in this window; not stacking')
        resting = self.request('GET', '/portfolio/orders',
                               params={'ticker': ticker, 'status': 'resting'}, auth=True)
        if any(o.get('ticker') == ticker for o in resting.get('orders', [])):
            raise ValueError('Existing resting order in this window; not stacking')

    def place_live(self, ticker: str, choice: 'Choice') -> dict:
        # V2 order prices ALWAYS use the YES leg. ask = buy NO / sell YES.
        yes_leg_price = choice.ask if choice.side == 'YES' else Decimal('1') - choice.ask
        payload = {'ticker': ticker, 'client_order_id': str(uuid.uuid4()),
                   'side': 'bid' if choice.side == 'YES' else 'ask',
                   'count': f'{choice.count}.00', 'price': money(yes_leg_price),
                   'time_in_force': 'fill_or_kill', 'post_only': False,
                   'cancel_order_on_pause': True,
                   'self_trade_prevention_type': 'taker_at_cross'}
        return self.request('POST', '/portfolio/events/orders', payload=payload, auth=True)


@dataclass(frozen=True)
class Choice:
    side: str
    ask: Decimal  # dollars for this outcome, not YES-leg when buying NO
    count: int
    fee: Decimal
    cost: Decimal
    probability: float
    edge: float
    spread: Decimal


def _top(book: dict, key: str) -> tuple[Decimal, int] | None:
    levels = book.get(key, [])
    eligible = [(Decimal(str(p)), int(Decimal(str(q)))) for p, q in levels
                if 0 < Decimal(str(p)) < 1 and Decimal(str(q)) >= 1]
    return max(eligible, key=lambda x: x[0]) if eligible else None


def model_probability(spot: Decimal, strike: Decimal, remaining: float, annual_vol: float) -> float:
    """Illustrative lognormal model; uncalibrated, no demonstrated predictive edge."""
    vol = annual_vol * math.sqrt(max(1.0, remaining) / 31_536_000)
    z = math.log(float(spot / strike)) / vol
    return max(0.001, min(0.999, 0.5 * math.erfc(-z / math.sqrt(2))))


def evaluate(book: dict, spot: Decimal, strike: Decimal, remaining: float,
             multiplier: Decimal, cfg: Config) -> Choice | None:
    yes_bid, no_bid = _top(book, 'yes_dollars'), _top(book, 'no_dollars')
    if not yes_bid or not no_bid:
        return None
    yes_ask, yes_liq = Decimal('1') - no_bid[0], no_bid[1]
    no_ask, no_liq = Decimal('1') - yes_bid[0], yes_bid[1]
    spread = yes_ask - yes_bid[0]
    if spread < 0 or spread > cfg.max_spread:
        return None
    if abs(spot - strike) < Decimal(str(cfg.min_gap)):
        return None
    prob_yes = model_probability(spot, strike, remaining, cfg.annual_vol)
    possibilities: list[Choice] = []
    for side, ask, liquidity, prob in [('YES', yes_ask, yes_liq, prob_yes),
                                        ('NO', no_ask, no_liq, 1 - prob_yes)]:
        if not Decimal('0.20') <= ask <= Decimal('0.85') or prob < 0.60:
            continue
        available = min(cfg.max_contracts, liquidity)
        for count in range(available, 0, -1):
            fee = fee_estimate(count, ask, multiplier)
            # Conservative extra 2 cents; does not guarantee coverage of all actual fees.
            cost = ask * count + fee + Decimal('0.02')
            if cost > cfg.max_order_dollars or cost > cfg.max_daily_spend:
                continue
            edge = prob - float(ask + (fee + Decimal('0.02')) / count)
            if edge >= cfg.min_edge:
                possibilities.append(Choice(side, ask, count, fee, cost, prob, edge, spread))
                break
    return max(possibilities, key=lambda c: c.edge * c.count) if possibilities else None


class Journal:
    def __init__(self):
        HERE.joinpath('data').mkdir(exist_ok=True)
        self.db = sqlite3.connect(HERE / 'data' / 'trades.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS trades (
            mode TEXT NOT NULL, ticker TEXT NOT NULL, day TEXT NOT NULL,
            created TEXT NOT NULL, side TEXT NOT NULL, count INTEGER NOT NULL,
            price TEXT NOT NULL, fee TEXT NOT NULL, reserved TEXT NOT NULL,
            model_probability REAL NOT NULL, edge REAL NOT NULL,
            status TEXT NOT NULL, order_id TEXT, result TEXT, estimated_pnl TEXT,
            PRIMARY KEY (mode,ticker))''')
        self.db.commit()

    def has_attempt(self, mode: str, ticker: str) -> bool:
        return self.db.execute('SELECT 1 FROM trades WHERE mode=? AND ticker=?',
                               (mode, ticker)).fetchone() is not None

    def limits(self, mode: str, day: str) -> tuple[int, Decimal]:
        rows = self.db.execute('SELECT reserved FROM trades WHERE mode=? AND day=?',
                               (mode, day)).fetchall()
        return len(rows), sum((Decimal(x[0]) for x in rows), Decimal('0'))

    def pending(self, mode: str) -> list[tuple]:
        return self.db.execute('''SELECT ticker,side,count,price,fee FROM trades
                                  WHERE mode=? AND status='filled' ''', (mode,)).fetchall()

    def reserve(self, cfg: Config, market: dict, c: Choice, day: str):
        # Durable before network request: on timeout/restart, never repeat the trade.
        self.db.execute('''INSERT INTO trades
          (mode,ticker,day,created,side,count,price,fee,reserved,model_probability,edge,status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
          (cfg.mode, market['ticker'], day, now_utc().isoformat(), c.side, c.count,
           money(c.ask), money(c.fee), money(c.cost), c.probability, c.edge, 'pending'))
        self.db.commit()

    def update(self, mode: str, ticker: str, status: str, order_id: str = None,
               result: str = None, pnl: Decimal = None):
        self.db.execute('''UPDATE trades SET status=?, order_id=COALESCE(?,order_id),
                           result=COALESCE(?,result),estimated_pnl=COALESCE(?,estimated_pnl)
                           WHERE mode=? AND ticker=?''',
                        (status, order_id, result, money(pnl) if pnl is not None else None,
                         mode, ticker))
        self.db.commit()


def reconcile(api: API, journal: Journal, cfg: Config):
    # These are projected P/Ls for paper, not the authoritative Kalshi cash ledger.
    for ticker, side, count, price, fee in journal.pending(cfg.mode):
        try:
            m = api.market(ticker)
            result = str(m.get('result') or '').lower()
            if result not in ('yes', 'no'):
                continue
            won = (side.lower() == result)
            estimated_pnl = (Decimal(count) if won else Decimal(0)) - Decimal(count) * Decimal(price) - Decimal(fee)
            journal.update(cfg.mode, ticker, 'settled', result=result, pnl=estimated_pnl)
            LOG.info('%s settled %s: %s | modeled gross outcome P/L $%.2f',
                     ticker, result.upper(), 'WIN' if won else 'LOSS', estimated_pnl)
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOG.warning('Settlement status unavailable for %s: %s', ticker, exc)


def tick(api: API, journal: Journal, cfg: Config):
    now = now_utc()
    today = now.astimezone(ET).date().isoformat()
    trades, spent = journal.limits(cfg.mode, today)
    if trades >= cfg.max_trades or spent >= cfg.max_daily_spend:
        LOG.info('Daily caps reached: %s attempts, $%s reserved', trades, spent)
        return
    market = api.current_market(now)
    if not market:
        LOG.info('No open %s market found', SERIES)
        return
    ticker = market['ticker']
    if journal.has_attempt(cfg.mode, ticker):
        return
    remain = (iso_time(market['close_time']) - now).total_seconds()
    elapsed = (now - iso_time(market['open_time'])).total_seconds()
    if elapsed < cfg.earliest_entry_elapsed or remain < cfg.latest_entry_remaining:
        return
    if market.get('floor_strike') is None:
        LOG.info('%s: strike not published yet', ticker)
        return
    strike = Decimal(str(market['floor_strike']))
    if strike < 1000:
        return
    spot = api.spot()
    # Fee multiplier is read from current Kalshi series data; no hardcoded override.
    multiplier = api.series_fee_multiplier()
    book = api.orderbook(ticker)
    yes_bid, no_bid = _top(book, 'yes_dollars'), _top(book, 'no_dollars')
    p_yes = model_probability(spot, strike, remain, cfg.annual_vol)
    yes_ask = (Decimal('1') - no_bid[0]) if no_bid else None
    no_ask = (Decimal('1') - yes_bid[0]) if yes_bid else None
    spread = (yes_ask - yes_bid[0]) if yes_ask is not None and yes_bid else None
    LOG.info('MARKET_SNAPSHOT ticker=%s spot=%s strike=%s seconds=%d yes_probability=%.4f no_probability=%.4f yes_ask=%s no_ask=%s spread=%s',
             ticker, spot, strike, remain, p_yes, 1-p_yes,
             yes_ask if yes_ask is not None else 'NA',
             no_ask if no_ask is not None else 'NA',
             spread if spread is not None else 'NA')
    candidate = evaluate(book, spot, strike, remain, multiplier, cfg)
    if not candidate:
        LOG.info('%s spot=%s strike=%s seconds=%d: no eligible edge', ticker, spot, strike, remain)
        return
    trades, spent = journal.limits(cfg.mode, today)
    if spent + candidate.cost > cfg.max_daily_spend:
        LOG.info('Skip %s: daily spend cap', ticker)
        return
    LOG.info('SIGNAL %s %s x%d @ $%s | spot %s / strike %s | model %.1f%% | edge %.1f%% | max charge $%s',
             ticker, candidate.side, candidate.count, candidate.ask, spot, strike,
             candidate.probability * 100, candidate.edge * 100, candidate.cost)
    if cfg.mode == 'live':
        api.account_safe(ticker, candidate.cost)
    journal.reserve(cfg, market, candidate, today)
    if cfg.mode == 'paper':
        journal.update(cfg.mode, ticker, 'filled', order_id='PAPER-SIMULATED')
        LOG.info('PAPER SIMULATION recorded; assumes top-of-book fill without slippage')
        return
    try:
        response = api.place_live(ticker, candidate)
        fill_count = Decimal(str(response.get('fill_count', '0')))
        if fill_count == Decimal(candidate.count):
            journal.update(cfg.mode, ticker, 'filled', order_id=response.get('order_id'))
            LOG.info('LIVE ORDER FILLED: order_id=%s', response.get('order_id'))
        elif fill_count == 0:
            journal.update(cfg.mode, ticker, 'no_fill', order_id=response.get('order_id'))
            LOG.info('LIVE FOK order not filled; no reattempt this window')
        else:
            LOG.critical('Unexpected partial FOK fill: %s. STOP AND REVIEW ACCOUNT.', response)
            raise RuntimeError('Unexpected partial FOK; trading stopped')
    except Exception as exc:
        # Keep status pending because the remote order may have filled despite a timeout.
        LOG.exception('Ambiguous/failed live order. STOP; inspect Kalshi account before restarting.')
        raise UncertainLiveOrderError('Live order status uncertain; bot halted. Inspect Kalshi account.') from exc


def print_status(journal: Journal, mode: str):
    rows = journal.db.execute('''SELECT day,ticker,side,count,price,status,result,estimated_pnl
        FROM trades WHERE mode=? ORDER BY created DESC LIMIT 30''', (mode,)).fetchall()
    for row in rows:
        print(' | '.join(str(v if v is not None else '-') for v in row))
    day = now_utc().astimezone(ET).date().isoformat()
    n, spent = journal.limits(mode, day)
    print(f'\nToday (ET): {n} attempts, ${spent:.2f} reserved (includes buffers/failed attempts)')


def main():
    parser = argparse.ArgumentParser(description='KXBTC15M paper/live bot (live gated)')
    parser.add_argument('--once', action='store_true', help='one scan, then exit')
    parser.add_argument('--status', action='store_true', help='last 30 bot trades, no API calls')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    mode = os.getenv('MODE', 'paper').strip().lower()
    if args.status:
        print_status(Journal(), mode)
        return
    cfg = Config.from_env()
    # POSIX file lock: prevent two Mac/Unix processes sharing this directory from trading.
    lock = open(HERE / 'data' / 'run.lock', 'a+') if (HERE / 'data').exists() else None
    if lock is None:
        (HERE / 'data').mkdir(exist_ok=True)
        lock = open(HERE / 'data' / 'run.lock', 'a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another instance is already running in this folder.')
    journal = Journal()
    api = API(cfg.mode == 'live')
    LOG.warning('MODE=%s | experimental model, NOT validated, NO GUARANTEED RETURNS', cfg.mode.upper())
    LOG.warning('Price input Coinbase BTC/USD, NOT CF Benchmarks BRTI settlement input.')
    LOG.info('Caps: per-order $%s; daily total $%s; max %d entries/day. Exit: hold to settlement.',
             cfg.max_order_dollars, cfg.max_daily_spend, cfg.max_trades)
    iteration = 0
    while True:
        if (HERE / 'STOP').exists():
            LOG.warning('STOP file found; halted before any new orders.')
            break
        try:
            if iteration % 3 == 0:
                reconcile(api, journal, cfg)
            tick(api, journal, cfg)
        except UncertainLiveOrderError:
            LOG.critical('CRITICAL: Live order may exist. Bot halted; manually inspect orders and account.')
            raise SystemExit(2)
        except requests.RequestException as exc:
            LOG.warning('Market data/network error; skipping scan: %s', exc)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            LOG.warning('Bad/missing market input; skipping scan: %s', exc)
        iteration += 1
        if args.once:
            break
        try:
            time.sleep(cfg.interval)
        except KeyboardInterrupt:
            LOG.info('Stopped by operator')
            break


if __name__ == '__main__':
    main()
