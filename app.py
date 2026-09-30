import os, time, shutil, tempfile
from pathlib import Path
from flask import Flask, jsonify, request, Response, stream_with_context

ROOT = Path(tempfile.gettempdir()) / "route-race"
ROOT.mkdir(parents=True, exist_ok=True)
CHUNK = 1024 * 1024

app = Flask(__name__)
application = app

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Railway-Debug"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True)

@app.route("/api/info", methods=["GET"])
def info():
    du = shutil.disk_usage(ROOT)
    return jsonify(
        region=os.getenv("RAILWAY_REPLICA_REGION") or os.getenv("RAILWAY_REGION") or "unknown",
        free_bytes=du.free,
        root=str(ROOT),
        service=os.getenv("RAILWAY_SERVICE_NAME","unknown"),
    )

@app.route("/api/upload/discard", methods=["PUT","OPTIONS"])
def upload_discard():
    if request.method == "OPTIONS":
        return ("",204)
    started=time.perf_counter()
    total=0
    while True:
        b=request.stream.read(CHUNK)
        if not b: break
        total += len(b)
    sec=max(time.perf_counter()-started,1e-9)
    return jsonify(ok=True,bytes=total,seconds_server=sec,server_mbps=(total*8/1_000_000)/sec)

@app.route("/api/upload/tmp", methods=["PUT","OPTIONS"])
def upload_tmp():
    if request.method == "OPTIONS":
        return ("",204)
    p=ROOT/f"u-{time.time_ns()}.bin"
    started=time.perf_counter()
    total=0
    try:
        with open(p,"wb",buffering=CHUNK) as f:
            while True:
                b=request.stream.read(CHUNK)
                if not b: break
                f.write(b); total += len(b)
        sec=max(time.perf_counter()-started,1e-9)
        return jsonify(ok=True,bytes=total,seconds_server=sec,server_mbps=(total*8/1_000_000)/sec)
    finally:
        try: p.unlink()
        except Exception: pass

def gen(total, chunk):
    block=bytes(chunk)
    left=total
    while left:
        n=min(chunk,left)
        yield block[:n]
        left-=n

@app.route("/api/download/generated", methods=["GET"])
def download_generated():
    mb=max(1,min(int(request.args.get("mb","32")),512))
    chunk_kb=max(16,min(int(request.args.get("chunk_kb","2048")),8192))
    total=mb*1024*1024
    chunk=chunk_kb*1024
    return Response(stream_with_context(gen(total,chunk)),mimetype="application/octet-stream",
        headers={"Content-Length":str(total),"X-Accel-Buffering":"no"})

@app.route("/", methods=["GET"])
def root():
    return """<!doctype html><meta charset="utf-8"><title>US West Route Race Node</title>
    <body style="font-family:system-ui;padding:40px"><h1>US West Route Race Node</h1>
    <p>Benchmark node is healthy.</p></body>"""
