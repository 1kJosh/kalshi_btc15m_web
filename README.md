# Kalshi BTC 15-Minute Browser Dashboard

This is a browser UI for the existing KXBTC15M rule-based bot. **Paper mode only**; it never sends real orders. Simulated fills are hypothetical. The model has not been validated as profitable. Coinbase spot differs from the CF Benchmarks BRTI settlement benchmark.

## Deploy without installing Python on your Mac

1. Create a **private** GitHub repository and upload `app.py`, `bot.py`, `requirements.txt`, and `.streamlit/config.toml` (if present). Do not upload passwords, API keys, or `.env` files.
2. At https://share.streamlit.io, choose **Create app**, connect GitHub, select the repo and set main file to `app.py`.
3. In app **Advanced settings / Secrets**, enter `DASHBOARD_PASSWORD = "a-long-unique-password"` (use your own strong password). Deploy.
4. Open the resulting Streamlit URL on your Mac or phone, enter the password, and click **Start**.

**Important limitations:** The app scans only while the browser page is open and connected. Streamlit Community Cloud can sleep, restart, or lose local trade-history files; it is not a dependable always-on trading host. This is for testing only. Never share the password or private keys. Live trading is deliberately disabled in this web version. Some hosts may restrict external API access.

## Local preview (optional)
`pip install -r requirements.txt` then set `DASHBOARD_PASSWORD` in your environment and run `streamlit run app.py`.

## Risk settings
The UI lets you reduce the per-trade and daily paper limits. The bot's additional conservative checks (spread, minimum model edge, entry window, contract limit) remain active. A trade may never occur. The P/L figures are estimated from recorded settlements, not an exchange ledger.
