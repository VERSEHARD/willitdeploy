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
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

APP_VERSION = "0.5.1"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pricepulse.sqlite3"
ADMIN_TOKEN = os.getenv("MONITOR_TOKEN", "")
CHECK_TICK_SECONDS = max(10, int(os.getenv("CHECK_TICK_SECONDS", "30")))
DEFAULT_INTERVAL_MIN = max(1, int(os.getenv("DEFAULT_INTERVAL_MIN", "5")))
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
ENABLE_BACKGROUND = os.getenv("ENABLE_BACKGROUND", "1") == "1"
MAX_BODY_BYTES = 8_000_000
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
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
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
            "is_demo": "INTEGER NOT NULL DEFAULT 0",
            "last_availability": "TEXT"
        }
        for name, ddl in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE monitors ADD COLUMN {name} {ddl}")

        lab_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lab_runs)").fetchall()} if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lab_runs'"
        ).fetchone() else set()

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

        CREATE TABLE IF NOT EXISTS product_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            meta TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_product_events_created
            ON product_events(created_at DESC);

        CREATE TABLE IF NOT EXISTS pilot_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            contact TEXT NOT NULL,
            urls_json TEXT NOT NULL,
            rules TEXT NOT NULL,
            delivery TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pilot_requests_created
            ON pilot_requests(created_at DESC);

        CREATE TABLE IF NOT EXISTS listing_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            price REAL,
            currency TEXT,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            meta TEXT,
            UNIQUE(monitor_id, item_key),
            FOREIGN KEY(monitor_id) REFERENCES monitors(id)
        );
        CREATE INDEX IF NOT EXISTS idx_listing_items_monitor_seen
            ON listing_items(monitor_id, last_seen DESC);
        """)

        lab_additions = {
            "stable": "INTEGER",
            "second_latency_ms": "INTEGER",
            "expected_stable": "INTEGER",
            "stability_correct": "INTEGER"
        }
        for name, ddl in lab_additions.items():
            if name not in lab_cols:
                conn.execute(f"ALTER TABLE lab_runs ADD COLUMN {name} {ddl}")



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


def extract_structured_product(html):
    """Best-effort extraction from ecommerce metadata before falling back to visible text."""
    soup = BeautifulSoup(html, "html.parser")
    result = {"price": None, "currency": None, "availability": None, "source": None}

    price_selectors = [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[property="og:price:amount"]', "content"),
        ('meta[itemprop="price"]', "content"),
        ('[itemprop="price"]', "content"),
    ]
    for selector, attr in price_selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        raw = node.get(attr) or node.get_text(" ", strip=True)
        if not raw:
            continue
        cleaned = re.sub(r"[^0-9.\-]", "", raw.replace(",", ""))
        try:
            result["price"] = float(cleaned)
            result["source"] = selector
            break
        except ValueError:
            pass

    currency_node = (
        soup.select_one('meta[property="product:price:currency"]')
        or soup.select_one('meta[property="og:price:currency"]')
        or soup.select_one('meta[itemprop="priceCurrency"]')
        or soup.select_one('[itemprop="priceCurrency"]')
    )
    if currency_node:
        raw = currency_node.get("content") or currency_node.get_text(" ", strip=True)
        if raw:
            result["currency"] = raw.strip().upper()[:8]

    availability_node = soup.select_one('[itemprop="availability"]')
    if availability_node:
        raw = availability_node.get("href") or availability_node.get("content") or availability_node.get_text(" ", strip=True)
        if raw:
            result["availability"] = raw.split("/")[-1].strip()

    def visit(value):
        if isinstance(value, dict):
            type_value = value.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any(t in {"Offer", "AggregateOffer", "Product"} for t in types if t):
                offers = value.get("offers")
                if isinstance(offers, dict):
                    visit(offers)
                elif isinstance(offers, list):
                    for item in offers:
                        visit(item)

                if result["price"] is None:
                    for key in ("price", "lowPrice", "highPrice"):
                        if value.get(key) not in (None, ""):
                            try:
                                result["price"] = float(str(value[key]).replace(",", ""))
                                result["source"] = "json-ld"
                                break
                            except ValueError:
                                pass
                if result["currency"] is None and value.get("priceCurrency"):
                    result["currency"] = str(value["priceCurrency"]).upper()[:8]
                if result["availability"] is None and value.get("availability"):
                    result["availability"] = str(value["availability"]).split("/")[-1]
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            visit(json.loads(raw))
        except Exception:
            continue

    return result


def extract_marketplace_listings(html, base_url):
    """Extract public listing cards from supported marketplace/search pages."""
    soup = BeautifulSoup(html, "html.parser")
    host = (urlparse(base_url).hostname or "").lower()
    results = []
    seen = set()

    if "vinted." in host:
        link_pattern = re.compile(r"/items/(\d+)")
        currency = "GBP" if host.endswith(".co.uk") else None
    elif "ebay." in host:
        link_pattern = re.compile(r"/itm/(?:[^/]+/)?(\d+)")
        currency = "GBP" if host.endswith(".co.uk") else None
    else:
        return results

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        match = link_pattern.search(href)
        if not match:
            continue
        item_key = match.group(1)
        if item_key in seen:
            continue

        node = anchor
        context = ""
        for _ in range(5):
            node = node.parent if node and node.parent else None
            if node is None:
                break
            candidate = normalize_text(node.get_text(" ", strip=True))
            if len(candidate) >= 12:
                context = candidate
            if len(candidate) >= 40:
                break

        anchor_text = normalize_text(anchor.get_text(" ", strip=True))
        title = anchor.get("title") or anchor.get("aria-label") or anchor_text or context[:180]
        title = normalize_text(title)[:220]
        price = extract_price(context, None)
        if price is None:
            price = extract_price(anchor_text, None)

        results.append({
            "item_key": item_key,
            "url": urljoin(base_url, href.split("?")[0]),
            "title": title,
            "price": price,
            "currency": currency,
            "context": context[:500],
        })
        seen.add(item_key)
        if len(results) >= 100:
            break

    return results


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
        structured = extract_structured_product(html)
        if m["price_regex"] or m["selector"]:
            price = extract_price(content, m["price_regex"])
        else:
            price = structured.get("price")
            if price is None:
                price = extract_price(content, None)
        currency = m.get("currency") or structured.get("currency") or detect_currency(content)
        availability = structured.get("availability")
        host = (urlparse(final_url).hostname or "").lower()
        if "vinted." in host and "/items/" in final_url:
            availability = "Sold" if re.search(r"\bSold\b", content, flags=re.I) else "Active"

        listing_feed = extract_marketplace_listings(html, final_url) if m.get("kind") == "listing_feed" else []
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
        def in_stock(value):
            if not value:
                return None
            normalized = str(value).lower()
            if "outofstock" in normalized or "out of stock" in normalized or "soldout" in normalized:
                return False
            if "instock" in normalized or "in stock" in normalized or "limitedavailability" in normalized:
                return True
            return None

        stock_now = in_stock(availability)
        stock_before = in_stock(m.get("last_availability"))
        stock_became_available = m.get("kind") == "stock" and stock_now is True and stock_before is False
        stock_became_unavailable = m.get("kind") == "stock" and stock_now is False and stock_before is True

        new_listing_items = []
        if m.get("kind") == "listing_feed" and listing_feed:
            with db() as conn:
                existing_rows = conn.execute(
                    "SELECT item_key FROM listing_items WHERE monitor_id=?",
                    (monitor_id,),
                ).fetchall()
                existing_keys = {row["item_key"] for row in existing_rows}
                had_baseline = bool(existing_keys)

                for item in listing_feed:
                    conn.execute(
                        """INSERT INTO listing_items(
                            monitor_id,item_key,url,title,price,currency,first_seen,last_seen,status,meta
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(monitor_id,item_key) DO UPDATE SET
                            url=excluded.url,title=excluded.title,price=excluded.price,
                            currency=excluded.currency,last_seen=excluded.last_seen,status='active',meta=excluded.meta""",
                        (
                            monitor_id,item["item_key"],item["url"],item["title"],item["price"],item["currency"],
                            checked,checked,"active",json.dumps({"context": item.get("context")})
                        ),
                    )

                if had_baseline:
                    for item in listing_feed:
                        if item["item_key"] in existing_keys:
                            continue
                        title_ok = True if not m["must_contain"] else m["must_contain"].lower() in (item["title"] or "").lower()
                        price_ok = True if m["max_price"] is None else (item["price"] is not None and item["price"] <= float(m["max_price"]))
                        if title_ok and price_ok:
                            new_listing_items.append(item)

        event_types = []
        if new_listing_items:
            event_types.append("new_listing")
        if changed and m.get("kind") != "listing_feed":
            event_types.append("content_changed")
        if price_changed:
            event_types.append("price_changed")
        if threshold_crossed:
            event_types.append("price_threshold")
        if keyword_became_true:
            event_types.append("keyword_match")
        if keyword_became_false:
            event_types.append("keyword_lost")
        if stock_became_available:
            event_types.append("stock_available")
        if stock_became_unavailable:
            event_types.append("stock_unavailable")

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
            "availability": availability,
            "structured_source": structured.get("source"),
            "url": final_url,
            "new_listings": new_listing_items[:10],
        }

        with db() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_checked=?,next_check=?,last_http_status=?,last_hash=?,last_excerpt=?,
                       last_price=?,last_error=NULL,change_count=change_count+?,
                       checks_count=?,success_count=?,last_latency_ms=?,avg_latency_ms=?,
                       last_change_at=CASE WHEN ?=1 THEN ? ELSE last_change_at END,
                       currency=COALESCE(currency,?),last_availability=?
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
                    availability,
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
                    "stock_available": "Product became available.",
                    "stock_unavailable": "Product became unavailable.",
                    "new_listing": f"{len(new_listing_items)} new matching listing{'s' if len(new_listing_items) != 1 else ''}.",
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
                    "availability": availability,
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
            "availability": availability,
            "structured_source": structured.get("source"),
            "keyword_ok": keyword_ok,
            "threshold_ok": threshold_ok,
            "latency_ms": latency_ms,
            "diff": diff_lines,
            "listing_count": len(listing_feed),
            "new_listing_count": len(new_listing_items),
            "new_listings": new_listing_items[:10],
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
            "target": "Vinted UK fashion feed",
            "url": "https://www.vinted.co.uk/catalog/1223-aviatoru-tipa-jakas/brand/14803-vintage-dressing",
            "expect": "Vintage Dressing",
            "expect_stable": False,
        },
        {
            "target": "Vinted live Japan Style item",
            "url": "https://www.vinted.co.uk/items/7587599448-y2k-japanese-ruffle-blouse-sheer-ivory-shirt-with-contrast-cuffs",
            "expect": "Japan Style",
            "expect_stable": False,
        },
        {
            "target": "Rightbiz UK business feed",
            "url": "https://www.rightbiz.co.uk/search/?more_category=none&sector=businesses&location=uk&sortby=new&noindex=1",
            "expect": "Business",
            "expect_stable": True,
        },
        {
            "target": "BusinessesForSale UK feed",
            "url": "https://uk.businessesforsale.com/uk/search/businesses-for-sale",
            "expect": "Businesses",
            "expect_stable": False,
        },
        {
            "target": "Dynamic control · TimeAPI.io",
            "url": "https://timeapi.io/api/Time/current/zone?timeZone=UTC",
            "expect": "dateTime",
            "expect_stable": False,
        },
    ]

    for target in targets:
        status = None
        second_status = None
        latency_ms = None
        second_latency_ms = None
        html = ""
        stable = None
        expected_stable = bool(target.get("expect_stable", True))
        stability_correct = None
        signal = None
        error = None
        ok = False

        try:
            first_started = time.perf_counter()
            status, final_url, html = fetch_page(target["url"])
            latency_ms = int((time.perf_counter() - first_started) * 1000)
            first_text = extract_content(html, None)
            first_hash = hashlib.sha256(first_text.encode("utf-8")).hexdigest()

            time.sleep(0.2)

            second_started = time.perf_counter()
            second_status, _, second_html = fetch_page(target["url"])
            second_latency_ms = int((time.perf_counter() - second_started) * 1000)
            second_text = extract_content(second_html, None)
            second_hash = hashlib.sha256(second_text.encode("utf-8")).hexdigest()

            stable = first_hash == second_hash
            expected_stable = bool(target.get("expect_stable", True))
            stability_correct = stable == expected_stable
            has_signal = (
                target["expect"].lower() in first_text.lower()
                and target["expect"].lower() in second_text.lower()
            )
            ok = status == 200 and second_status == 200 and has_signal
            signal = target["expect"] if has_signal else None
            if not has_signal:
                error = f"Expected signal not found consistently: {target['expect']}"
        except Exception as exc:
            error = str(exc)
            if latency_ms is None:
                latency_ms = 0

        print("[PricePulse] lab", json.dumps({
            "target": target["target"],
            "ok": ok,
            "stable": stable,
            "expected_stable": expected_stable,
            "stability_correct": stability_correct,
            "http_status": status,
            "second_http_status": second_status,
            "latency_ms": latency_ms,
            "second_latency_ms": second_latency_ms,
            "signal": signal,
            "error": error,
        }, sort_keys=True), flush=True)

        with db() as conn:
            conn.execute(
                """INSERT INTO lab_runs(
                    target,url,created_at,ok,http_status,latency_ms,content_bytes,signal,error,
                    stable,second_latency_ms,expected_stable,stability_correct
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    None if stable is None else (1 if stable else 0),
                    second_latency_ms,
                    1 if expected_stable else 0,
                    None if stability_correct is None else (1 if stability_correct else 0),
                ),
            )
            conn.execute(
                """DELETE FROM lab_runs WHERE id NOT IN (
                    SELECT id FROM lab_runs ORDER BY created_at DESC LIMIT 100
                )"""
            )

    lab_last_run = now_ts()
    summary = lab_evidence_summary(100)
    print("[PricePulse] reliability-lab complete", json.dumps({
        "rolling_samples": summary["samples"],
        "rolling_pass_rate": summary["rolling_pass_rate"],
        "rolling_stability_rate": summary["rolling_stability_rate"],
    }, sort_keys=True), flush=True)


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
    if not ENABLE_BACKGROUND:
        return
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



def audit_rendered_ui(response, label):
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    ids = [node.get("id") for node in soup.select("[id]")]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    assert not duplicates, f"{label}: duplicate element ids: {duplicates}"

    id_set = set(ids)
    missing_targets = []
    for node in soup.select("[data-bs-target]"):
        target = (node.get("data-bs-target") or "").strip()
        if target.startswith("#") and target[1:] not in id_set:
            missing_targets.append(target)
    assert not missing_targets, f"{label}: missing data-bs targets: {missing_targets}"

    unlabeled_buttons = []
    for button in soup.find_all("button"):
        visible = normalize_text(button.get_text(" ", strip=True))
        if not visible and not button.get("title") and not button.get("aria-label"):
            unlabeled_buttons.append(str(button)[:120])
    assert not unlabeled_buttons, f"{label}: unlabeled buttons found"

    forbidden_copy = ("unlock the power", "supercharge", "seamlessly integrate")
    lowered = html.lower()
    assert not any(term in lowered for term in forbidden_copy), f"{label}: generic marketing copy found"

    return {
        "ids": len(ids),
        "buttons": len(soup.find_all("button")),
        "links": len(soup.find_all("a")),
    }


def audit_css_contract():
    for filename in ("app.css", "landing.css"):
        css_path = BASE_DIR / "static" / filename
        css = css_path.read_text(encoding="utf-8")
        assert "linear-gradient(" not in css, f"{filename}: decorative gradient violates design contract"
        assert "radial-gradient(" not in css, f"{filename}: decorative gradient violates design contract"
        assert "backdrop-filter" not in css, f"{filename}: glassmorphism violates design contract"
    return True


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
    structured_fixture = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Demo","offers":{"@type":"Offer","price":"129.99","priceCurrency":"USD","availability":"https://schema.org/InStock"}}
    </script></head><body><h1>Demo</h1></body></html>
    """
    listing_fixture = """
    <html><body><div class="card"><a href="/items/123-demo-y2k">Y2K jacket £42.00</a></div></body></html>
    """
    extracted = extract_marketplace_listings(listing_fixture, "https://www.vinted.co.uk/catalog")
    assert extracted and extracted[0]["item_key"] == "123"
    assert extracted[0]["price"] == 42.0

    structured = extract_structured_product(structured_fixture)
    assert structured["price"] == 129.99
    assert structured["currency"] == "USD"
    assert structured["availability"] == "InStock"

    client = app.test_client()
    landing_response = client.get("/", headers={"X-PricePulse-Self-Test": "1"})
    assert landing_response.status_code == 200
    assert b"PricePulse" in landing_response.data
    assert landing_response.headers.get("X-Content-Type-Options") == "nosniff"
    assert landing_response.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in landing_response.headers.get("Content-Security-Policy", "")
    workspace_response = client.get("/app")
    assert workspace_response.status_code == 200
    assert b"Reliability lab" in workspace_response.data
    pilot_response = client.get("/pilot")
    assert pilot_response.status_code == 200
    assert b"Founding pilot" in pilot_response.data
    landing_audit = audit_rendered_ui(landing_response, "landing")
    workspace_audit = audit_rendered_ui(workspace_response, "workspace")
    pilot_audit = audit_rendered_ui(pilot_response, "pilot")
    assert audit_css_contract() is True
    metrics_response = client.get("/api/product-metrics")
    assert metrics_response.status_code == 200
    print("[PricePulse] ui-contract PASS", json.dumps({
        "landing": landing_audit,
        "workspace": workspace_audit,
        "pilot": pilot_audit,
    }, sort_keys=True), flush=True)

    try:
        validate_public_url("http://127.0.0.1/internal")
        raise AssertionError("SSRF guard failed")
    except ValueError:
        pass
    print("[PricePulse] startup-self-test PASS", flush=True)


def seed_demo_monitor():
    """Seed buyer-backed public targets rather than static pricing-page demos."""
    if not SEED_DEMO:
        return
    with db() as conn:
        old_demo_names = (
            "Books demo under 60",
            "Live proof · Linear pricing",
        )
        for name in old_demo_names:
            row = conn.execute("SELECT id FROM monitors WHERE name=?", (name,)).fetchone()
            if row:
                conn.execute("DELETE FROM snapshots WHERE monitor_id=?", (row["id"],))
                conn.execute("DELETE FROM events WHERE monitor_id=?", (row["id"],))
                conn.execute("DELETE FROM listing_items WHERE monitor_id=?", (row["id"],))
                conn.execute("DELETE FROM monitors WHERE id=?", (row["id"],))

        demos = [
            {
                "name": "Buyer-backed · Vinted fast-fashion feed",
                "url": "https://www.vinted.co.uk/catalog/1223-aviatoru-tipa-jakas/brand/14803-vintage-dressing",
                "kind": "listing_feed",
                "must_contain": None,
                "max_price": None,
                "interval_min": 15,
            },
            {
                "name": "Buyer-backed · Vinted Japan Style item",
                "url": "https://www.vinted.co.uk/items/7587599448-y2k-japanese-ruffle-blouse-sheer-ivory-shirt-with-contrast-cuffs",
                "kind": "keyword",
                "must_contain": "Sold",
                "max_price": None,
                "interval_min": 15,
            },
        ]

        for demo in demos:
            exists = conn.execute("SELECT id FROM monitors WHERE name=?", (demo["name"],)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE monitors
                       SET url=?,must_contain=?,max_price=?,interval_min=?,kind=?,enabled=1,next_check=?
                       WHERE id=?""",
                    (
                        demo["url"],demo["must_contain"],demo["max_price"],demo["interval_min"],
                        demo["kind"],now_ts(),exists["id"]
                    ),
                )
                continue
            conn.execute(
                """INSERT INTO monitors(
                    name,url,selector,must_contain,price_regex,max_price,interval_min,
                    webhook_url,enabled,created_at,next_check,kind,is_demo
                ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    demo["name"],
                    demo["url"],
                    None,
                    demo["must_contain"],
                    None,
                    demo["max_price"],
                    demo["interval_min"],
                    None,
                    now_ts(),
                    now_ts(),
                    demo["kind"],
                    1,
                ),
            )
    print("[PricePulse] buyer-backed proof monitors seeded", flush=True)


@app.after_request
def production_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    elif request.path.startswith("/api/") or request.path in {"/", "/app", "/pilot"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.before_request
def boot():
    ensure_scheduler()


def should_track_request():
    if request.headers.get("X-PricePulse-Self-Test") == "1":
        return False
    ua = (request.headers.get("User-Agent") or "").lower()
    bot_tokens = (
        "bot", "crawler", "spider", "slurp", "headless", "uptime",
        "monitoring", "railway", "preview", "facebookexternalhit", "linkedinbot"
    )
    return not any(token in ua for token in bot_tokens)


def lab_evidence_summary(limit=100):
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM lab_runs ORDER BY created_at DESC, id DESC LIMIT ?""",
            (max(8, min(100, int(limit))),),
        ).fetchall()

    history = []
    latest = {}
    per_target = {}
    for row in rows:
        d = dict(row)
        d["ok"] = bool(d["ok"])
        d["stable"] = None if d.get("stable") is None else bool(d["stable"])
        d["expected_stable"] = None if d.get("expected_stable") is None else bool(d["expected_stable"])
        d["stability_correct"] = None if d.get("stability_correct") is None else bool(d["stability_correct"])
        d["created_at_iso"] = iso(d["created_at"])
        history.append(d)
        latest.setdefault(d["target"], d)

        target = per_target.setdefault(d["target"], {
            "target": d["target"],
            "url": d["url"],
            "checks": 0,
            "passes": 0,
            "stable_samples": 0,
            "stable_passes": 0,
        })
        target["checks"] += 1
        target["passes"] += 1 if d["ok"] else 0
        stability_result = d["stability_correct"] if d["stability_correct"] is not None else d["stable"]
        if stability_result is not None:
            target["stable_samples"] += 1
            target["stable_passes"] += 1 if stability_result else 0

    valid_stability = [
        x for x in history
        if (x["stability_correct"] if x["stability_correct"] is not None else x["stable"]) is not None
    ]
    for target in per_target.values():
        target["pass_rate"] = round(target["passes"] / target["checks"] * 100, 1) if target["checks"] else None
        target["stability_rate"] = round(
            target["stable_passes"] / target["stable_samples"] * 100, 1
        ) if target["stable_samples"] else None

    latest_values = list(latest.values())
    latest_stability = [
        x for x in latest_values
        if (x["stability_correct"] if x["stability_correct"] is not None else x["stable"]) is not None
    ]
    return {
        "latest": latest_values,
        "history": history,
        "samples": len(history),
        "latest_pass_rate": round(
            sum(1 for x in latest_values if x["ok"]) / len(latest_values) * 100, 1
        ) if latest_values else None,
        "latest_stability_rate": round(
            sum(
                1 for x in latest_stability
                if (x["stability_correct"] if x["stability_correct"] is not None else x["stable"])
            ) / len(latest_stability) * 100, 1
        ) if latest_stability else None,
        "rolling_pass_rate": round(
            sum(1 for x in history if x["ok"]) / len(history) * 100, 1
        ) if history else None,
        "rolling_stability_rate": round(
            sum(
                1 for x in valid_stability
                if (x["stability_correct"] if x["stability_correct"] is not None else x["stable"])
            ) / len(valid_stability) * 100, 1
        ) if valid_stability else None,
        "per_target": sorted(per_target.values(), key=lambda x: x["target"].lower()),
        "last_run_iso": iso(max((x["created_at"] for x in latest_values), default=None)),
    }


def public_proof():
    evidence = lab_evidence_summary(100)
    with db() as conn:
        checks = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"]
        monitors = conn.execute("SELECT COUNT(*) AS c FROM monitors").fetchone()["c"]
        proof_row = conn.execute(
            """SELECT * FROM monitors
               WHERE is_demo=1
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        recent_events = conn.execute(
            """SELECT e.*,m.name AS monitor_name
               FROM events e JOIN monitors m ON m.id=e.monitor_id
               WHERE m.is_demo=1
               ORDER BY e.id DESC LIMIT 3"""
        ).fetchall()

    proof_monitor = serialize_monitor(proof_row) if proof_row else None
    proof_events = []
    for row in recent_events:
        item = dict(row)
        item["created_at_iso"] = iso(item["created_at"])
        proof_events.append(item)

    history_by_target = {item["target"]: item for item in evidence["per_target"]}
    latest = []
    for item in evidence["latest"]:
        enriched = dict(item)
        historical = history_by_target.get(item["target"], {})
        enriched["history_checks"] = historical.get("checks", 0)
        enriched["history_pass_rate"] = historical.get("pass_rate")
        enriched["history_stability_rate"] = historical.get("stability_rate")
        latest.append(enriched)

    return {
        "lab_targets": len(latest),
        "lab_passed": sum(1 for r in latest if r.get("ok")),
        "lab_pass_rate": evidence["latest_pass_rate"],
        "lab_stability_rate": evidence["latest_stability_rate"],
        "rolling_samples": evidence["samples"],
        "rolling_pass_rate": evidence["rolling_pass_rate"],
        "rolling_stability_rate": evidence["rolling_stability_rate"],
        "checks": checks,
        "monitors": monitors,
        "proof_monitor": proof_monitor,
        "proof_events": proof_events,
        "lab_latest": latest[:8],
        "lab_per_target": evidence["per_target"],
    }



@app.get("/")
def landing():
    proof = public_proof()
    if should_track_request():
        with db() as conn:
            conn.execute(
                "INSERT INTO product_events(created_at,event_type,meta) VALUES(?,?,?)",
                (now_ts(), "landing_view", json.dumps({"ua": (request.headers.get("User-Agent") or "")[:180]})),
            )
    return render_template("landing.html", version=APP_VERSION, **proof)


@app.route("/pilot", methods=["GET", "POST"])
def pilot():
    error = None
    success = None
    submitted = {
        "contact": "",
        "urls": "",
        "rules": "",
        "delivery": "",
    }

    if request.method == "POST":
        submitted = {
            "contact": str(request.form.get("contact") or "").strip(),
            "urls": str(request.form.get("urls") or "").strip(),
            "rules": str(request.form.get("rules") or "").strip(),
            "delivery": str(request.form.get("delivery") or "").strip(),
        }
        honeypot = str(request.form.get("website") or "").strip()
        agreed = request.form.get("agreement") == "on"

        try:
            if honeypot:
                raise ValueError("Could not submit this request.")
            if len(submitted["contact"]) < 3 or len(submitted["contact"]) > 180:
                raise ValueError("Add an email, GitHub username, or another contact we can reply to.")
            if len(submitted["rules"]) < 5 or len(submitted["rules"]) > 2000:
                raise ValueError("Describe what should trigger an alert.")
            if not agreed:
                raise ValueError("Confirm the pilot terms before submitting.")

            raw_urls = [
                line.strip() for line in submitted["urls"].splitlines()
                if line.strip()
            ]
            if not raw_urls:
                raise ValueError("Add at least one public URL.")
            if len(raw_urls) > 5:
                raise ValueError("The founding pilot supports up to 5 URLs.")

            clean_urls = []
            for url in raw_urls:
                clean_urls.append(validate_public_url(url))

            # Avoid accidental duplicate submissions from double-clicks/reloads.
            duplicate_cutoff = now_ts() - 600
            urls_json = json.dumps(clean_urls)
            with db() as conn:
                duplicate = conn.execute(
                    """SELECT id FROM pilot_requests
                       WHERE contact=? AND urls_json=? AND created_at>=?
                       ORDER BY id DESC LIMIT 1""",
                    (submitted["contact"], urls_json, duplicate_cutoff),
                ).fetchone()
                if duplicate:
                    request_id = duplicate["id"]
                else:
                    cur = conn.execute(
                        """INSERT INTO pilot_requests(
                            created_at,contact,urls_json,rules,delivery,status,user_agent
                        ) VALUES(?,?,?,?,?,'new',?)""",
                        (
                            now_ts(),
                            submitted["contact"],
                            urls_json,
                            submitted["rules"],
                            submitted["delivery"][:180] or None,
                            (request.headers.get("User-Agent") or "")[:220],
                        ),
                    )
                    request_id = cur.lastrowid
                    conn.execute(
                        "INSERT INTO product_events(created_at,event_type,meta) VALUES(?,?,?)",
                        (
                            now_ts(),
                            "pilot_submitted",
                            json.dumps({"request_id": request_id, "url_count": len(clean_urls)}),
                        ),
                    )

            success = {
                "request_id": request_id,
                "url_count": len(clean_urls),
            }
            submitted = {"contact": "", "urls": "", "rules": "", "delivery": ""}
        except Exception as exc:
            error = str(exc)

    return render_template(
        "pilot.html",
        version=APP_VERSION,
        error=error,
        success=success,
        submitted=submitted,
    )


@app.get("/api/pilot-requests")
def pilot_requests():
    denied = require_admin()
    if denied:
        return denied
    with db() as conn:
        rows = conn.execute(
            """SELECT id,created_at,contact,urls_json,rules,delivery,status
               FROM pilot_requests ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["created_at_iso"] = iso(d["created_at"])
        try:
            d["urls"] = json.loads(d.pop("urls_json"))
        except Exception:
            d["urls"] = []
            d.pop("urls_json", None)
        out.append(d)
    return jsonify(out)


@app.get("/app")
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
        counts = {
            row["monitor_id"]: row["c"]
            for row in conn.execute(
                "SELECT monitor_id,COUNT(*) AS c FROM listing_items GROUP BY monitor_id"
            ).fetchall()
        }
    items = []
    for row in rows:
        item = serialize_monitor(row)
        item["listing_count"] = int(counts.get(row["id"], 0))
        items.append(item)
    return jsonify(items)


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


@app.post("/api/product-event")
def product_event():
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or "").strip()
    allowed = {"pilot_click", "workspace_open", "probe_started"}
    if event_type not in allowed:
        return jsonify({"error": "Unsupported product event."}), 400
    meta = payload.get("meta")
    with db() as conn:
        conn.execute(
            "INSERT INTO product_events(created_at,event_type,meta) VALUES(?,?,?)",
            (now_ts(), event_type, json.dumps(meta)[:1000] if meta is not None else None),
        )
    return jsonify({"ok": True})


@app.get("/api/product-metrics")
def product_metrics():
    cutoff = now_ts() - 7 * 86400
    with db() as conn:
        rows = conn.execute(
            """SELECT event_type,COUNT(*) AS c
               FROM product_events WHERE created_at>=?
               GROUP BY event_type""",
            (cutoff,),
        ).fetchall()
    counts = {row["event_type"]: row["c"] for row in rows}
    views = int(counts.get("landing_view", 0))
    clicks = int(counts.get("pilot_click", 0))
    return jsonify({
        "window_days": 7,
        "landing_views": views,
        "pilot_clicks": clicks,
        "pilot_submissions": int(counts.get("pilot_submitted", 0)),
        "workspace_opens": int(counts.get("workspace_open", 0)),
        "probe_starts": int(counts.get("probe_started", 0)),
        "pilot_ctr": round(clicks / views * 100, 1) if views else None,
    })


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
        explicit_regex = str(payload.get("price_regex") or "").strip() or None
        structured = extract_structured_product(html)
        price = extract_price(content, explicit_regex) if explicit_regex or selector else structured.get("price")
        if price is None:
            price = extract_price(content, explicit_regex)
        soup = BeautifulSoup(html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None
        script_count = len(soup.find_all("script"))
        client_shell = len(content) < 120 and script_count >= 8
        anti_bot = status in {401, 403, 429} or any(
            phrase in (title or "").lower()
            for phrase in ("just a moment", "access denied", "verify you are human")
        )
        if anti_bot:
            monitorability = "blocked"
        elif client_shell:
            monitorability = "browser-needed"
        elif status == 200 and len(content) >= 40:
            monitorability = "good"
        else:
            monitorability = "limited"
        return jsonify({
            "ok": True,
            "http_status": status,
            "final_url": final_url,
            "title": title,
            "latency_ms": latency_ms,
            "content_chars": len(content),
            "script_count": script_count,
            "price": price,
            "currency": structured.get("currency") or detect_currency(content),
            "availability": structured.get("availability"),
            "structured_source": structured.get("source"),
            "preview": content[:700],
            "monitorability": monitorability,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "monitorability": "failed"}), 400


@app.get("/api/monitors/<int:monitor_id>/listings")
def monitor_listings(monitor_id):
    limit = max(1, min(100, int(request.args.get("limit", "50"))))
    with db() as conn:
        monitor = conn.execute("SELECT id FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not monitor:
            return jsonify({"error": "Monitor not found."}), 404
        rows = conn.execute(
            """SELECT item_key,url,title,price,currency,first_seen,last_seen,status,meta
               FROM listing_items WHERE monitor_id=?
               ORDER BY first_seen DESC LIMIT ?""",
            (monitor_id, limit),
        ).fetchall()

    items = []
    for row in rows:
        d = dict(row)
        d["first_seen_iso"] = iso(d["first_seen"])
        d["last_seen_iso"] = iso(d["last_seen"])
        try:
            d["meta_json"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta_json"] = {}
        items.append(d)
    return jsonify(items)


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
    evidence = lab_evidence_summary(100)
    return jsonify({
        "last_run_iso": evidence["last_run_iso"],
        "targets": evidence["latest"],
        "pass_rate": evidence["latest_pass_rate"],
        "stability_rate": evidence["latest_stability_rate"],
        "rolling": {
            "samples": evidence["samples"],
            "pass_rate": evidence["rolling_pass_rate"],
            "stability_rate": evidence["rolling_stability_rate"],
            "targets": evidence["per_target"],
        },
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
        if kind not in {"content", "price", "stock", "keyword", "listing_feed"}:
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
                if value not in {"content", "price", "stock", "keyword", "listing_feed"}:
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
        conn.execute("DELETE FROM snapshots WHERE monitor_id=?", (monitor_id,))
        conn.execute("DELETE FROM listing_items WHERE monitor_id=?", (monitor_id,))
        cur = conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        if cur.rowcount == 0:
            return jsonify({"error": "Monitor not found."}), 404
    return jsonify({"ok": True})


init_db()
migrate_schema()
startup_self_test()
seed_demo_monitor()
ensure_scheduler()
