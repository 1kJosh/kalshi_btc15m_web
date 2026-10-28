"""Read-only Streamlit monitor for the Kalshi BTC 15-minute PAPER bot.

GitHub Actions is the only scheduled scanner. This dashboard never runs bot.py.
"""
import csv
import hmac
import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import streamlit as st

st.set_page_config(page_title="BTC 15m Bot | Monitor", page_icon="₿", layout="wide")
st.title("₿ BTC 15-Minute Bot")
st.caption("Kalshi KXBTC15M • PAPER SIMULATION ONLY • Supabase performance monitor")
st.warning("No real Kalshi orders are sent. Predictions and hypothetical P/L do not demonstrate a profitable strategy. Coinbase BTC spot is a proxy, not the CF Benchmarks BRTI settlement reference.")


def setting(name):
    # Streamlit Cloud secrets are usually exposed as environment variables, but
    # reading st.secrets also supports local .streamlit/secrets.toml.
    value = os.environ.get(name, "")
    if value:
        return value
    try:
        return str(st.secrets.get(name, ""))
    except (FileNotFoundError, OSError, KeyError):
        return ""


password = setting("DASHBOARD_PASSWORD")
if not password:
    st.error("Missing DASHBOARD_PASSWORD in Streamlit Secrets.")
    st.stop()
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if not st.session_state.authenticated:
    supplied = st.text_input("Dashboard password", type="password")
    if st.button("Unlock") and hmac.compare_digest(supplied, password):
        st.session_state.authenticated = True
        st.rerun()
    st.stop()

with st.sidebar:
    st.header("Monitoring")
    st.success("Read-only dashboard")
    st.write("**Scanning:** GitHub Actions")
    st.write("**Storage:** Supabase PostgreSQL")
    st.write("**Trading:** Paper only")
    st.caption("This page never launches bot.py or submits orders. Scheduled GitHub Actions scans may be delayed or skipped.")
    if st.button("Refresh dashboard", use_container_width=True):
        st.rerun()
    if st.button("Lock dashboard", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()

url = setting("SUPABASE_DB_URL")
if not url:
    st.error("Missing SUPABASE_DB_URL in Streamlit Secrets. Add it there; never commit credentials to GitHub.")
    st.stop()


def read_table(conn, table, limit):
    # Table names are hardcoded below, not supplied by users.
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM bot_private.{table} ORDER BY created DESC LIMIT %s", (limit,))
        return [dict(row) for row in cur.fetchall()]


def csv_data(rows):
    output = io.StringIO()
    if rows:
        cols = list(dict.fromkeys(k for row in rows for k in row.keys()))
        writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else str(v) for k, v in row.items()})
    return output.getvalue()


def safe_float(value):
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


@st.fragment(run_every="60s")
def monitor():
    try:
        with psycopg.connect(url, connect_timeout=10, sslmode="require", row_factory=dict_row) as conn:
            conn.execute("SET default_transaction_read_only = on")
            predictions = read_table(conn, "bot_predictions", 1000)
            shadow = read_table(conn, "bot_shadow_trades", 2000)
            trades = read_table(conn, "bot_trades", 1000)
    except (psycopg.Error, ValueError) as exc:
        # Do not print exception text: connection errors may include credentials.
        st.error("Unable to read Supabase tables. Verify the Streamlit secret, database permissions, and bot_private table schema.")
        st.caption(f"Error type: {type(exc).__name__}")
        return

    st.caption(f"Last dashboard refresh: {datetime.now(ZoneInfo('America/New_York')).strftime('%b %d, %Y %I:%M:%S %p ET')} • Up to 1,000 predictions/trades and 2,000 shadow quotes displayed")
    st.subheader("Prediction accuracy tracker")
    st.caption("One saved forecast per market. Only confirmed YES/NO results count as settled. Skipped trades can still have predictions.")
    completed = [r for r in predictions if r.get("result") in ("YES", "NO")]
    correct = sum(r.get("predicted_side") == r.get("result") for r in completed)
    scored = [r for r in completed if safe_float(r.get("yes_probability")) is not None]
    brier = sum((safe_float(r["yes_probability"]) - (1 if r["result"] == "YES" else 0)) ** 2 for r in scored) / len(scored) if scored else None
    a, b, c, d = st.columns(4)
    a.metric("Markets recorded", len(predictions))
    b.metric("Settled predictions", len(completed))
    c.metric("Directional accuracy", f"{100 * correct / len(completed):.1f}%" if completed else "—")
    d.metric("Brier score (lower better)", f"{brier:.3f}" if brier is not None else "—")

    
# 100-Prediction Performance Tracker
st.markdown("### 100-Prediction Testing Progress")

target = 100
settled_count = len(completed)
progress = min(settled_count / target, 1.0)

st.progress(progress)
st.caption(f"{settled_count} / {target} settled predictions")

remaining = max(target - settled_count, 0)

p1, p2, p3 = st.columns(3)
p1.metric("Predictions remaining", remaining)
p2.metric(
    "Current accuracy",
    f"{100 * correct / settled_count:.1f}%" if settled_count else "—"
)
p3.metric(
    "Current Brier score",
    f"{brier:.3f}" if brier is not None else "—"
)

# Historical performance at fixed milestones
st.markdown("#### Performance milestones")

chronological = sorted(
    completed,
    key=lambda row: str(row.get("created", ""))
)

milestone_rows = []

for milestone in (25, 50, 75, 100):
    if len(chronological) >= milestone:
        sample = chronological[:milestone]

        wins = sum(
            r.get("predicted_side") == r.get("result")
            for r in sample
        )

        accuracy = 100 * wins / milestone

        valid = [
            r for r in sample
            if safe_float(r.get("yes_probability")) is not None
        ]

        milestone_brier = (
            sum(
                (
                    safe_float(r["yes_probability"])
                    - (1 if r["result"] == "YES" else 0)
                ) ** 2
                for r in valid
            ) / len(valid)
            if valid else None
        )

        milestone_rows.append({
            "Milestone": milestone,
            "Status": "Completed",
            "Accuracy": f"{accuracy:.1f}%",
            "Brier score": (
                f"{milestone_brier:.3f}"
                if milestone_brier is not None
                else "—"
            ),
        })
    else:
        milestone_rows.append({
            "Milestone": milestone,
            "Status": "Pending",
            "Accuracy": "—",
            "Brier score": "—",
        })

st.dataframe(
    milestone_rows,
    hide_index=True,
    use_container_width=True
)

if settled_count >= target:
    st.success(
        "100 settled predictions collected! "
        "Ready for a full performance review."
    )
else:
    st.info(
        f"Continue paper testing with the existing filters. "
        f"{remaining} more settled predictions needed."
    )

    if completed:
        hits = 0
        chart = []
        for i, r in enumerate(sorted(completed, key=lambda row: str(row.get("created", ""))), 1):
            hits += r.get("predicted_side") == r.get("result")
            chart.append({"Settled market": i, "Cumulative accuracy (%)": 100 * hits / i})
        st.line_chart(chart, x="Settled market", y="Cumulative accuracy (%)")
    else:
        st.info("No settled predictions yet.")
    display_predictions = [{
        "Market": r.get("ticker"),
        "Recorded (UTC)": str(r.get("created", "")),
        "YES forecast": f"{100 * safe_float(r['yes_probability']):.1f}%" if safe_float(r.get("yes_probability")) is not None else "—",
        "Predicted": r.get("predicted_side"),
        "Decision": r.get("decision"),
        "Actual result": r.get("result") or "Pending",
        "Correct": "Pending" if r.get("result") not in ("YES", "NO") else ("Yes" if r.get("predicted_side") == r.get("result") else "No"),
    } for r in predictions[:100]]
    st.dataframe(display_predictions, hide_index=True, use_container_width=True)
    st.download_button("Download prediction history (CSV)", csv_data(predictions), file_name="kalshi_predictions.csv", mime="text/csv")

    st.subheader("Shadow trades — hypothetical quotes")
    st.caption("YES and NO quotes for the same market are alternative hypothetical scenarios, not a combined portfolio. Rejected quotes were not executed.")
    settled_shadow = [r for r in shadow if safe_float(r.get("estimated_pnl")) is not None]
    rejected = [r for r in settled_shadow if r.get("filter_reasons") != "eligible"]
    eligible = [r for r in settled_shadow if r.get("filter_reasons") == "eligible"]
    s1, s2, s3 = st.columns(3)
    s1.metric("Shadow quotes recorded", len(shadow))
    s2.metric("Settled rejected quotes", len(rejected))
    s3.metric("Rejected quote hypothetical P/L", f"${sum(safe_float(r['estimated_pnl']) for r in rejected):.2f}" if rejected else "—")
    if settled_shadow:
        st.caption(f"Eligible quotes settled: {len(eligible)} | Their hypothetical P/L: ${sum(safe_float(r['estimated_pnl']) for r in eligible):.2f}. These are not realized profits.")
    st.dataframe(shadow[:200], hide_index=True, use_container_width=True)
    st.download_button("Download shadow-trade history (CSV)", csv_data(shadow), file_name="kalshi_shadow_trades.csv", mime="text/csv")

    st.subheader("Simulated paper trades")
    st.caption("Only entries actually recorded in bot_trades count as simulated trades; predictions and shadow quotes do not.")
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    paper = [r for r in trades if r.get("mode", "paper") == "paper"]
    today_rows = [r for r in paper if str(r.get("day", ""))[:10] == today]
    settled = [r for r in paper if safe_float(r.get("estimated_pnl")) is not None]
    t1, t2, t3 = st.columns(3)
    t1.metric("Paper entries today", len(today_rows))
    t2.metric("Budget reserved today", f"${sum(safe_float(r.get('reserved')) or 0 for r in today_rows):.2f}")
    t3.metric("Estimated settled P/L (shown history)", f"${sum(safe_float(r['estimated_pnl']) for r in settled):.2f}" if settled else "—")
    if settled:
        wins = sum(safe_float(r["estimated_pnl"]) > 0 for r in settled)
        st.metric("Simulated settled win rate", f"{100 * wins / len(settled):.1f}%")
        running = 0.0
        chart = []
        for r in sorted(settled, key=lambda row: str(row.get("created", ""))):
            running += safe_float(r["estimated_pnl"])
            chart.append({"Trade": len(chart) + 1, "Cumulative estimated P/L ($)": running})
        st.line_chart(chart, x="Trade", y="Cumulative estimated P/L ($)")
    else:
        st.info("No settled simulated paper trades yet. This is normal when all entry filters reject quotes.")
    st.dataframe(paper[:100], hide_index=True, use_container_width=True)
    st.download_button("Download paper-trade history (CSV)", csv_data(paper), file_name="kalshi_paper_trades.csv", mime="text/csv")
    st.caption("Figures are preliminary and based on limited saved history. The model is experimental and unvalidated.")


monitor()
