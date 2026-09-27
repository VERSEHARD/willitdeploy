import hashlib
import difflib
import statistics
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

APP_VERSION = "0.2.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pricepulse.sqlite3"
ADMIN_TOKEN = os.getenv("MONITOR_TOKEN", "")
CHECK_TICK_SECONDS = max(10, int(os.getenv("CHECK_TICK_SECONDS", "30")))
DEFAULT_INTERVAL_MIN = max(1, int(os.getenv("DEFAULT_INTERVAL_MIN", "5")))
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
MAX_BODY_BYTES = 2_000_000
USER_AGENT = os.getenv(
    "FETCH_USER_AGENT",
    "PricePulse/0.1 (+https://github.com/VERSEHARD/willitdeploy)"
)

app = Flask(__name__)
scheduler_started = False
scheduler_lock = threading.Lock()
lab_last_run = 0
LAB_INTERVAL_SECONDS = max(900, int(os.getenv("LAB_INTERVAL_SECONDS", "21600")))


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


def migrate_schema():
    with db() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(monitors)").fetchall()}
        additions = {
            "kind": "TEXT DEFAULT 'content'",
            "ignore_regex": "TEXT",
            "currency": "TEXT",
            "checks_count": "INTEGER NOT NULL DEFAULT 0",
            "success_count": "INTEGER NOT NULL DEFAULT 0",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_latency_ms": "INTEGER",
            "avg_latency_ms": "REAL",
            "last_change_at": "INTEGER",
            "is_demo": "INTEGER NOT NULL DEFAULT 0"
        }
        for name, ddl in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE monitors ADD COLUMN {name} {ddl}")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            http_status INTEGER,
            content_hash TEXT,
            excerpt TEXT,
            price REAL,
            latency_ms INTEGER,
            changed INTEGER NOT NULL DEFAULT 0,
            triggers TEXT,
            FOREIGN KEY(monitor_id) REFERENCES monitors(id)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_monitor_created
            ON snapshots(monitor_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS lab_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            http_status INTEGER,
            latency_ms INTEGER,
            content_bytes INTEGER,
            signal TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lab_runs_created
            ON lab_runs(created_at DESC);
        """)


def serialize_monitor(row):
    d = dict(row)
    for key in ("created_at", "last_checked", "next_check", "last_change_at"):
        d[key + "_iso"] = iso(d.get(key))
    d["enabled"] = bool(d["enabled"])
    d["is_demo"] = bool(d.get("is_demo"))
    checks = int(d.get("checks_count") or 0)
    success = int(d.get("success_count") or 0)
    d["success_rate"] = round(success / checks * 100, 1) if checks else None
    if d.get("last_error"):
        d["health"] = "error"
    elif d.get("last_checked"):
        d["health"] = "healthy"
    else:
        d["health"] = "pending"
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
    if not any(kind in content_type for kind in ("html", "text", "json", "xml", "rss", "atom")):
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


def extract_price(text, pattern=None):
    """Extract a numeric price. Explicit regex wins; otherwise use conservative currency heuristics."""
    if pattern:
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

    patterns = [
        r"(?:USD|US\$|\$)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        r"(?:GBP|£)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        r"(?:EUR|€)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        r"(?:INR|₹)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
    ]
    for candidate in patterns:
        m = re.search(candidate, text, flags=re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def detect_currency(text):
    for symbol, code in (("₹", "INR"), ("£", "GBP"), ("€", "EUR"), ("$", "USD")):
        if symbol in text:
            return code
    for code in ("USD", "GBP", "EUR", "INR"):
        if re.search(rf"\b{code}\b", text, flags=re.I):
            return code
    return None


def apply_ignore_regex(text, pattern):
    if not pattern:
        return text
    try:
        return re.sub(pattern, "", text, flags=re.I)
    except re.error as exc:
        raise ValueError(f"Invalid ignore regex: {exc}")


def text_diff(before, after, max_lines=8):
    if not before:
        return []
    old = before.split()
    new = after.split()
    diff = list(difflib.unified_diff(old, new, lineterm=""))
    cleaned = [line for line in diff if line and not line.startswith(("---", "+++", "@@"))]
    return cleaned[:max_lines]




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
        host = (urlparse(url).hostname or "").lower()
        events = ", ".join(payload.get("events") or [])
        price = payload.get("price")
        currency = payload.get("currency") or ""
        value = f"{currency} {price}".strip() if price is not None else "n/a"
        message = (
            f"PricePulse · {payload.get('name')}\n"
            f"Signal: {events or 'change'}\n"
            f"Value: {value}\n"
            f"{payload.get('url')}"
        )

        if "discord.com" in host or "discordapp.com" in host:
            body = {"content": message[:1900]}
        elif "hooks.slack.com" in host:
            body = {"text": message[:3500]}
        else:
            body = payload

        r = requests.post(
            url,
            json=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=(5, 10),
            allow_redirects=False,
        )
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
    started = time.perf_counter()
    try:
        status, final_url, html = fetch_page(m["url"])
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = extract_content(html, m["selector"])
        content = apply_ignore_regex(content, m.get("ignore_regex"))
        excerpt = content[:1000]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        price = extract_price(content, m["price_regex"])
        currency = m.get("currency") or detect_currency(content)
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
        keyword_became_false = (
            bool(m["must_contain"])
            and not keyword_ok
            and m["last_excerpt"] is not None
            and m["must_contain"].lower() in (m["last_excerpt"] or "").lower()
        )
        price_changed = (
            price is not None
            and m["last_price"] is not None
            and abs(float(price) - float(m["last_price"])) > 0.000001
        )

        event_types = []
        if changed:
            event_types.append("content_changed")
        if price_changed:
            event_types.append("price_changed")
        if threshold_crossed:
            event_types.append("price_threshold")
        if keyword_became_true:
            event_types.append("keyword_match")
        if keyword_became_false:
            event_types.append("keyword_lost")

        next_check = checked + int(m["interval_min"]) * 60
        checks_count = int(m.get("checks_count") or 0) + 1
        success_count = int(m.get("success_count") or 0) + 1
        prev_avg = float(m.get("avg_latency_ms") or 0)
        avg_latency = round(((prev_avg * (checks_count - 1)) + latency_ms) / checks_count, 1)

        diff_lines = text_diff(m.get("last_excerpt"), excerpt) if changed else []
        detail_payload = {
            "excerpt": excerpt[:500],
            "diff": diff_lines,
            "price": price,
            "currency": currency,
            "url": final_url,
        }

        with db() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_checked=?,next_check=?,last_http_status=?,last_hash=?,last_excerpt=?,
                       last_price=?,last_error=NULL,change_count=change_count+?,
                       checks_count=?,success_count=?,last_latency_ms=?,avg_latency_ms=?,
                       last_change_at=CASE WHEN ?=1 THEN ? ELSE last_change_at END,
                       currency=COALESCE(currency,?)
                   WHERE id=?""",
                (
                    checked,
                    next_check,
                    status,
                    digest,
                    excerpt,
                    price,
                    1 if event_types else 0,
                    checks_count,
                    success_count,
                    latency_ms,
                    avg_latency,
                    1 if event_types else 0,
                    checked,
                    currency,
                    monitor_id,
                ),
            )

            conn.execute(
                """INSERT INTO snapshots(
                    monitor_id,created_at,http_status,content_hash,excerpt,price,latency_ms,changed,triggers
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    monitor_id,
                    checked,
                    status,
                    digest,
                    excerpt,
                    price,
                    latency_ms,
                    1 if changed else 0,
                    json.dumps(event_types),
                ),
            )
            conn.execute(
                """DELETE FROM snapshots
                   WHERE monitor_id=? AND id NOT IN (
                       SELECT id FROM snapshots WHERE monitor_id=? ORDER BY created_at DESC LIMIT 50
                   )""",
                (monitor_id, monitor_id),
            )

            if not m["last_hash"]:
                emit_event(conn, monitor_id, "baseline", "Baseline captured.", json.dumps(detail_payload))
            for event_type in event_types:
                old_price = float(m["last_price"]) if m["last_price"] is not None else None
                delta_pct = ((float(price) - old_price) / old_price * 100) if price_changed and old_price else None
                summary = {
                    "content_changed": "Tracked content changed.",
                    "price_changed": (
                        f"Price changed from {currency + ' ' if currency else ''}{old_price:g} "
                        f"to {currency + ' ' if currency else ''}{float(price):g}"
                        + (f" ({delta_pct:+.1f}%)" if delta_pct is not None else "")
                    ),
                    "price_threshold": f"Price reached target: {currency + ' ' if currency else ''}{price}",
                    "keyword_match": f"Keyword appeared: {m['must_contain']}",
                    "keyword_lost": f"Keyword disappeared: {m['must_contain']}",
                }[event_type]
                emit_event(conn, monitor_id, event_type, summary, json.dumps(detail_payload))

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
                    "currency": currency,
                    "max_price": m["max_price"],
                    "keyword_ok": keyword_ok,
                    "threshold_ok": threshold_ok,
                    "excerpt": excerpt[:500],
                    "diff": diff_lines,
                    "checked_at": iso(checked),
                },
            )

        result = {
            "ok": True,
            "status": status,
            "changed": changed,
            "events": event_types,
            "price": price,
            "currency": currency,
            "keyword_ok": keyword_ok,
            "threshold_ok": threshold_ok,
            "latency_ms": latency_ms,
            "diff": diff_lines,
        }
        print("[PricePulse] check", json.dumps({"id": monitor_id, **result}, sort_keys=True), flush=True)
        return result

    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        next_check = checked + int(m["interval_min"]) * 60
        checks_count = int(m.get("checks_count") or 0) + 1
        failure_count = int(m.get("failure_count") or 0) + 1
        with db() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_checked=?,next_check=?,last_error=?,checks_count=?,failure_count=?,
                       last_latency_ms=?
                   WHERE id=?""",
                (checked, next_check, str(exc), checks_count, failure_count, latency_ms, monitor_id),
            )
            emit_event(conn, monitor_id, "error", "Check failed.", str(exc))
        result = {"ok": False, "error": str(exc), "latency_ms": latency_ms}
        print("[PricePulse] check", json.dumps({"id": monitor_id, **result}, sort_keys=True), flush=True)
        return result


def run_reliability_lab():
    global lab_last_run
    targets = [
        {
            "target": "Static product page",
            "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
            "expect": "£51.77",
        },
        {
            "target": "Distill pricing",
            "url": "https://distill.io/pricing/",
            "expect": "Starter",
        },
        {
            "target": "ChangeTower pricing",
            "url": "https://changetower.com/pricing/",
            "expect": "Business",
        },
        {
            "target": "Browse AI pricing",
            "url": "https://www.browse.ai/pricing",
            "expect": "Professional",
        },
    ]
    for target in targets:
        started = time.perf_counter()
        try:
            status, final_url, html = fetch_page(target["url"])
            latency_ms = int((time.perf_counter() - started) * 1000)
            text = extract_content(html, None)
            ok = status == 200 and target["expect"].lower() in text.lower()
            signal = target["expect"] if ok else None
            error = None if ok else f"Expected signal not found: {target['expect']}"
        except Exception as exc:
            status = None
            latency_ms = int((time.perf_counter() - started) * 1000)
            html = ""
            ok = False
            signal = None
            error = str(exc)

        print("[PricePulse] lab", json.dumps({
            "target": target["target"],
            "ok": ok,
            "http_status": status,
            "latency_ms": latency_ms,
            "signal": signal,
            "error": error,
        }, sort_keys=True), flush=True)

        with db() as conn:
            conn.execute(
                """INSERT INTO lab_runs(target,url,created_at,ok,http_status,latency_ms,content_bytes,signal,error)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    target["target"],
                    target["url"],
                    now_ts(),
                    1 if ok else 0,
                    status,
                    latency_ms,
                    len(html.encode("utf-8")),
                    signal,
                    error,
                ),
            )
            conn.execute(
                """DELETE FROM lab_runs WHERE id NOT IN (
                    SELECT id FROM lab_runs ORDER BY created_at DESC LIMIT 100
                )"""
            )
    lab_last_run = now_ts()
    print("[PricePulse] reliability-lab complete", flush=True)


def scheduler_loop():
    global lab_last_run
    while True:
        try:
            if now_ts() - int(lab_last_run or 0) >= LAB_INTERVAL_SECONDS:
                run_reliability_lab()
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



def startup_self_test():
    fixture = """
    <html><body><div class="product_main">
      <p class="price_color">£51.77</p><p>In stock</p>
    </div></body></html>
    """
    content = extract_content(fixture, ".product_main")
    price = extract_price(content, r"£([0-9.]+)")
    assert price is not None and abs(price - 51.77) < 0.001
    assert "In stock" in content
    try:
        validate_public_url("http://127.0.0.1/internal")
        raise AssertionError("SSRF guard failed")
    except ValueError:
        pass
    print("[PricePulse] startup-self-test PASS", flush=True)


def seed_demo_monitor():
    if not SEED_DEMO:
        return
    with db() as conn:
        row = conn.execute("SELECT id FROM monitors WHERE name=?", ("Books demo under 60",)).fetchone()
        if row:
            return
        conn.execute(
            """INSERT INTO monitors(
                name,url,selector,must_contain,price_regex,max_price,interval_min,
                webhook_url,enabled,created_at,next_check
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
            (
                "Books demo under 60",
                "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
                ".product_main",
                "In stock",
                r"£([0-9.]+)",
                60.0,
                60,
                None,
                now_ts(),
                now_ts(),
            ),
        )
    print("[PricePulse] demo monitor seeded", flush=True)


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
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "db": str(DB_PATH),
        "scheduler": scheduler_started,
        "lab_interval_seconds": LAB_INTERVAL_SECONDS,
        "last_lab_run_iso": iso(lab_last_run),
    })


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


@app.get("/api/stats")
def stats():
    cutoff_24h = now_ts() - 86400
    with db() as conn:
        monitor_rows = conn.execute("SELECT * FROM monitors").fetchall()
        event_count_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at>=? AND event_type!='baseline'",
            (cutoff_24h,),
        ).fetchone()["c"]
        snapshots_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM snapshots WHERE created_at>=?",
            (cutoff_24h,),
        ).fetchone()["c"]

    monitors = [dict(r) for r in monitor_rows]
    total_checks = sum(int(m.get("checks_count") or 0) for m in monitors)
    success = sum(int(m.get("success_count") or 0) for m in monitors)
    latencies = [float(m["avg_latency_ms"]) for m in monitors if m.get("avg_latency_ms") is not None]
    return jsonify({
        "monitors": len(monitors),
        "active": sum(1 for m in monitors if m.get("enabled")),
        "checks_total": total_checks,
        "checks_24h": snapshots_24h,
        "success_rate": round(success / total_checks * 100, 1) if total_checks else None,
        "avg_latency_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "signals_24h": event_count_24h,
        "errors": sum(1 for m in monitors if m.get("last_error")),
    })


@app.get("/api/monitors/<int:monitor_id>/history")
def monitor_history(monitor_id):
    limit = max(1, min(50, int(request.args.get("limit", "25"))))
    with db() as conn:
        monitor = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not monitor:
            return jsonify({"error": "Monitor not found."}), 404
        snaps = conn.execute(
            """SELECT id,created_at,http_status,excerpt,price,latency_ms,changed,triggers
               FROM snapshots WHERE monitor_id=? ORDER BY created_at DESC LIMIT ?""",
            (monitor_id, limit),
        ).fetchall()
        events = conn.execute(
            """SELECT id,created_at,event_type,summary,detail
               FROM events WHERE monitor_id=? ORDER BY created_at DESC LIMIT ?""",
            (monitor_id, limit),
        ).fetchall()

    out_snaps = []
    for row in snaps:
        d = dict(row)
        d["created_at_iso"] = iso(d["created_at"])
        try:
            d["triggers"] = json.loads(d.get("triggers") or "[]")
        except json.JSONDecodeError:
            d["triggers"] = []
        out_snaps.append(d)

    out_events = []
    for row in events:
        d = dict(row)
        d["created_at_iso"] = iso(d["created_at"])
        try:
            d["detail_json"] = json.loads(d.get("detail") or "{}")
        except json.JSONDecodeError:
            d["detail_json"] = None
        out_events.append(d)

    return jsonify({
        "monitor": serialize_monitor(monitor),
        "snapshots": out_snaps,
        "events": out_events,
    })


@app.post("/api/probe")
def probe_page():
    denied = require_admin()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_public_url(str(payload.get("url") or "").strip())
        selector = str(payload.get("selector") or "").strip() or None
        started = time.perf_counter()
        status, final_url, html = fetch_page(url)
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = extract_content(html, selector)
        price = extract_price(content, str(payload.get("price_regex") or "").strip() or None)
        soup = BeautifulSoup(html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None
        return jsonify({
            "ok": True,
            "http_status": status,
            "final_url": final_url,
            "title": title,
            "latency_ms": latency_ms,
            "content_chars": len(content),
            "price": price,
            "currency": detect_currency(content),
            "preview": content[:700],
            "monitorability": "good" if status == 200 and len(content) >= 40 else "limited",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "monitorability": "failed"}), 400


@app.get("/api/report")
def report():
    days = max(1, min(30, int(request.args.get("days", "7"))))
    cutoff = now_ts() - days * 86400
    with db() as conn:
        events = conn.execute(
            """SELECT e.*,m.name AS monitor_name,m.url
               FROM events e JOIN monitors m ON m.id=e.monitor_id
               WHERE e.created_at>=? AND e.event_type!='baseline'
               ORDER BY e.created_at DESC""",
            (cutoff,),
        ).fetchall()

    type_counts = {}
    monitor_counts = {}
    items = []
    for row in events:
        d = dict(row)
        type_counts[d["event_type"]] = type_counts.get(d["event_type"], 0) + 1
        monitor_counts[d["monitor_name"]] = monitor_counts.get(d["monitor_name"], 0) + 1
        items.append({
            "monitor": d["monitor_name"],
            "event_type": d["event_type"],
            "summary": d["summary"],
            "created_at_iso": iso(d["created_at"]),
            "url": d["url"],
        })

    highlights = sorted(monitor_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    return jsonify({
        "days": days,
        "total_signals": len(items),
        "type_counts": type_counts,
        "top_monitors": [{"name": name, "signals": count} for name, count in highlights],
        "items": items[:50],
    })


@app.get("/api/lab")
def reliability_lab():
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM lab_runs ORDER BY created_at DESC LIMIT 40"""
        ).fetchall()
    latest = {}
    for row in rows:
        d = dict(row)
        if d["target"] not in latest:
            d["created_at_iso"] = iso(d["created_at"])
            d["ok"] = bool(d["ok"])
            latest[d["target"]] = d
    values = list(latest.values())
    return jsonify({
        "last_run_iso": iso(max((v["created_at"] for v in values), default=None)),
        "targets": values,
        "pass_rate": round(sum(1 for v in values if v["ok"]) / len(values) * 100, 1) if values else None,
    })


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
        ignore_regex = str(payload.get("ignore_regex") or "").strip() or None
        kind = str(payload.get("kind") or "content").strip().lower()
        if kind not in {"content", "price", "stock", "keyword"}:
            kind = "content"
        currency = str(payload.get("currency") or "").strip().upper() or None
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
        if ignore_regex:
            re.compile(ignore_regex)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO monitors(
                name,url,selector,must_contain,price_regex,max_price,interval_min,
                webhook_url,enabled,created_at,next_check,kind,ignore_regex,currency
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (
                name,url,selector,must_contain,price_regex,max_price,interval_min,
                webhook_url,now_ts(),now_ts(),kind,ignore_regex,currency
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


@app.patch("/api/monitors/<int:monitor_id>")
def update_monitor(monitor_id):
    denied = require_admin()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    allowed = {
        "name", "selector", "must_contain", "price_regex", "max_price",
        "interval_min", "webhook_url", "kind", "ignore_regex", "currency"
    }
    updates = {}
    try:
        for key in allowed:
            if key not in payload:
                continue
            value = payload[key]
            if key in {"name", "selector", "must_contain", "price_regex", "webhook_url", "kind", "ignore_regex", "currency"}:
                value = str(value or "").strip() or None
            if key == "name" and not value:
                raise ValueError("Name cannot be empty.")
            if key == "kind":
                if value not in {"content", "price", "stock", "keyword"}:
                    raise ValueError("Invalid monitor type.")
            if key == "interval_min":
                value = max(1, min(1440, int(value)))
            if key == "max_price":
                value = float(value) if value not in (None, "") else None
            if key == "webhook_url" and value:
                validate_public_url(value)
            if key in {"price_regex", "ignore_regex"} and value:
                re.compile(value)
            if key == "currency" and value:
                value = value.upper()
            updates[key] = value
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if not updates:
        return jsonify({"error": "No editable fields supplied."}), 400

    with db() as conn:
        exists = conn.execute("SELECT id FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not exists:
            return jsonify({"error": "Monitor not found."}), 404
        updates["next_check"] = now_ts()
        sets = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE monitors SET {sets} WHERE id=?", [*updates.values(), monitor_id])
        row = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    return jsonify(serialize_monitor(row))


@app.get("/api/export.csv")
def export_csv():
    import csv
    import io
    from flask import Response

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "name", "url", "kind", "interval_min", "health", "last_price", "currency",
        "last_checked", "last_change", "success_rate", "change_count"
    ])
    with db() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY id").fetchall()
    for row in rows:
        m = serialize_monitor(row)
        writer.writerow([
            m["name"], m["url"], m.get("kind"), m["interval_min"], m["health"],
            m.get("last_price"), m.get("currency"), m.get("last_checked_iso"),
            m.get("last_change_at_iso"), m.get("success_rate"), m.get("change_count"),
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pricepulse-monitors.csv"},
    )


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
migrate_schema()
startup_self_test()
seed_demo_monitor()
ensure_scheduler()
