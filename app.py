import os
import re
import json
import time
import hashlib
import shutil
import threading
import zipfile
import mimetypes
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote
from urllib import robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_file, Response, request

app = Flask(__name__)
application = app

TARGET_URL = os.getenv("TARGET_URL", "https://www.grownbrilliance.com/").rstrip("/") + "/"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CAPTURE_DIR = DATA_DIR / "grown-brilliance-public"
ZIP_PATH = DATA_DIR / "grown-brilliance-public.zip"
MAX_PAGES = int(os.getenv("MAX_PAGES", "5000"))
MAX_ASSETS = int(os.getenv("MAX_ASSETS", "15000"))
PAGE_WORKERS = int(os.getenv("PAGE_WORKERS", "10"))
ASSET_WORKERS = int(os.getenv("ASSET_WORKERS", "14"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
AUTO_START = os.getenv("AUTO_START", "1") == "1"
FORCE_RECRAWL = os.getenv("FORCE_RECRAWL", "1") == "1"
USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (compatible; VediReferenceCapture/1.0; public-storefront-archiver)"
)

TARGET_HOST = urlparse(TARGET_URL).netloc.lower()
TARGET_ROOT = TARGET_HOST[4:] if TARGET_HOST.startswith("www.") else TARGET_HOST
ASSET_HOST_SUFFIXES = tuple(
    x.strip().lower()
    for x in os.getenv(
        "ASSET_HOST_SUFFIXES",
        f"{TARGET_ROOT},cdn.shopify.com,shopifycdn.net,cdn.shopifycdn.net,fonts.googleapis.com,fonts.gstatic.com"
    ).split(",")
    if x.strip()
)

state_lock = threading.Lock()
crawl_lock = threading.Lock()
state = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "pages_discovered": 0,
    "pages_saved": 0,
    "assets_discovered": 0,
    "assets_saved": 0,
    "errors": 0,
    "message": "Waiting to start",
    "zip_path": str(ZIP_PATH),
}


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def inc_state(key, amount=1):
    with state_lock:
        state[key] = int(state.get(key, 0)) + amount


def snapshot_state():
    with state_lock:
        return dict(state)


def normalize_url(url, base=None):
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    return url


def is_target_page(url):
    host = urlparse(url).netloc.lower()
    return host == TARGET_ROOT or host == f"www.{TARGET_ROOT}"


def is_allowed_asset(url):
    host = urlparse(url).netloc.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in ASSET_HOST_SUFFIXES)


def safe_component(value):
    value = unquote(value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value[:180] or "_"


def page_path(url):
    p = urlparse(url)
    host = safe_component(p.netloc)
    raw = p.path or "/"
    parts = [safe_component(x) for x in raw.split("/") if x]
    if raw.endswith("/") or not parts:
        parts.append("index")
    elif "." not in parts[-1]:
        parts.append("index")
    if p.query:
        qhash = hashlib.sha1(p.query.encode("utf-8")).hexdigest()[:10]
        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}__q_{qhash}{ext or '.html'}"
    elif not os.path.splitext(parts[-1])[1]:
        parts[-1] += ".html"
    elif parts[-1] == "index":
        parts[-1] = "index.html"
    return CAPTURE_DIR / "pages" / host / Path(*parts)


def asset_path(url, content_type=None):
    p = urlparse(url)
    host = safe_component(p.netloc)
    raw_parts = [safe_component(x) for x in p.path.split("/") if x]
    if not raw_parts:
        raw_parts = ["index"]
    name = raw_parts[-1]
    if "." not in name:
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".bin"
        name += ext
        raw_parts[-1] = name
    if p.query:
        qhash = hashlib.sha1(p.query.encode("utf-8")).hexdigest()[:10]
        stem, ext = os.path.splitext(raw_parts[-1])
        raw_parts[-1] = f"{stem}__q_{qhash}{ext}"
    return CAPTURE_DIR / "assets" / host / Path(*raw_parts)


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="ignore")


def extract_asset_urls(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = set()

    attrs = [
        ("script", "src"),
        ("img", "src"),
        ("source", "src"),
        ("video", "src"),
        ("audio", "src"),
        ("iframe", "src"),
    ]
    for tag, attr in attrs:
        for node in soup.find_all(tag):
            u = normalize_url(node.get(attr), base_url)
            if u:
                out.add(u)

    for node in soup.find_all("link"):
        rel = " ".join(node.get("rel") or []).lower()
        if any(k in rel for k in ("stylesheet", "icon", "preload", "modulepreload", "manifest")):
            u = normalize_url(node.get("href"), base_url)
            if u:
                out.add(u)

    for node in soup.find_all(["img", "source"]):
        srcset = node.get("srcset") or ""
        for item in srcset.split(","):
            candidate = item.strip().split(" ")[0].strip()
            u = normalize_url(candidate, base_url)
            if u:
                out.add(u)

    return out


def extract_css_urls(css, base_url):
    out = set()
    for match in re.findall(r"url\(([^)]+)\)", css, flags=re.I):
        raw = match.strip().strip("'\"")
        if raw.startswith("data:"):
            continue
        u = normalize_url(raw, base_url)
        if u:
            out.add(u)
    for match in re.findall(r"@import\s+(?:url\()?['\"]?([^'\"\)\s;]+)", css, flags=re.I):
        u = normalize_url(match, base_url)
        if u:
            out.add(u)
    return out


def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.8",
        "Accept": "*/*",
    })
    return s


def fetch_text(session, url):
    r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.url, r.headers


def collect_sitemap_urls(session, sitemap_url, seen_sitemaps=None, depth=0):
    if seen_sitemaps is None:
        seen_sitemaps = set()
    if depth > 6 or sitemap_url in seen_sitemaps:
        return set(), seen_sitemaps
    seen_sitemaps.add(sitemap_url)

    try:
        r = session.get(sitemap_url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        xml = r.content
        path = CAPTURE_DIR / "meta" / "sitemaps" / f"{hashlib.sha1(sitemap_url.encode()).hexdigest()}.xml"
        write_bytes(path, xml)
        root = ET.fromstring(xml)
    except Exception:
        inc_state("errors")
        return set(), seen_sitemaps

    locs = []
    for node in root.iter():
        if node.tag.lower().endswith("loc") and node.text:
            locs.append(node.text.strip())

    tag = root.tag.lower()
    urls = set()
    if tag.endswith("sitemapindex"):
        for child in locs:
            child_urls, seen_sitemaps = collect_sitemap_urls(
                session, child, seen_sitemaps, depth + 1
            )
            urls.update(child_urls)
    else:
        for u in locs:
            u = normalize_url(u)
            if u and is_target_page(u):
                urls.add(u)

    return urls, seen_sitemaps


def build_robot_parser(session):
    robots_url = urljoin(TARGET_URL, "/robots.txt")
    rp = robotparser.RobotFileParser()
    try:
        r = session.get(robots_url, timeout=REQUEST_TIMEOUT)
        txt = r.text if r.ok else ""
        write_text(CAPTURE_DIR / "meta" / "robots.txt", txt)
        rp.set_url(robots_url)
        rp.parse(txt.splitlines())
    except Exception:
        rp = None
        inc_state("errors")
    return rp


def crawl_page(url, rp):
    if rp and not rp.can_fetch(USER_AGENT, url):
        return {"url": url, "skipped": "robots", "assets": []}

    s = get_session()
    try:
        r = s.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400 or "text/html" not in ctype.lower():
            return {"url": url, "status": r.status_code, "assets": []}
        html = r.text
        dest = page_path(url)
        write_text(dest, html)
        assets = extract_asset_urls(html, r.url)
        inc_state("pages_saved")
        return {
            "url": url,
            "final_url": r.url,
            "status": r.status_code,
            "content_type": ctype,
            "local_path": str(dest.relative_to(CAPTURE_DIR)),
            "assets": sorted(assets),
        }
    except Exception as e:
        inc_state("errors")
        return {"url": url, "error": str(e), "assets": []}


def download_asset(url):
    if not is_allowed_asset(url):
        return {"url": url, "skipped": "external-host"}
    s = get_session()
    try:
        r = s.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return {"url": url, "status": r.status_code}
        ctype = r.headers.get("content-type", "")
        dest = asset_path(url, ctype)
        write_bytes(dest, r.content)
        inc_state("assets_saved")
        nested = []
        if "text/css" in ctype.lower():
            try:
                nested = sorted(extract_css_urls(r.text, r.url))
            except Exception:
                nested = []
        return {
            "url": url,
            "final_url": r.url,
            "status": r.status_code,
            "content_type": ctype,
            "bytes": len(r.content),
            "local_path": str(dest.relative_to(CAPTURE_DIR)),
            "nested_assets": nested,
        }
    except Exception as e:
        inc_state("errors")
        return {"url": url, "error": str(e)}


def create_zip():
    tmp = str(ZIP_PATH) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in CAPTURE_DIR.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(CAPTURE_DIR.parent))
    os.replace(tmp, ZIP_PATH)


def do_crawl(force=False):
    if not crawl_lock.acquire(blocking=False):
        return
    try:
        set_state(
            status="running",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            finished_at=None,
            pages_discovered=0,
            pages_saved=0,
            assets_discovered=0,
            assets_saved=0,
            errors=0,
            message="Preparing capture",
        )

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if force and CAPTURE_DIR.exists():
            shutil.rmtree(CAPTURE_DIR, ignore_errors=True)
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        if force and ZIP_PATH.exists():
            ZIP_PATH.unlink(missing_ok=True)

        session = get_session()
        rp = build_robot_parser(session)

        set_state(message="Reading Shopify sitemap")
        sitemap_url = urljoin(TARGET_URL, "/sitemap.xml")
        urls, seen_sitemaps = collect_sitemap_urls(session, sitemap_url)
        urls.add(TARGET_URL)

        page_urls = sorted(urls)[:MAX_PAGES]
        set_state(pages_discovered=len(page_urls), message="Capturing public HTML")

        page_results = []
        assets = set()

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = {pool.submit(crawl_page, u, rp): u for u in page_urls}
            for fut in as_completed(futures):
                result = fut.result()
                page_results.append(result)
                assets.update(result.get("assets") or [])

        all_asset_urls = set(assets)
        set_state(assets_discovered=len(all_asset_urls), message="Downloading public assets")

        asset_results = []
        processed = set()
        round_urls = set(u for u in all_asset_urls if is_allowed_asset(u))

        while round_urls and len(processed) < MAX_ASSETS:
            batch = list(round_urls - processed)[: max(0, MAX_ASSETS - len(processed))]
            if not batch:
                break
            new_nested = set()
            with ThreadPoolExecutor(max_workers=ASSET_WORKERS) as pool:
                futures = {pool.submit(download_asset, u): u for u in batch}
                for fut in as_completed(futures):
                    result = fut.result()
                    asset_results.append(result)
                    processed.add(result.get("url"))
                    for nested in result.get("nested_assets") or []:
                        if is_allowed_asset(nested) and nested not in processed:
                            new_nested.add(nested)
            round_urls = new_nested
            all_asset_urls.update(new_nested)
            set_state(assets_discovered=min(len(all_asset_urls), MAX_ASSETS))

        manifest = {
            "target": TARGET_URL,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "limits": {
                "max_pages": MAX_PAGES,
                "max_assets": MAX_ASSETS,
                "page_workers": PAGE_WORKERS,
                "asset_workers": ASSET_WORKERS,
            },
            "robots_respected": True,
            "sitemaps": sorted(seen_sitemaps),
            "pages": page_results,
            "assets": asset_results,
            "external_asset_urls_not_downloaded": sorted(
                u for u in all_asset_urls if not is_allowed_asset(u)
            ),
        }
        write_text(
            CAPTURE_DIR / "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )

        summary = {
            "target": TARGET_URL,
            "pages_discovered": len(page_urls),
            "pages_saved": snapshot_state()["pages_saved"],
            "assets_discovered": len(all_asset_urls),
            "assets_saved": snapshot_state()["assets_saved"],
            "errors": snapshot_state()["errors"],
            "zip": ZIP_PATH.name,
        }
        write_text(
            CAPTURE_DIR / "SUMMARY.json",
            json.dumps(summary, indent=2),
        )

        set_state(message="Packaging ZIP")
        create_zip()
        set_state(
            status="complete",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            message="Capture complete",
        )
    except Exception as e:
        inc_state("errors")
        set_state(
            status="failed",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            message=f"Capture failed: {e}",
        )
    finally:
        crawl_lock.release()


def start_crawl(force=False):
    current = snapshot_state()
    if current["status"] == "running":
        return False
    t = threading.Thread(target=do_crawl, kwargs={"force": force}, daemon=True)
    t.start()
    return True


@app.get("/api/health")
def health():
    return jsonify({"ok": True, **snapshot_state()})


@app.get("/pricepulse/health")
def legacy_health():
    return health()


@app.get("/api/status")
def status():
    s = snapshot_state()
    s["download_ready"] = ZIP_PATH.exists()
    if ZIP_PATH.exists():
        s["zip_bytes"] = ZIP_PATH.stat().st_size
    return jsonify(s)


@app.route("/api/start", methods=["GET", "POST"])
def api_start():
    force = request.args.get("force", "1") == "1"
    started = start_crawl(force=force)
    return jsonify({"started": started, **snapshot_state()})


@app.get("/download")
def download():
    if not ZIP_PATH.exists():
        return jsonify({"error": "ZIP is not ready", **snapshot_state()}), 404
    return send_file(
        ZIP_PATH,
        as_attachment=True,
        download_name="grown-brilliance-public.zip",
        mimetype="application/zip",
    )


@app.get("/")
def home():
    s = snapshot_state()
    ready = ZIP_PATH.exists()
    button = (
        '<a href="/download" style="display:inline-block;padding:12px 18px;background:#111;color:white;text-decoration:none;border-radius:8px">Download capture ZIP</a>'
        if ready
        else '<a href="/api/start?force=1">Start/restart capture</a>'
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vedi Reference Capture</title>
<style>body{{font-family:Inter,Arial,sans-serif;max-width:760px;margin:60px auto;padding:0 20px;color:#171717}}code,pre{{background:#f4f4f4;padding:4px 7px;border-radius:5px}}pre{{white-space:pre-wrap;padding:16px}}.muted{{color:#666}}</style>
</head><body>
<h1>Vedi public storefront capture</h1>
<p class="muted">Target: {TARGET_URL}</p>
<p>Status: <strong>{s['status']}</strong> — {s['message']}</p>
<pre>{json.dumps(s, indent=2)}</pre>
<p>{button}</p>
<p><a href="/api/status">JSON status</a></p>
</body></html>"""
    return Response(html, mimetype="text/html")


if AUTO_START:
    start_crawl(force=FORCE_RECRAWL)
