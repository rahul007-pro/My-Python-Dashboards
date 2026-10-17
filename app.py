import base64
import codecs
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

BASE_DIR = Path(__file__).resolve().parent
STORED_DIR = BASE_DIR / "scripts" / "stored"
JOBS_DIR = BASE_DIR / "jobs"
DATA_DIR = BASE_DIR / "data"
MANIFEST_PATH = DATA_DIR / "catalog.json"
DEMO_SCRIPT = STORED_DIR / "demo_sample.py"

DEPENDENCIES = [
    "pandas",
    "numpy",
    "yfinance",
    "openpyxl",
    "xlsxwriter",
    "nselib",
    "requests",
    "lxml",
    "beautifulsoup4",
    "html5lib",
]

PROD_SCRIPTS = [
    {"key": "prod_nse_fno_mark_vcp_daily", "name": "NSE Fno Mark VCP Scanner Daily.py"},
    {"key": "prod_vcp_4hour_momentum", "name": "VCP_4hour_momentum_scanner.py"},
    {"key": "prod_last_30_days_vcp", "name": "Last 30 days VCP Scanner.py"},
    {"key": "prod_vcp_enhanced_v2", "name": "VCP_Enhanced_Scanner_v2.py"},
    {"key": "prod_daily_options_2_days_hh_ll", "name": "Daily Options 2 days HH and LL Python.py"},
]

PREPROD_EXTRA = [
    {"key": "pre_strong_vcp_top_gainers", "name": "Strong VCP Study from top gainers.py"},
    {"key": "pre_dynamic_two_day_pattern", "name": "Dynamic two day pattern study.py"},
    {"key": "pre_momentum_top_gainers_losers", "name": "Momentum from two days top gainers and losers.py"},
    {"key": "pre_vcp_minervini_daily_momentum", "name": "VCP Mark Minervini_daily_momentum_scanner.py"},
    {"key": "pre_nse_daily_momentum", "name": "NSEDailyMomentumScanner.py"},
    {"key": "pre_master_vcp_ecosystem", "name": "Master_VCP_Ecosystem_Scanner.py"},
    {"key": "pre_test_40_days_vcp_26jul2026", "name": "test 40 days VCP_26th Jul 2026.py"},
]

PREPROD_SCRIPTS = PROD_SCRIPTS + PREPROD_EXTRA
CATALOG = {"prod": PROD_SCRIPTS, "preprod": PREPROD_SCRIPTS}
ALL_SCRIPTS = PROD_SCRIPTS + PREPROD_EXTRA
SCRIPT_BY_KEY = {item["key"]: item for item in ALL_SCRIPTS}
DEMO = {"key": "demo_sample", "name": "VCP_Demo_Sample_Scanner.py"}

TERMINAL_STATUSES = {"completed", "failed"}
DEMO_SOURCE = '''import time
from datetime import date
from pathlib import Path

import openpyxl

print("VCP Demo Scanner starting")
print("Press Enter to scan with today's date.")
raw = input("Scan date (YYYY-MM-DD, blank = today): ").strip()
scan_date = raw or date.today().isoformat()
print(f"Scan date: {scan_date}")

rows = []
for index, symbol in enumerate(["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT"], start=1):
    print(f"\\rScanning {symbol:<8} {index * 16:>3}%", end="", flush=True)
    time.sleep(0.45)
    rows.append([symbol, round(120 + index * 7.5, 2), index * 3, "VCP CANDIDATE"])

print()
csv_path = Path.cwd() / f"VCP_Demo_{scan_date}.csv"
with csv_path.open("w", encoding="utf-8", newline="") as handle:
    handle.write("symbol,close,breakout_score,setup\\n")
    for row in rows:
        handle.write(",".join(str(value) for value in row) + "\\n")

workbook = openpyxl.Workbook()
sheet = workbook.active
sheet.title = "VCP Demo"
sheet.append(["Symbol", "Close", "Breakout Score", "Setup"])
for row in rows:
    sheet.append(row)
xlsx_path = Path.cwd() / f"VCP_Demo_{scan_date}.xlsx"
workbook.save(xlsx_path)

print(f"Created {csv_path.name}")
print(f"Created {xlsx_path.name}")
print("Demo scan complete")
'''

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["JSON_SORT_KEYS"] = False

_jobs = {}
_jobs_order = []
_jobs_lock = threading.Lock()
_manifest_lock = threading.Lock()
_dependency_lock = threading.Lock()
_dependency_state = "pending"


def utcnow():
    return datetime.now(timezone.utc)


def iso_now():
    return utcnow().isoformat()


def ensure_directories():
    STORED_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DEMO_SCRIPT.exists():
        DEMO_SCRIPT.write_text(DEMO_SOURCE, encoding="utf-8")


def load_manifest():
    if not MANIFEST_PATH.exists():
        return {}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(manifest):
    temp_path = MANIFEST_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, MANIFEST_PATH)


def script_path(key):
    return STORED_DIR / f"{key}.py"


def script_entry(key, manifest=None):
    if manifest is None:
        manifest = load_manifest()
    item = SCRIPT_BY_KEY.get(key)
    if item is None:
        return None
    path = script_path(key)
    meta = manifest.get(key, {})
    exists = path.is_file()
    stat = path.stat() if exists else None
    return {
        "key": key,
        "name": item["name"],
        "uploaded": exists,
        "original_name": meta.get("original_name") if exists else None,
        "uploaded_at": meta.get("uploaded_at") if exists else None,
        "size": stat.st_size if stat else 0,
    }


def catalog_entries(catalog_name, manifest=None):
    return [script_entry(item["key"], manifest) for item in CATALOG[catalog_name]]


def demo_entry():
    exists = DEMO_SCRIPT.is_file()
    stat = DEMO_SCRIPT.stat() if exists else None
    return {
        "key": DEMO["key"],
        "name": DEMO["name"],
        "uploaded": exists,
        "original_name": DEMO["name"] if exists else None,
        "uploaded_at": None,
        "size": stat.st_size if stat else 0,
    }


class Job:
    def __init__(self, title, kind):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.kind = kind
        self.status = "queued"
        self.created_at = iso_now()
        self.started_at = None
        self.finished_at = None
        self.current = 0
        self.total = 0
        self.current_label = ""
        self.exit_code = None
        self.error = None
        self.log_parts = []
        self.outputs = []
        self.workdir = JOBS_DIR / self.id
        self.condition = threading.Condition(threading.RLock())

    def append_log(self, text):
        if not text:
            return
        with self.condition:
            self.log_parts.append(text)
            self.condition.notify_all()

    def update(self, **values):
        with self.condition:
            for key, value in values.items():
                setattr(self, key, value)
            self.condition.notify_all()

    def summary(self):
        with self.condition:
            return self._summary_locked()

    def _summary_locked(self):
        started = self.started_at
        finished = self.finished_at
        duration = None
        if started:
            start_dt = datetime.fromisoformat(started)
            end_dt = datetime.fromisoformat(finished) if finished else utcnow()
            duration = max(0, int((end_dt - start_dt).total_seconds()))
        files = []
        for output in self.outputs:
            files.append(
                {
                    "name": output["name"],
                    "size": output["size"],
                    "extension": output["extension"],
                    "url": f"/api/jobs/{self.id}/files/{output['token']}",
                }
            )
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": duration,
            "current": self.current,
            "total": self.total,
            "current_label": self.current_label,
            "exit_code": self.exit_code,
            "error": self.error,
            "files": files,
            "workdir": str(self.workdir),
        }


def add_job(title, kind):
    job = Job(title, kind)
    job.workdir.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        _jobs[job.id] = job
        _jobs_order.append(job.id)
    return job


def get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def list_jobs(limit=30):
    with _jobs_lock:
        ids = list(reversed(_jobs_order[-limit:]))
        return [_jobs[job_id].summary() for job_id in ids if job_id in _jobs]


def recent_jobs(limit=30):
    with _jobs_lock:
        ids = list(reversed(_jobs_order[-limit:]))
        return [_jobs[job_id] for job_id in ids if job_id in _jobs]


def encode_token(relative_path):
    raw = base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


def decode_token(token):
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding).decode("utf-8")


def collect_outputs(job):
    workdir = job.workdir.resolve()
    outputs = []
    if not workdir.exists():
        return
    for path in sorted(workdir.rglob("*")):
        try:
            resolved = path.resolve()
            if not path.is_file() or not resolved.is_relative_to(workdir):
                continue
            relative = resolved.relative_to(workdir)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            stat = resolved.stat()
            outputs.append(
                {
                    "name": relative.as_posix(),
                    "size": stat.st_size,
                    "extension": resolved.suffix.lower().lstrip(".") or "file",
                    "token": encode_token(relative.as_posix()),
                }
            )
        except OSError:
            continue
    with job.condition:
        job.outputs = outputs
        job.condition.notify_all()


def ensure_dependencies(job):
    global _dependency_state
    with _dependency_lock:
        if _dependency_state == "ready":
            job.append_log("[runtime] Scanner dependencies verified.\n")
            return True
        job.append_log("[runtime] Installing scanner dependencies. This can take a minute on first run.\n")
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--break-system-packages",
            "--disable-pip-version-check",
            "--no-input",
            "-q",
            *DEPENDENCIES,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            )
            if process.stdout:
                for line in process.stdout:
                    job.append_log(line)
            code = process.wait()
        except OSError as exc:
            job.append_log(f"[runtime] Dependency installer could not start: {exc}\n")
            _dependency_state = "failed"
            return False
        if code == 0:
            _dependency_state = "ready"
            job.append_log("[runtime] Scanner dependencies installed.\n")
            return True
        _dependency_state = "failed"
        job.append_log("[runtime] Dependency installation failed. The script will still be attempted.\n")
        return False


def run_script_process(job, label, path):
    job.append_log("\n" + "=" * 72 + "\n")
    job.append_log(f"[{utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] RUN {label}\n")
    job.append_log(f"[runtime] Script: {path.name}\n")
    job.append_log(f"[runtime] Workdir: {job.workdir}\n\n")
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "MPLBACKEND": "Agg",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(path)],
            cwd=str(job.workdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=0,
        )
    except OSError as exc:
        job.append_log(f"[runtime] Could not start Python: {exc}\n")
        return 1
    try:
        if process.stdin:
            try:
                process.stdin.write(b"\n")
                process.stdin.flush()
            except OSError:
                pass
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = process.stdout.fileno() if process.stdout else None
        if fd is not None:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    job.append_log(text.replace("\r\n", "\n").replace("\r", "\n"))
        tail = decoder.decode(b"", final=True)
        if tail:
            job.append_log(tail.replace("\r\n", "\n").replace("\r", "\n"))
        code = process.wait()
    finally:
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass
    job.append_log(f"\n[runtime] Finished with exit code {code}.\n")
    return code


def execute_job(job, items, install_first=True):
    job.update(status="running", started_at=iso_now(), total=len(items))
    failures = 0
    try:
        job.append_log(f"[runtime] Job {job.id} started.\n")
        job.append_log(f"[runtime] Python {sys.version.split()[0]} on {sys.platform}.\n")
        if install_first:
            ensure_dependencies(job)
        for index, item in enumerate(items, start=1):
            label = item["label"]
            path = Path(item["path"])
            job.update(current=index, current_label=label)
            if not path.is_file():
                job.append_log(f"\n[runtime] Missing script, skipping: {label}\n")
                failures += 1
                continue
            code = run_script_process(job, label, path)
            if code != 0:
                failures += 1
            collect_outputs(job)
        collect_outputs(job)
        if failures:
            job.update(
                status="failed",
                error=f"{failures} script(s) reported a failure.",
                exit_code=1,
            )
        else:
            job.update(status="completed", exit_code=0)
    except Exception as exc:
        job.append_log(f"\n[runtime] Job crashed: {exc}\n")
        job.update(status="failed", error=str(exc), exit_code=1)
    finally:
        job.update(finished_at=iso_now(), current_label="")


def start_job(title, items, kind):
    job = add_job(title, kind)
    thread = threading.Thread(target=execute_job, args=(job, items), daemon=True)
    thread.start()
    return job


def script_items_from_catalog(catalog_name):
    manifest = load_manifest()
    items = []
    for item in CATALOG[catalog_name]:
        path = script_path(item["key"])
        if path.is_file():
            items.append({"label": item["name"], "path": str(path)})
    return items


def build_state():
    manifest = load_manifest()
    prod = catalog_entries("prod", manifest)
    preprod = catalog_entries("preprod", manifest)
    jobs = list_jobs()
    active = sum(1 for job in jobs if job["status"] in {"queued", "running"})
    return {
        "app": "VCP Scanner Control Deck",
        "server_time": iso_now(),
        "port": 5000,
        "catalogs": {"prod": prod, "preprod": preprod},
        "demo": {**demo_entry(), "source": DEMO_SOURCE},
        "jobs": jobs,
        "stats": {
            "prod_ready": sum(1 for item in prod if item["uploaded"]),
            "prod_total": len(prod),
            "preprod_ready": sum(1 for item in preprod if item["uploaded"]),
            "preprod_total": len(preprod),
            "active_jobs": active,
            "total_jobs": len(jobs),
        },
        "dependencies": _dependency_state,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": iso_now()})


@app.get("/api/state")
def api_state():
    return jsonify(build_state())


@app.post("/api/catalog/<key>/upload")
def upload_script(key):
    if key not in SCRIPT_BY_KEY:
        return jsonify({"error": "Unknown script slot."}), 404
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Choose a .py file to upload."}), 400
    original_name = Path(uploaded.filename).name
    if Path(original_name).suffix.lower() != ".py":
        return jsonify({"error": "Only Python .py files are accepted."}), 400
    destination = script_path(key)
    temp_path = destination.with_suffix(".uploading")
    uploaded.save(temp_path)
    if temp_path.stat().st_size == 0:
        return jsonify({"error": "The uploaded file is empty."}), 400
    os.replace(temp_path, destination)
    with _manifest_lock:
        manifest = load_manifest()
        manifest[key] = {
            "original_name": original_name,
            "uploaded_at": iso_now(),
            "size": destination.stat().st_size,
        }
        save_manifest(manifest)
    return jsonify({"ok": True, "script": script_entry(key, manifest)})


@app.post("/api/run")
def run_job():
    payload = request.get_json(silent=True) or {}
    scope = payload.get("scope")
    if scope == "demo":
        if not DEMO_SCRIPT.is_file():
            return jsonify({"error": "Demo scanner is not available."}), 400
        job = start_job("Demo sample scanner", [{"label": DEMO["name"], "path": str(DEMO_SCRIPT)}], "demo")
        return jsonify({"ok": True, "job_id": job.id})
    if scope == "script":
        key = payload.get("key")
        item = SCRIPT_BY_KEY.get(key)
        if item is None:
            return jsonify({"error": "Unknown script."}), 404
        path = script_path(key)
        if not path.is_file():
            return jsonify({"error": "Upload this script before running it."}), 400
        job = start_job(item["name"], [{"label": item["name"], "path": str(path)}], "script")
        return jsonify({"ok": True, "job_id": job.id})
    if scope == "scheduler":
        catalog_name = payload.get("catalog")
        if catalog_name not in CATALOG:
            return jsonify({"error": "Unknown scheduler."}), 404
        items = script_items_from_catalog(catalog_name)
        if not items:
            return jsonify({"error": "Upload at least one script before running the scheduler."}), 400
        title = f"{catalog_name.upper()} entire scheduler"
        job = start_job(title, items, "scheduler")
        return jsonify({"ok": True, "job_id": job.id, "script_count": len(items)})
    return jsonify({"error": "Unknown run scope."}), 400


@app.get("/api/jobs/<job_id>")
def job_snapshot(job_id):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job.summary())


def sse_event(event, data, event_id=None):
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@app.get("/api/jobs/<job_id>/events")
def job_events(job_id):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    try:
        start_index = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        start_index = 0

    @stream_with_context
    def generate():
        index = max(0, start_index)
        last_state = None
        yield "retry: 3000\n\n"
        while True:
            with job.condition:
                while index >= len(job.log_parts) and job.status not in TERMINAL_STATUSES:
                    job.condition.wait()
                chunks = job.log_parts[index:]
                index = len(job.log_parts)
                summary = job._summary_locked()
                terminal = job.status in TERMINAL_STATUSES
            state_payload = json.dumps(summary, sort_keys=True, ensure_ascii=False)
            if state_payload != last_state:
                last_state = state_payload
                yield sse_event("state", summary)
            if chunks:
                yield sse_event("log", {"text": "".join(chunks)}, index)
            if terminal:
                yield sse_event("done", summary)
                break

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/<job_id>/files/<token>")
def download_output(job_id, token):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    try:
        relative = decode_token(token)
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "Invalid file token."}), 400
    workdir = job.workdir.resolve()
    target = (workdir / relative).resolve()
    if not target.is_relative_to(workdir) or not target.is_file():
        return jsonify({"error": "File not found."}), 404
    return send_file(target, as_attachment=True, download_name=target.name)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "File is too large. Maximum upload size is 10 MB."}), 413


def main():
    ensure_directories()
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False, use_reloader=False)


ensure_directories()

if __name__ == "__main__":
    main()
