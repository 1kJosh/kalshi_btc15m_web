"""Browser control panel. Paper only; scans while a browser session stays connected."""
import os, re, sqlite3, subprocess, sys, time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'trades.sqlite3'
st.set_page_config(page_title='BTC 15m Bot', page_icon='₿', layout='wide')
st.title('₿ BTC 15-Minute Bot')
st.caption('Kalshi KXBTC15M • PAPER SIMULATION ONLY • Experimental strategy')

# Password protection is required for any publicly deployed copy.
password = os.environ.get('DASHBOARD_PASSWORD', '')
if not password:
    st.error('Set DASHBOARD_PASSWORD in your hosting service secrets before using this dashboard.')
    st.stop()
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if not st.session_state.authenticated:
    import hmac
    supplied = st.text_input('Dashboard password', type='password')
    if st.button('Unlock') and hmac.compare_digest(supplied, password):
        st.session_state.authenticated = True
        st.rerun()
    st.stop()

st.warning('No real Kalshi orders are sent. Paper fills are estimated and may differ from actual execution. This is not a validated profitable strategy.')
if 'running' not in st.session_state:
    st.session_state.running = False
if 'last_scan' not in st.session_state:
    st.session_state.last_scan = 0.0
if 'scan_log' not in st.session_state:
    st.session_state.scan_log = 'No scans yet.'
if 'snapshot' not in st.session_state:
    st.session_state.snapshot = None

with st.sidebar:
    st.header('Controls')
    st.write('Trading mode: **Paper only**')
    interval = st.slider('Scan every (seconds)', 20, 120, 30, 10)
    max_order = st.number_input('Max simulated order ($)', min_value=1, max_value=5, value=5)
    max_daily = st.number_input('Max daily simulated spend ($)', min_value=5, max_value=20, value=20)
    max_trades = st.number_input('Max trades/day', min_value=1, max_value=8, value=8)
    edge = st.slider('Minimum model edge', 0.05, 0.30, 0.12, 0.01)
    a,b = st.columns(2)
    if a.button('▶ Start', use_container_width=True):
        st.session_state.running = True
        st.session_state.last_scan = 0.0
    if b.button('■ Stop', use_container_width=True):
        st.session_state.running = False
    if st.button('Scan once', use_container_width=True):
        st.session_state.last_scan = 0.0
        st.session_state.force_scan = True
    st.caption('Auto-scanning works only while this page is open and connected. Cloud hosts may sleep or reset stored trades.')

def scan():
    env = os.environ.copy()
    env.update(MODE='paper', ENABLE_LIVE='false', LIVE_CONFIRM='', KALSHI_KEY_ID='', KALSHI_PRIVATE_KEY_PATH='',
               MAX_ORDER_DOLLARS=str(max_order), MAX_DAILY_SPEND=str(max_daily), MAX_TRADES_PER_DAY=str(max_trades), MIN_EDGE=str(edge))
    try:
        proc = subprocess.run([sys.executable, str(ROOT/'bot.py'), '--once'], cwd=ROOT,
                              env=env, text=True, capture_output=True, timeout=22)
        st.session_state.scan_log = (proc.stdout + '\n' + proc.stderr)[-7000:] or 'No output.'
        snapshots = re.findall(r'MARKET_SNAPSHOT ([^\n]+)', st.session_state.scan_log)
        if snapshots:
            fields = dict(re.findall(r'(\w+)=([^\s]+)', snapshots[-1]))
            st.session_state.snapshot = fields
        if proc.returncode:
            st.session_state.running = False
            st.session_state.scan_log += f'\nScan exited with status {proc.returncode}.'
    except subprocess.TimeoutExpired:
        st.session_state.scan_log = 'Scan timed out. No retry was attempted.'
        st.session_state.running = False
    except Exception as exc:
        st.session_state.scan_log = f'Scan failed: {type(exc).__name__}: {exc}'
        st.session_state.running = False
    st.session_state.last_scan = time.monotonic()

@st.fragment(run_every='5s')
def monitor():
    running = st.session_state.running
    st.subheader('Status: ' + ('🟢 Scanning' if running else '⚪ Stopped'))
    if st.session_state.pop('force_scan', False) or (running and time.monotonic() - st.session_state.last_scan >= interval):
        scan()
    snap = st.session_state.snapshot
    st.subheader('Latest market snapshot')
    if snap:
        st.caption('From the most recent successful market evaluation; not a live price feed.')
        x,y,z = st.columns(3)
        x.metric('BTC spot (Coinbase)', f"${float(snap['spot']):,.2f}")
        y.metric('Kalshi strike', f"${float(snap['strike']):,.2f}")
        z.metric('Time remaining', f"{int(float(snap['seconds'])) // 60}m {int(float(snap['seconds'])) % 60}s")
        a,b,c,d = st.columns(4)
        a.metric('Model YES probability', f"{float(snap['yes_probability'])*100:.1f}%")
        b.metric('Model NO probability', f"{float(snap['no_probability'])*100:.1f}%")
        c.metric('YES ask', 'Unavailable' if snap['yes_ask']=='NA' else f"${float(snap['yes_ask']):.2f}")
        d.metric('NO ask', 'Unavailable' if snap['no_ask']=='NA' else f"${float(snap['no_ask']):.2f}")
        st.caption(f"Market: {snap['ticker']} | Spread: {snap['spread']}. Model probabilities are uncalibrated estimates, not forecasts.")
        if 'no eligible edge' in st.session_state.scan_log:
            st.info('Decision: SKIP — no trade passed all price, liquidity, spread, model-edge and risk filters.')
        elif 'SIGNAL ' in st.session_state.scan_log:
            st.info('Decision: simulated trade signal generated. Check the scan log for details.')
        else:
            st.info('Decision: no completed entry signal in this scan. Check the log for details.')
    else:
        st.info('No market snapshot yet. Click Scan once, or Start, to load one.')
    if DB.exists():
        try:
            con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute('SELECT day,ticker,side,count,price,reserved,status,result,estimated_pnl,created FROM trades WHERE mode=? ORDER BY created DESC LIMIT 100', ('paper',))]
            con.close()
            today = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
            today_rows = [r for r in rows if r['day']==today]
            c1,c2,c3 = st.columns(3)
            c1.metric('Entries today', len(today_rows))
            c2.metric('Simulated budget reserved', f"${sum(float(r['reserved']) for r in today_rows):.2f}")
            settled = [r for r in rows if r['estimated_pnl'] is not None]
            c3.metric('Estimated settled P/L (shown history)', f"${sum(float(r['estimated_pnl']) for r in settled):.2f}")
            finished = [r for r in rows if r['status']=='settled' and r['estimated_pnl'] is not None]
            if finished:
                wins = sum(float(r['estimated_pnl']) > 0 for r in finished)
                st.metric('Simulated settled win rate', f"{100*wins/len(finished):.1f}%", help=f'{wins} of {len(finished)} settled trades in displayed history')
                chart_rows = sorted(finished, key=lambda r: r['created'])
                running_pnl = 0.0
                chart = []
                for r in chart_rows:
                    running_pnl += float(r['estimated_pnl'])
                    chart.append({'Trade':len(chart)+1,'Cumulative estimated P/L ($)':running_pnl})
                st.line_chart(chart, x='Trade', y='Cumulative estimated P/L ($)')
            else:
                st.caption('Win rate and P/L chart will appear after simulated trades settle.')
            st.subheader('Trade history')
            st.dataframe(rows, use_container_width=True, hide_index=True)
        except sqlite3.Error as exc:
            st.info(f'Trade history not available yet: {exc}')
    else:
        st.info('No simulated trades yet. Start scanning to begin.')
    with st.expander('Latest scan log'):
        st.code(st.session_state.scan_log)
monitor()
st.caption('Model uses Coinbase BTC spot as a proxy, not the CF Benchmarks BRTI used for settlement. Differences can reverse outcomes. Browser controls do not make this a 24/7 hosted worker.')
