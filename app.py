"""
NSE F&O VCP / Momentum Scanner Dashboard
One-click runner for all scanner scripts.
Works when all files are in the root of the repository.
"""

import streamlit as st
import subprocess
import sys
import os
from datetime import datetime, date
from pathlib import Path
import time

# ---------------------------------------------------------------------------
# Paths - everything is in the same folder (repo root)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SCANNERS_DIR = BASE_DIR                    # scanners are in the same folder
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Script groups (mirroring the original BAT files)
# ---------------------------------------------------------------------------
PROD_SCRIPTS = [
    "NSE Fno Mark VCP Scanner Daily.py",
    "VCP_4hour_momentum_scanner.py",
    "Last 30 days VCP Scanner.py",
    "VCP_Enhanced_Scanner_v2.py",
    "Daily Options 2 days HH and LL Python.py",
]

PREPROD_SCRIPTS = [
    "NSE Fno Mark VCP Scanner Daily.py",
    "Strong VCP Study from top gainers.py",
    "Dynamic two day pattern study.py",
    "Momentum from two days top gainers and losers.py",
    "VCP_4hour_momentum_scanner.py",
    "VCP Mark Minervini_daily_momentum_scanner.py",
    "NSEDailyMomentumScanner.py",
    "Last 30 days VCP Scanner.py",
    "VCP_Enhanced_Scanner_v2.py",
    "Master_VCP_Ecosystem_Scanner.py",
    "test 40 days VCP_26th Jul 2026.py",
    "Daily Options 2 days HH and LL Python.py",
]

EXTRA_SCRIPTS = [
    "VCP_Enhanced_Scanner_Updated.py",
    "test 2 days HH and LL.py",
]

ALL_KNOWN = list(dict.fromkeys(PROD_SCRIPTS + PREPROD_SCRIPTS + EXTRA_SCRIPTS))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def list_available_scripts():
    available = []
    for name in ALL_KNOWN:
        p = SCANNERS_DIR / name
        if p.exists():
            available.append(name)
    for p in SCANNERS_DIR.glob("*.py"):
        if p.name not in available and p.name != "app.py":
            available.append(p.name)
    return available


def run_script(script_name: str, target_date: str, timeout: int = 900) -> tuple[str, str, int]:
    script_path = SCANNERS_DIR / script_name
    if not script_path.exists():
        return "", f"Script not found: {script_path}", 1

    stdin_data = f"{target_date}\n"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=str(OUTPUTS_DIR),
            env=env,
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", f"Timed out after {timeout}s", -1
    except Exception as e:
        return "", str(e), -2


def get_output_files(since_ts: float | None = None):
    files = []
    for pattern in ("*.xlsx", "*.xls", "*.csv"):
        for f in OUTPUTS_DIR.glob(pattern):
            if since_ts is None or f.stat().st_mtime >= since_ts:
                files.append(f)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="NSE F&O Scanner Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>

/* Main App */
.stApp {
    background: linear-gradient(135deg,#0B1120,#111827);
}

/* Header */
.dashboard-header {
    padding: 1rem;
    border-radius: 15px;
    background: linear-gradient(90deg,#0ea5e9,#2563eb);
    color: white;
    margin-bottom: 20px;
}

/* Cards */
.metric-card {
    background: #1f2937;
    padding: 15px;
    border-radius: 12px;
    border: 1px solid #374151;
    text-align: center;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: #0f172a;
}

/* Tabs */
.stTabs [data-baseweb="tab"] {
    font-size: 15px;
    font-weight: 600;
    padding: .75rem 1rem;
}

/* Buttons */
.stButton > button {
    background: linear-gradient(90deg,#2563eb,#1d4ed8);
    color: white;
    border-radius: 10px;
    border: none;
    font-weight: bold;
}

.stButton > button:hover {
    background: linear-gradient(90deg,#3b82f6,#2563eb);
    transform: scale(1.02);
}

/* Metrics */
div[data-testid="metric-container"] {
    background-color: #111827;
    border: 1px solid #334155;
    padding: 15px;
    border-radius: 12px;
}

/* File Cards */
.file-card {
    background: #111827;
    border: 1px solid #334155;
    padding: 12px;
    border-radius: 10px;
}

</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="dashboard-header">
    <h1>📈 NSE F&O Scanner Pro</h1>
    <p>Volume Contraction Pattern • Momentum • Ecosystem Scanner Dashboard</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("## ⚙️ Control Center")
    st.success("System Ready")
    target_date = st.date_input(
        "Target analysis date",
        value=date.today(),
        help="Most scanners use this as the end / back-test date. Leave as today for live scan.",
    )
    date_str = target_date.strftime("%Y-%m-%d")

    st.markdown("---")
    st.subheader("Quick Run")
    run_prod = st.button("▶ Run All PROD", type="primary", use_container_width=True)
    run_preprod = st.button("▶ Run All PrePROD", use_container_width=True)
    st.markdown("---")
    st.info(
        "Scripts run on Streamlit Cloud. Excel outputs appear below and can be downloaded. "
        "Long scans (Master Ecosystem, 30-day windows) can take 3–10 minutes."
    )

tab_prod, tab_preprod, tab_all, tab_logs, tab_files = st.tabs(
    ["PROD (5 scripts)", "PrePROD (12 scripts)", "All Scripts", "Live Logs", "Output Files"]
)

available = list_available_scripts()
files_count = len(get_output_files())

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "Available Scripts",
    len(available)
)

c2.metric(
    "PROD Scripts",
    len(PROD_SCRIPTS)
)

c3.metric(
    "PrePROD Scripts",
    len(PREPROD_SCRIPTS)
)

c4.metric(
    "Output Files",
    files_count
)

filtered_scripts = [
    s for s in available
    if search_text.lower() in s.lower()
]
cols = st.columns(2)
for i, name in enumerate(filtered_scripts):
    col = cols[i % 2]
    exists = name in available
    label = f"🟢 {name}" if exists else f"🔴 {name}"
        if col.button(label, key=f"{key_prefix}_{i}", disabled=not exists, use_container_width=True):
            with st.spinner(f"Running {name} …"):
                start = time.time()
                out, err, code = run_script(name, date_str)
                elapsed = time.time() - start
            st.session_state["last_log"] = {
                "script": name,
                "date": date_str,
                "stdout": out,
                "stderr": err,
                "code": code,
                "elapsed": elapsed,
            }
            st.rerun()

with tab_prod:
    st.subheader("PROD Scheduler (mirrors PROD Python Scheduler.bat)")
    st.write("These five scripts match your production BAT.")
    render_script_buttons(PROD_SCRIPTS, "prod")

with tab_preprod:
    st.subheader("PrePROD / Test Scheduler (mirrors Test2 PrePROD BAT)")
    st.write("Full set of research & experimental scanners.")
    render_script_buttons(PREPROD_SCRIPTS, "preprod")

with tab_all:
    st.subheader("Every available scanner")
    render_script_buttons(available, "all")

if run_prod or run_preprod:
    scripts_to_run = PROD_SCRIPTS if run_prod else PREPROD_SCRIPTS
    group_name = "PROD" if run_prod else "PrePROD"
    st.session_state["bulk_log"] = []
    progress = st.progress(0, text=f"Starting {group_name} …")
    log_placeholder = st.empty()

    for idx, name in enumerate(scripts_to_run):
        if name not in available:
            st.session_state["bulk_log"].append(f"SKIPPED (not found): {name}")
            continue
        progress.progress((idx) / len(scripts_to_run), text=f"Running {name} …")
        start = time.time()
        out, err, code = run_script(name, date_str)
        elapsed = time.time() - start
        status = "OK" if code == 0 else f"FAIL ({code})"
        entry = f"[{status}] {name} ({elapsed:.1f}s)\n"
        if out:
            entry += out[-3000:] + "\n"
        if err:
            entry += "STDERR:\n" + err[-1500:] + "\n"
        st.session_state["bulk_log"].append(entry)
        log_placeholder.code("\n".join(st.session_state["bulk_log"][-5:]), language="text")

    progress.progress(1.0, text=f"{group_name} finished")
    st.success(f"{group_name} batch completed. Check Live Logs and Output Files tabs.")

with tab_logs:
    st.subheader("Last single-script run")
    if "last_log" in st.session_state:
        log = st.session_state["last_log"]
        st.markdown(f"**Script:** `{log['script']}`  \n**Date:** {log['date']}  \n**Exit code:** {log['code']}  \n**Elapsed:** {log['elapsed']:.1f}s")
        if log["stdout"]:
            st.text_area("STDOUT", log["stdout"], height=300)
        if log["stderr"]:
            st.text_area("STDERR", log["stderr"], height=150)
    else:
        st.info("No single-script run yet. Click any script button.")

    st.subheader("Bulk run log")
    if "bulk_log" in st.session_state and st.session_state["bulk_log"]:
        st.code("\n".join(st.session_state["bulk_log"]), language="text")
    else:
        st.info("No bulk run yet.")

with tab_files:
    st.subheader("Generated Excel / CSV files")
    files = get_output_files()
    if not files:
        st.info("No output files yet. Run a scanner first.")
    else:
        for f in files:
            col1, col2, col3 = st.columns([4, 1, 1])
            size_kb = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            col1.markdown(
    f"""
    <div class='file-card'>
        <b>{f.name}</b><br>
        {size_kb:.1f} KB<br>
        {mtime}
    </div>
    """,
    unsafe_allow_html=True
)
            with open(f, "rb") as fp:
                col2.download_button(
                    "Download",
                    data=fp,
                    file_name=f.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_{f.name}",
                )
            if col3.button("🗑", key=f"del_{f.name}"):
                f.unlink()
                st.rerun()

st.markdown("---")
st.markdown("""
---
<center>
<b>NSE Scanner Pro Dashboard</b><br>
Production • Research • Momentum • VCP Analysis
</center>
""", unsafe_allow_html=True)
