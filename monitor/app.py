import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

APP_VERSION = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pricepulse.sqlite3"
ADMIN_TOKEN = os.getenv("MONITOR_TOKEN", "")
CHECK_TICK_SECONDS = max(10, int(os.getenv("CHECK_TICK_SECONDS", "30")))
DEFAULT_INTERVAL_MIN = max(1, int(os.getenv("DEFAULT_INTERVAL_MIN", "5")))
MAX_BODY_BYTES = 2_000_000
USER_AGENT = os.getenv(
    "FETCH_USER_AGENT",
    "PricePulse/0.1 (+https://github.com/VERSEHARD/willitdeploy)"
)

app = Flask(__name__)
scheduler_started = False
scheduler_lock = threading.Lock()


def now_ts():
    return int(time.time())


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            selector TEXT,
            must_contain TEXT,
            price_regex TEXT,
            max_price REAL,
            interval_min INTEGER NOT NULL DEFAULT 5,
            webhook_url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            last_checked INTEGER,
            next_check INTEGER,
            last_http_status INTEGER,
            last_hash TEXT,
            last_excerpt TEXT,
            last_price REAL,
            last_error TEXT,
            change_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail TEXT,
            FOREIGN KEY(monitor_id) REFERENCES monitors(id)
        );
        """)


def serialize_monitor(row):
    d = dict(row)
    for key in ("created_at", "last_checked", "next_check"):
        d[key + "_iso"] = iso(d.get(key))
    d["enabled"] = bool(d["enabled"])
    return d


def private_or_local(host):
    if not host:
        return True
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Hostname could not be resolved.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def validate_public_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed.")
    if not parsed.hostname or private_or_local(parsed.hostname):
        raise ValueError("Private/local network URLs are not allowed.")
    return value


def fetch_page(url):
    validate_public_url(url)
    r = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=(5, 15),
        allow_redirects=True,
        stream=True,
    )
    validate_public_url(r.url)
    chunks = []
    total = 0
    for chunk in r.iter_content(65536):
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ValueError("Page exceeded the 2 MB fetch limit.")
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type = (r.headers.get("content-type") or "").lower()
    if "html" not in content_type and "text" not in content_type:
        raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
    text = body.decode(r.encoding or "utf-8", errors="replace")
    return r.status_code, r.url, text


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_content(html, selector):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    if selector:
        node = soup.select_one(selector)
        if node is None:
            raise ValueError(f"CSS selector did not match: {selector}")
        return normalize_text(node.get_text(" ", strip=True))
    return normalize_text(soup.get_text(" ", strip=True))


def extract_price(text, pattern):
    if not pattern:
        return None
    try:
        m = re.search(pattern, text, flags=re.I)
    except re.error as exc:
        raise ValueError(f"Invalid price regex: {exc}")
    if not m:
        return None
    raw = m.group(1) if m.lastindex else m.group(0)
    raw = re.sub(r"[^0-9.\-]", "", raw.replace(",", ""))
    try:
        return float(raw)
    except ValueError:
        return None


def emit_event(conn, monitor_id, event_type, summary, detail=None):
    conn.execute(
        "INSERT INTO events(monitor_id,created_at,event_type,summary,detail) VALUES(?,?,?,?,?)",
        (monitor_id, now_ts(), event_type, summary, detail),
    )


def send_webhook(url, payload):
    if not url:
        return None
    try:
        validate_public_url(url)
        r = requests.post(url, json=payload, timeout=(5, 10), allow_redirects=False)
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_monitor(monitor_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Monitor not found."}
        m = dict(row)

    checked = now_ts()
    try:
        status, final_url, html = fetch_page(m["url"])
        content = extract_content(html, m["selector"])
        excerpt = content[:500]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        price = extract_price(content, m["price_regex"])
        keyword_ok = True if not m["must_contain"] else m["must_contain"].lower() in content.lower()
        threshold_ok = True if m["max_price"] is None else (price is not None and price <= float(m["max_price"]))

        changed = bool(m["last_hash"]) and digest != m["last_hash"]
        threshold_crossed = (
            m["max_price"] is not None
            and price is not None
            and price <= float(m["max_price"])
            and (m["last_price"] is None or float(m["last_price"]) > float(m["max_price"]))
        )
        keyword_became_true = (
            bool(m["must_contain"])
            and keyword_ok
            and m["last_excerpt"] is not None
            and m["must_contain"].lower() not in (m["last_excerpt"] or "").lower()
        )

        event_types = []
        if changed:
            event_types.append("content_changed")
        if threshold_crossed:
            event_types.append("price_threshold")
        if keyword_became_true:
            event_types.append("keyword_match")

        next_check = checked + int(m["interval_min"]) * 60
        with db() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_checked=?,next_check=?,last_http_status=?,last_hash=?,last_excerpt=?,
                       last_price=?,last_error=NULL,change_count=change_count+?
                   WHERE id=?""",
                (
                    checked,
                    next_check,
                    status,
                    digest,
                    excerpt,
                    price,
                    1 if event_types else 0,
                    monitor_id,
                ),
            )

            if not m["last_hash"]:
                emit_event(conn, monitor_id, "baseline", "Baseline captured.", final_url)
            for event_type in event_types:
                summary = {
                    "content_changed": "Page content changed.",
                    "price_threshold": f"Price reached target: {price}",
                    "keyword_match": f"Keyword appeared: {m['must_contain']}",
                }[event_type]
                emit_event(conn, monitor_id, event_type, summary, excerpt)

        if event_types and m["webhook_url"]:
            send_webhook(
                m["webhook_url"],
                {
                    "service": "PricePulse",
                    "monitor_id": monitor_id,
                    "name": m["name"],
                    "url": m["url"],
                    "events": event_types,
                    "price": price,
                    "max_price": m["max_price"],
                    "keyword_ok": keyword_ok,
                    "threshold_ok": threshold_ok,
                    "excerpt": excerpt,
                    "checked_at": iso(checked),
                },
            )

        return {
            "ok": True,
            "status": status,
            "changed": changed,
            "events": event_types,
            "price": price,
            "keyword_ok": keyword_ok,
            "threshold_ok": threshold_ok,
        }

    except Exception as exc:
        next_check = checked + int(m["interval_min"]) * 60
        with db() as conn:
            conn.execute(
                "UPDATE monitors SET last_checked=?,next_check=?,last_error=? WHERE id=?",
                (checked, next_check, str(exc), monitor_id),
            )
            emit_event(conn, monitor_id, "error", "Check failed.", str(exc))
        return {"ok": False, "error": str(exc)}


def scheduler_loop():
    while True:
        try:
            due = []
            with db() as conn:
                rows = conn.execute(
                    """SELECT id FROM monitors
                       WHERE enabled=1 AND (next_check IS NULL OR next_check<=?)
                       ORDER BY COALESCE(next_check,0) ASC LIMIT 20""",
                    (now_ts(),),
                ).fetchall()
                due = [r["id"] for r in rows]
            for monitor_id in due:
                run_monitor(monitor_id)
        except Exception:
            pass
        time.sleep(CHECK_TICK_SECONDS)


def ensure_scheduler():
    global scheduler_started
    with scheduler_lock:
        if scheduler_started:
            return
        scheduler_started = True
        threading.Thread(target=scheduler_loop, daemon=True, name="pricepulse-scheduler").start()


def require_admin():
    if not ADMIN_TOKEN:
        return None
    token = request.headers.get("X-Monitor-Token", "")
    if token != ADMIN_TOKEN:
        return (jsonify({"error": "Unauthorized."}), 401)
    return None


@app.before_request
def boot():
    ensure_scheduler()


@app.get("/")
def index():
    return render_template(
        "index.html",
        version=APP_VERSION,
        token_required=bool(ADMIN_TOKEN),
        default_interval=DEFAULT_INTERVAL_MIN,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "version": APP_VERSION, "db": str(DB_PATH)})


@app.get("/api/monitors")
def list_monitors():
    with db() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
    return jsonify([serialize_monitor(r) for r in rows])


@app.get("/api/events")
def list_events():
    limit = max(1, min(100, int(request.args.get("limit", "30"))))
    with db() as conn:
        rows = conn.execute(
            """SELECT e.*,m.name AS monitor_name,m.url
               FROM events e JOIN monitors m ON m.id=e.monitor_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["created_at_iso"] = iso(d["created_at"])
        out.append(d)
    return jsonify(out)


@app.post("/api/monitors")
def create_monitor():
    denied = require_admin()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        name = str(payload.get("name") or "").strip()
        url = validate_public_url(str(payload.get("url") or "").strip())
        selector = str(payload.get("selector") or "").strip() or None
        must_contain = str(payload.get("must_contain") or "").strip() or None
        price_regex = str(payload.get("price_regex") or "").strip() or None
        max_price = payload.get("max_price")
        max_price = float(max_price) if max_price not in (None, "") else None
        interval_min = max(1, min(1440, int(payload.get("interval_min") or DEFAULT_INTERVAL_MIN)))
        webhook_url = str(payload.get("webhook_url") or "").strip() or None
        if webhook_url:
            validate_public_url(webhook_url)
        if not name:
            raise ValueError("Name is required.")
        if price_regex:
            re.compile(price_regex)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO monitors(
                name,url,selector,must_contain,price_regex,max_price,interval_min,
                webhook_url,enabled,created_at,next_check
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
            (
                name,url,selector,must_contain,price_regex,max_price,interval_min,
                webhook_url,now_ts(),now_ts()
            ),
        )
        monitor_id = cur.lastrowid
        row = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    return jsonify(serialize_monitor(row)), 201


@app.post("/api/monitors/<int:monitor_id>/run")
def run_now(monitor_id):
    denied = require_admin()
    if denied:
        return denied
    return jsonify(run_monitor(monitor_id))


@app.post("/api/monitors/<int:monitor_id>/toggle")
def toggle_monitor(monitor_id):
    denied = require_admin()
    if denied:
        return denied
    with db() as conn:
        row = conn.execute("SELECT enabled FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not row:
            return jsonify({"error": "Monitor not found."}), 404
        enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE monitors SET enabled=?,next_check=? WHERE id=?", (enabled, now_ts(), monitor_id))
    return jsonify({"ok": True, "enabled": bool(enabled)})


@app.delete("/api/monitors/<int:monitor_id>")
def delete_monitor(monitor_id):
    denied = require_admin()
    if denied:
        return denied
    with db() as conn:
        conn.execute("DELETE FROM events WHERE monitor_id=?", (monitor_id,))
        cur = conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        if cur.rowcount == 0:
            return jsonify({"error": "Monitor not found."}), 404
    return jsonify({"ok": True})


init_db()
