import json
import os
import platform
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from scanner import scan_repo, scan_fixture

APP_VERSION = "0.1.0"
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
WORK_DIR = DATA_DIR / "work"
RUNTIME_DIR = DATA_DIR / "runtimes"
WORK_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("SCAN_WORKERS", "1"))))
write_lock = threading.Lock()


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
                mode TEXT NOT NULL,
                runtimes TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT,
                result_json TEXT,
                error TEXT
            )
            """
        )
        conn.commit()


init_db()


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


def worker(scan_id: str, repo_url: str, branch: str | None, mode: str, runtimes: list[int]):
    try:
        update_scan(scan_id, status="running")
        allow_full = os.getenv("ALLOW_FULL_BUILDS", "0") == "1"
        if mode == "full" and not allow_full:
            raise RuntimeError("Full builds are disabled on this server. Set ALLOW_FULL_BUILDS=1 to enable them.")

        result = scan_repo(
            repo_url=repo_url,
            branch=branch or None,
            mode=mode,
            node_majors=runtimes,
            work_root=WORK_DIR,
            runtime_root=RUNTIME_DIR,
        )
        update_scan(
            scan_id,
            status="completed",
            summary=result.get("summary", ""),
            result_json=json.dumps(result),
        )
    except Exception as exc:
        update_scan(scan_id, status="failed", error=str(exc))


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
    repo_url = str(payload.get("repo_url", "")).strip()
    branch = str(payload.get("branch", "")).strip() or None
    mode = str(payload.get("mode", "static")).strip().lower()
    runtimes = payload.get("runtimes", [22, 24, 26])

    if mode not in {"static", "full"}:
        return jsonify({"error": "mode must be static or full"}), 400
    if not repo_url.startswith("https://github.com/"):
        return jsonify({"error": "v0.1 only accepts public https://github.com/owner/repo URLs"}), 400

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
            "INSERT INTO scans (id, created_at, updated_at, repo_url, branch, mode, runtimes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, ts, ts, repo_url, branch, mode, json.dumps(normalized), "queued"),
        )
        conn.commit()

    executor.submit(worker, scan_id, repo_url, branch, mode, normalized)
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
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/scans")
def list_scans():
    limit = min(100, max(1, int(request.args.get("limit", "30"))))
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, updated_at, repo_url, branch, mode, runtimes, status, summary, error FROM scans ORDER BY created_at DESC LIMIT ?",
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
