import json
import os
import platform
import shutil
import sqlite3
import threading
import time
import uuid
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from scanner import scan_repo, scan_fixture, regression_checks, quick_npm_probe, repair_npm11_lockfile

APP_VERSION = "0.5.0"
BASE_DIR = Path(__file__).resolve().parent


def pick_data_dir() -> Path:
    configured = os.getenv("DATA_DIR", "/data")
    p = Path(configured)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return p
    except Exception:
        fallback = BASE_DIR / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = pick_data_dir()
DB_PATH = DATA_DIR / "willitdeploy.sqlite3"

# Build workspaces and downloaded Node runtimes are intentionally ephemeral.
# Railway volumes can be small; using /tmp keeps large runtime tarballs/extracts
# off the persistent volume while preserving only the lightweight SQLite history.
TEMP_STORAGE_DIR = Path(
    os.getenv("TEMP_STORAGE_DIR", str(Path(tempfile.gettempdir()) / "willitdeploy"))
)
WORK_DIR = TEMP_STORAGE_DIR / "work"
RUNTIME_DIR = TEMP_STORAGE_DIR / "runtimes"
WORK_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("SCAN_WORKERS", "1"))))
write_lock = threading.Lock()


RESEARCH_BATCH_ID = "npm11-repair-003"
RESEARCH_STATE_PATH = DATA_DIR / "research_state.json"
research_state_lock = threading.Lock()
research_thread_started = False

RESEARCH_TARGETS = [
    {"repo": "https://github.com/Crypto-Mikael/tailwind-material", "branch": "main", "project_path": ".", "label": "positive control"},
    {"repo": "https://github.com/Ontotext-AD/graphdb.js", "branch": None, "project_path": None, "label": "confirmed npm11 mismatch control"},
    {"repo": "https://github.com/timkindberg/formframe", "branch": None, "project_path": None, "label": "reported npm11 esbuild optional mismatch"},
    {"repo": "https://github.com/jQuinRivero/palimpsest", "branch": None, "project_path": "frontend", "label": "reported npm11.8+ lockfile rejection"},
    {"repo": "https://github.com/ruvnet/metaharness", "branch": None, "project_path": None, "label": "reported npm10 pass/npm11 fail"},
    {"repo": "https://github.com/dmccoystephenson/atomic-core", "branch": None, "project_path": None, "label": "reported npm11 emnapi lock mismatch"},
    {"repo": "https://github.com/ConductionNL/versioniq", "branch": "development", "project_path": None, "label": "reported npm10/npm11 toolchain drift"},
    {"repo": "https://github.com/mei-shui-xing/galatea-garden-chatgpt-wake-mcp", "branch": None, "project_path": None, "label": "reported npm11 wasi lock mismatch"},
    {"repo": "https://github.com/cheeriojs/cheerio", "branch": "main", "project_path": ".", "label": "negative control"},
    {"repo": "https://github.com/uuidjs/uuid", "branch": "main", "project_path": ".", "label": "modern npm control"},
]


def research_default_state():
    return {
        "batch_id": RESEARCH_BATCH_ID,
        "status": "pending",
        "stage": "queued",
        "started_at": None,
        "updated_at": now_ts(),
        "finished_at": None,
        "current": None,
        "targets_total": len(RESEARCH_TARGETS),
        "probe_completed": 0,
        "confirm_completed": 0,
        "repair_completed": 0,
        "candidates": 0,
        "confirmed": 0,
        "repair_verified": 0,
        "money_earned_usd": 0,
        "monetization_gate": "Need reproducible independent compatibility failures before selling anything.",
        "results": [],
        "confirmations": [],
        "repairs": [],
        "notes": [
            "Quick probes use npm ci --dry-run --ignore-scripts; target lifecycle scripts are not executed.",
            "Repair validation only allows package-lock.json to change and verifies npm 10 + npm 11 afterwards."
        ],
    }


def read_research_state():
    with research_state_lock:
        if not RESEARCH_STATE_PATH.exists():
            return research_default_state()
        try:
            data = json.loads(RESEARCH_STATE_PATH.read_text(encoding="utf-8"))
            if data.get("batch_id") != RESEARCH_BATCH_ID:
                return research_default_state()
            return data
        except Exception:
            return research_default_state()


def write_research_state(state):
    state["updated_at"] = now_ts()
    tmp = RESEARCH_STATE_PATH.with_suffix(".tmp")
    with research_state_lock:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(RESEARCH_STATE_PATH)


def research_worker():
    state = read_research_state()
    if state.get("status") == "completed":
        return

    state = research_default_state()
    state["status"] = "running"
    state["stage"] = "screening"
    state["started_at"] = now_ts()
    write_research_state(state)

    candidates = []
    for idx, target in enumerate(RESEARCH_TARGETS, start=1):
        state["current"] = {
            "phase": "screening",
            "index": idx,
            "total": len(RESEARCH_TARGETS),
            "repo": target["repo"].replace("https://github.com/", ""),
            "label": target["label"],
        }
        write_research_state(state)
        try:
            result = quick_npm_probe(
                repo_url=target["repo"],
                branch=target["branch"],
                project_path=target["project_path"],
                work_root=WORK_DIR,
                runtime_root=RUNTIME_DIR,
                node_majors=[22],
                npm_versions=["10.9.9", "11.19.0"],
            )
            item = {
                "repo": result.get("repo"),
                "label": target["label"],
                "classification": result.get("classification"),
                "status": result.get("status"),
                "repo_size_mb": result.get("repo_size_mb"),
                "project_path": result.get("project_path"),
                "matrix": result.get("matrix", []),
            }
            if result.get("classification") == "npm11_break_candidate":
                candidates.append(target)
        except Exception as exc:
            item = {
                "repo": target["repo"].replace("https://github.com/", ""),
                "label": target["label"],
                "classification": "probe_error",
                "status": "error",
                "error": str(exc),
                "matrix": [],
            }

        state["results"].append(item)
        state["probe_completed"] = idx
        state["candidates"] = len(candidates)
        write_research_state(state)

    state["stage"] = "confirmation"
    write_research_state(state)

    # Confirm up to four npm-11 break candidates across three Node majors.
    # Confirmation still uses dry-run + ignore-scripts so arbitrary repo scripts never execute.
    for idx, target in enumerate(candidates[:4], start=1):
        state["current"] = {
            "phase": "confirmation",
            "index": idx,
            "total": min(4, len(candidates)),
            "repo": target["repo"].replace("https://github.com/", ""),
            "label": target["label"],
        }
        write_research_state(state)
        try:
            result = quick_npm_probe(
                repo_url=target["repo"],
                branch=target["branch"],
                project_path=target["project_path"],
                work_root=WORK_DIR,
                runtime_root=RUNTIME_DIR,
                node_majors=[22, 24, 26],
                npm_versions=["10.9.9", "11.19.0"],
            )
            rows = result.get("matrix", [])
            npm10 = [r for r in rows if r.get("npm_requested") == "10.9.9"]
            npm11 = [r for r in rows if r.get("npm_requested") == "11.19.0"]
            confirmed = (
                len(npm10) == 3 and len(npm11) == 3
                and all(r.get("status") == "pass" for r in npm10)
                and all(r.get("status") != "pass" for r in npm11)
            )
            confirmation = {
                "repo": result.get("repo"),
                "confirmed": confirmed,
                "classification": result.get("classification"),
                "matrix": rows,
            }
        except Exception as exc:
            confirmation = {
                "repo": target["repo"].replace("https://github.com/", ""),
                "confirmed": False,
                "classification": "confirmation_error",
                "error": str(exc),
                "matrix": [],
            }

        state["confirmations"].append(confirmation)
        state["confirm_completed"] = idx
        state["confirmed"] = sum(1 for x in state["confirmations"] if x.get("confirmed"))
        write_research_state(state)

    # Turn the compatibility finding into a concrete deliverable: a verified
    # package-lock-only patch. This is the first thing we can plausibly sell.
    state["stage"] = "repair_validation"
    write_research_state(state)

    confirmed_repos = {x.get("repo") for x in state["confirmations"] if x.get("confirmed")}
    repair_targets = [t for t in candidates[:4] if t["repo"].replace("https://github.com/", "") in confirmed_repos]
    patch_dir = DATA_DIR / "research_patches" / RESEARCH_BATCH_ID
    patch_dir.mkdir(parents=True, exist_ok=True)

    for idx, target in enumerate(repair_targets, start=1):
        slug = target["repo"].replace("https://github.com/", "")
        state["current"] = {
            "phase": "repair_validation",
            "index": idx,
            "total": len(repair_targets),
            "repo": slug,
            "label": target["label"],
        }
        write_research_state(state)

        try:
            repaired = repair_npm11_lockfile(
                repo_url=target["repo"],
                branch=target["branch"],
                project_path=target["project_path"],
                work_root=WORK_DIR,
                runtime_root=RUNTIME_DIR,
                node_major=22,
                npm_old="10.9.9",
                npm_new="11.19.0",
            )
            patch_name = None
            if repaired.get("verified") and repaired.get("patch"):
                patch_name = slug.replace("/", "__") + ".patch"
                (patch_dir / patch_name).write_text(repaired["patch"], encoding="utf-8")
            repair_item = {
                "repo": slug,
                "status": repaired.get("status"),
                "verified": bool(repaired.get("verified")),
                "reason": repaired.get("reason"),
                "changed_paths": repaired.get("changed_paths", []),
                "patch_bytes": repaired.get("patch_bytes", 0),
                "additions": repaired.get("additions", 0),
                "deletions": repaired.get("deletions", 0),
                "before": repaired.get("before"),
                "after": repaired.get("after"),
                "patch_file": patch_name,
            }
        except Exception as exc:
            repair_item = {
                "repo": slug,
                "status": "error",
                "verified": False,
                "error": str(exc),
                "patch_file": None,
            }

        state["repairs"].append(repair_item)
        state["repair_completed"] = idx
        state["repair_verified"] = sum(1 for x in state["repairs"] if x.get("verified"))
        write_research_state(state)

    state["current"] = None
    state["status"] = "completed"
    state["stage"] = "done"
    state["finished_at"] = now_ts()

    if state["repair_verified"] >= 3:
        state["monetization_gate"] = "PASSED: multiple independent npm 11 failures reproduced AND automatically repaired with verified package-lock-only patches. Product candidate: $1 verified npm 11 lockfile repair."
    elif state["confirmed"] >= 3:
        state["monetization_gate"] = "PARTIAL: failures reproduce, but automatic repair is not reliable enough to sell yet."
    elif state["confirmed"] >= 1:
        state["monetization_gate"] = "PARTIAL: reproducible failures exist, but sample is too small for a paid claim."
    else:
        state["monetization_gate"] = "FAILED: this batch did not reproduce enough independent npm 10→11 breaks. Change hypothesis."
    write_research_state(state)


def start_research_thread_once():
    global research_thread_started
    if research_thread_started:
        return
    research_thread_started = True

    # Persist the new batch immediately before the worker starts. This makes
    # deployment/startup observable even if the first request arrives during
    # Railway health checks or a previous persisted batch exists on /data.
    state = read_research_state()
    if state.get("status") != "completed":
        seed = research_default_state()
        seed["status"] = "running"
        seed["stage"] = "starting"
        seed["started_at"] = now_ts()
        write_research_state(seed)

    thread = threading.Thread(target=research_worker, name="wid-research", daemon=True)
    thread.start()


def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                repo_url TEXT NOT NULL,
                branch TEXT,
                project_path TEXT,
                mode TEXT NOT NULL,
                runtimes TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT,
                result_json TEXT,
                error TEXT
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scans)").fetchall()}
        if "project_path" not in cols:
            conn.execute("ALTER TABLE scans ADD COLUMN project_path TEXT")
        conn.commit()


init_db()
try:
    os.chmod(DB_PATH, 0o600)
except OSError:
    pass


def now_ts() -> int:
    return int(time.time())


def require_token():
    expected = os.getenv("SCAN_TOKEN", "").strip()
    if not expected:
        return None
    supplied = (request.headers.get("X-Scan-Token") or "").strip()
    if supplied != expected:
        return jsonify({"error": "Invalid or missing scan token"}), 401
    return None


def update_scan(scan_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = now_ts()
    with write_lock, db_conn() as conn:
        parts = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [scan_id]
        conn.execute(f"UPDATE scans SET {parts} WHERE id = ?", values)
        conn.commit()


def worker(scan_id: str, repo_url: str, branch: str | None, project_path: str | None, mode: str, runtimes: list[int]):
    try:
        update_scan(scan_id, status="running")
        allow_full = os.getenv("ALLOW_FULL_BUILDS", "0") == "1"
        if mode == "full" and not allow_full:
            raise RuntimeError("Full builds are disabled on this server. Set ALLOW_FULL_BUILDS=1 to enable them.")

        npm_versions = ["10.9.9", "11.19.0"] if mode == "full" else ["bundled"]
        result = scan_repo(
            repo_url=repo_url,
            branch=branch or None,
            mode=mode,
            node_majors=runtimes,
            work_root=WORK_DIR,
            runtime_root=RUNTIME_DIR,
            project_path=project_path,
            npm_versions=npm_versions,
        )
        update_scan(
            scan_id,
            status="completed",
            summary=result.get("summary", ""),
            result_json=json.dumps(result),
        )
    except Exception as exc:
        update_scan(scan_id, status="failed", error=str(exc))


@app.before_request
def ensure_research_started():
    start_research_thread_once()


@app.get("/api/research/status")
def research_status():
    state = read_research_state()
    completed = int(state.get("probe_completed", 0)) + int(state.get("confirm_completed", 0)) + int(state.get("repair_completed", 0))
    repair_target_count = min(4, int(state.get("confirmed", 0)))
    total = int(state.get("targets_total", 0)) + min(4, int(state.get("candidates", 0))) + repair_target_count
    state["progress_completed"] = completed
    state["progress_total"] = max(total, int(state.get("targets_total", 0)))
    state["progress_percent"] = round((completed / state["progress_total"] * 100), 1) if state["progress_total"] else 0
    return jsonify(state)


@app.get("/")
def index():
    return render_template(
        "index.html",
        version=APP_VERSION,
        full_builds_enabled=os.getenv("ALLOW_FULL_BUILDS", "0") == "1",
        token_required=bool(os.getenv("SCAN_TOKEN", "").strip()),
    )


@app.get("/api/health")
def health():
    tools = {name: bool(shutil.which(name)) for name in ("git", "make", "g++", "python3")}
    return jsonify(
        {
            "ok": True,
            "app": "WillItDeploy",
            "version": APP_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "data_dir": str(DATA_DIR),
            "temp_storage_dir": str(TEMP_STORAGE_DIR),
            "runtime_dir": str(RUNTIME_DIR),
            "temp_free_bytes": shutil.disk_usage(TEMP_STORAGE_DIR).free,
            "full_builds_enabled": os.getenv("ALLOW_FULL_BUILDS", "0") == "1",
            "token_required": bool(os.getenv("SCAN_TOKEN", "").strip()),
            "tools": tools,
        }
    )


@app.post("/api/scans")
def create_scan():
    auth = require_token()
    if auth:
        return auth

    payload = request.get_json(silent=True) or {}
    repo_url = str(payload.get("repo_url") or "").strip()

    def optional_text(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned or cleaned.lower() in {"none", "null"}:
            return None
        return cleaned

    branch = optional_text(payload.get("branch"))
    mode = str(payload.get("mode") or "static").strip().lower()
    project_path = optional_text(payload.get("project_path"))
    runtimes = payload.get("runtimes", [22, 24, 26])

    if mode not in {"static", "full"}:
        return jsonify({"error": "mode must be static or full"}), 400
    if not repo_url.startswith("https://github.com/"):
        return jsonify({"error": "Only public https://github.com/owner/repo URLs are accepted"}), 400

    normalized = []
    for value in runtimes:
        try:
            major = int(value)
        except (TypeError, ValueError):
            continue
        if 18 <= major <= 30 and major not in normalized:
            normalized.append(major)
    if not normalized:
        return jsonify({"error": "Choose at least one Node major version"}), 400
    if len(normalized) > 4:
        return jsonify({"error": "Maximum 4 runtime versions per scan"}), 400

    scan_id = uuid.uuid4().hex[:12]
    ts = now_ts()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO scans (id, created_at, updated_at, repo_url, branch, project_path, mode, runtimes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, ts, ts, repo_url, branch, project_path, mode, json.dumps(normalized), "queued"),
        )
        conn.commit()

    executor.submit(worker, scan_id, repo_url, branch, project_path, mode, normalized)
    return jsonify({"id": scan_id, "status": "queued"}), 202


@app.post("/api/self-test")
def self_test():
    auth = require_token()
    if auth:
        return auth
    if os.getenv("ALLOW_FULL_BUILDS", "0") != "1":
        return jsonify({"error": "Set ALLOW_FULL_BUILDS=1 to run the bundled full-build self-test."}), 400

    try:
        result = scan_fixture(
            fixture_dir=BASE_DIR / "fixtures" / "hello-node",
            node_majors=[22, 24, 26],
            work_root=WORK_DIR,
            runtime_root=RUNTIME_DIR,
        )
        result["regression_checks"] = regression_checks(BASE_DIR / "fixtures")
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/scans")
def list_scans():
    limit = min(100, max(1, int(request.args.get("limit", "30"))))
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, updated_at, repo_url, branch, project_path, mode, runtimes, status, summary, error FROM scans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify([dict(row) | {"runtimes": json.loads(row["runtimes"])} for row in rows])


@app.get("/api/scans/<scan_id>")
def get_scan(scan_id: str):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if not row:
        return jsonify({"error": "Scan not found"}), 404
    data = dict(row)
    data["runtimes"] = json.loads(data["runtimes"])
    if data.get("result_json"):
        data["result"] = json.loads(data["result_json"])
    data.pop("result_json", None)
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
