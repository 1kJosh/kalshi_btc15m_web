# TradingView-style indicator research (proposal only)

This module is **not connected to TradingView**, the live bot, or the scheduled workflow.
It computes RSI(14), EMA(9/21), MACD(12/26/9), and session VWAP from
**completed OHLCV candles** supplied by a future read-only data adapter.

## Integration plan
1. Choose a single BTC/USD exchange and timeframe, document candle timestamps,
   missing-candle behavior, and UTC session boundaries. TradingView charts can
   differ by exchange and timeframe; Coinbase spot is not the Kalshi BRTI benchmark.
2. Build a read-only candle adapter and log timestamped snapshots to a separate
   research table. Never change the existing trade eligibility or paper reservations.
3. Add unit tests with fixed candle fixtures, including flat prices, missing data,
   zero volume, and short history. Cross-check values against TradingView with
   identical symbol, exchange, timeframe, and session settings.
4. Compare shadow signals against settled paper predictions out of sample, with
   costs and skipped-trade counts. Require review before any strategy changes.

**Safety:** No TradingView credentials, webhook, order API, live mode, or trading
configuration is introduced. This PR is not a deployment and does not alter
paper-bot scheduling or execution.
