from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import resource
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

MAX_LOG_CHARS = 120_000
MAX_REPO_MB = int(os.getenv("MAX_REPO_MB", "250"))
INSTALL_TIMEOUT = int(os.getenv("INSTALL_TIMEOUT_SECONDS", "480"))
BUILD_TIMEOUT = int(os.getenv("BUILD_TIMEOUT_SECONDS", "480"))

KNOWN_NATIVE_OR_RISKY = {
    "deasync": "Native addon; older releases can break across Node/Python/node-gyp changes.",
    "node-sass": "Deprecated native addon; historically sensitive to Node ABI changes.",
    "bcrypt": "Native addon in many versions; runtime compatibility depends on prebuilt binaries/toolchain.",
    "sharp": "Native dependency; usually ships prebuilds but runtime/platform support matters.",
    "canvas": "Native dependency; may require system libraries and compilation.",
    "sqlite3": "Native addon; prebuilt binary availability varies by runtime/platform.",
    "better-sqlite3": "Native addon; Node ABI compatibility matters.",
    "grpc": "Legacy native package; modern projects usually use @grpc/grpc-js.",
    "fsevents": "Platform-specific native dependency (macOS only).",
    "ffi-napi": "Native FFI addon; can be sensitive to Node ABI/toolchain changes.",
}


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 120, limits: bool = False) -> dict[str, Any]:
    started = time.time()

    def preexec():
        os.setsid()
        if limits:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (max(60, timeout), max(60, timeout + 5)))
                resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000_000, 1_000_000_000))
                resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
                resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
            except Exception:
                pass

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        preexec_fn=preexec if os.name == "posix" else None,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        out, _ = proc.communicate()
    duration = round(time.time() - started, 2)
    if len(out) > MAX_LOG_CHARS:
        out = out[-MAX_LOG_CHARS:]
        out = "[log truncated to last 120k chars]\n" + out
    return {
        "cmd": cmd,
        "code": -9 if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "output": out,
    }


def repo_slug(repo_url: str) -> str:
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", repo_url)
    if not m:
        raise ValueError("Only public GitHub repository URLs are supported in v0.1")
    return f"{m.group(1)}/{m.group(2)}"


def folder_size_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return round(total / 1024 / 1024, 1)


def clone_repo(repo_url: str, branch: str | None, destination: Path):
    cmd = ["git", "clone", "--depth=1", "--single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(destination)]
    result = run(cmd, timeout=120)
    if result["code"] != 0:
        raise RuntimeError("git clone failed:\n" + result["output"][-5000:])
    size = folder_size_mb(destination)
    if size > MAX_REPO_MB:
        raise RuntimeError(f"Repository is {size} MB after shallow clone; v0.1 limit is {MAX_REPO_MB} MB")
    return size


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def static_analysis(repo: Path) -> dict[str, Any]:
    package_path = repo / "package.json"
    lock_path = repo / "package-lock.json"
    if not package_path.exists():
        raise RuntimeError("No package.json found at repository root. v0.1 scans root-level Node projects only.")

    package = load_json(package_path)
    lock = load_json(lock_path) if lock_path.exists() else {}
    deps = {}
    for bucket in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, version in (package.get(bucket) or {}).items():
            deps.setdefault(name, {"declared": version, "buckets": []})["buckets"].append(bucket)

    native_flags = []
    for name, why in KNOWN_NATIVE_OR_RISKY.items():
        if name in deps or package_in_lock(lock, name):
            native_flags.append({"package": name, "why": why, "declared": deps.get(name, {}).get("declared")})

    scripts = package.get("scripts") or {}
    engines = package.get("engines") or {}
    package_manager = package.get("packageManager")
    node_gyp_versions = find_versions_in_lock(lock, "node-gyp")
    deasync_versions = find_versions_in_lock(lock, "deasync")

    config_files = [
        name for name in [
            "Dockerfile", "railway.json", "railway.toml", "nixpacks.toml", "Procfile", ".nvmrc", ".node-version", "volta.json"
        ] if (repo / name).exists()
    ]

    risk = 0
    reasons = []
    if native_flags:
        risk += min(40, 8 * len(native_flags))
        reasons.append(f"{len(native_flags)} native/runtime-sensitive package(s) detected")
    if node_gyp_versions:
        old = [v for v in node_gyp_versions if major_of(v) and major_of(v) < 10]
        if old:
            risk += 25
            reasons.append("node-gyp <10 appears in the lockfile; Python 3.12+ compatibility can be a problem")
    if not engines.get("node"):
        risk += 10
        reasons.append("No Node engine range is pinned in package.json")
    if not lock_path.exists():
        risk += 10
        reasons.append("No package-lock.json found; installs may drift")
    if "build" not in scripts:
        reasons.append("No npm build script found; full scan will only validate install")
    risk = min(100, risk)

    return {
        "name": package.get("name") or repo.name,
        "version": package.get("version"),
        "engines": engines,
        "package_manager": package_manager,
        "scripts": sorted(scripts.keys()),
        "dependency_count": len(deps),
        "lockfile": "package-lock.json" if lock_path.exists() else None,
        "config_files": config_files,
        "native_or_risky_dependencies": native_flags,
        "node_gyp_versions": node_gyp_versions,
        "deasync_versions": deasync_versions,
        "risk_score": risk,
        "risk_reasons": reasons,
    }


def package_in_lock(lock: dict[str, Any], name: str) -> bool:
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if isinstance(packages, dict) and f"node_modules/{name}" in packages:
        return True
    deps = lock.get("dependencies") if isinstance(lock, dict) else None
    return isinstance(deps, dict) and name in deps


def find_versions_in_lock(lock: dict[str, Any], name: str) -> list[str]:
    versions = set()
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if path.endswith(f"node_modules/{name}") and isinstance(meta, dict) and meta.get("version"):
                versions.add(str(meta["version"]))
    deps = lock.get("dependencies") if isinstance(lock, dict) else None
    if isinstance(deps, dict):
        meta = deps.get(name)
        if isinstance(meta, dict) and meta.get("version"):
            versions.add(str(meta["version"]))
    return sorted(versions)


def major_of(version: str) -> int | None:
    m = re.search(r"(\d+)", str(version))
    return int(m.group(1)) if m else None


def get_node_release(major: int) -> tuple[str, str]:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=20) as resp:
        releases = json.loads(resp.read().decode("utf-8"))
    arch = platform.machine().lower()
    platform_key = "linux-x64" if arch in {"x86_64", "amd64"} else "linux-arm64" if arch in {"aarch64", "arm64"} else None
    if not platform_key:
        raise RuntimeError(f"Unsupported host architecture for Node runtime download: {arch}")
    for item in releases:
        if item.get("version", "").startswith(f"v{major}.") and platform_key in (item.get("files") or []):
            version = item["version"]
            return version, platform_key
    raise RuntimeError(f"No Node {major} Linux binary found in nodejs.org index")


def ensure_node(major: int, runtime_root: Path) -> Path:
    version, platform_key = get_node_release(major)
    target = runtime_root / f"node-{version}-{platform_key}"
    node_bin = target / "bin" / "node"
    if node_bin.exists():
        return target

    runtime_root.mkdir(parents=True, exist_ok=True)
    archive = runtime_root / f"node-{version}-{platform_key}.tar.xz"
    url = f"https://nodejs.org/dist/{version}/node-{version}-{platform_key}.tar.xz"
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:xz") as tar:
        safe_extract(tar, runtime_root)
    archive.unlink(missing_ok=True)
    if not node_bin.exists():
        raise RuntimeError(f"Downloaded Node {version} but binary was not found")
    return target


def safe_extract(tar: tarfile.TarFile, path: Path):
    base = path.resolve()
    for member in tar.getmembers():
        dest = (path / member.name).resolve()
        if base not in dest.parents and dest != base:
            raise RuntimeError("Unsafe path in Node tar archive")
    tar.extractall(path)


def sanitized_env(node_dir: Path, workspace: Path) -> dict[str, str]:
    allowed = {
        "PATH": f"{node_dir / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(workspace / ".home"),
        "TMPDIR": str(workspace / ".tmp"),
        "CI": "1",
        "NODE_ENV": "production",
        "NPM_CONFIG_CACHE": str(workspace / ".npm-cache"),
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "npm_config_update_notifier": "false",
    }
    Path(allowed["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(allowed["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return allowed


def classify_failure(output: str) -> list[dict[str, str]]:
    patterns = [
        (r"No module named ['\"]distutils['\"]", "python_distutils_removed", "Python 3.12+ removed distutils; older node-gyp versions commonly fail here."),
        (r"node-gyp", "node_gyp", "Native addon compilation failed through node-gyp."),
        (r"Unsupported engine|EBADENGINE", "engine_mismatch", "A package declares an incompatible Node/npm engine range."),
        (r"ERR_OSSL_EVP_UNSUPPORTED", "openssl_compat", "Likely Node/OpenSSL compatibility issue."),
        (r"NODE_MODULE_VERSION", "node_abi", "Native addon ABI does not match this Node runtime."),
        (r"Cannot find module", "missing_module", "Build/runtime could not resolve a required module."),
        (r"ERESOLVE", "dependency_resolution", "npm dependency resolution conflict."),
        (r"ETIMEDOUT|ECONNRESET|ENETUNREACH", "network", "Network failure during install/build; may not be a compatibility bug."),
    ]
    hits = []
    for pattern, code, explanation in patterns:
        if re.search(pattern, output, flags=re.IGNORECASE):
            hits.append({"code": code, "explanation": explanation})
    return hits


def full_build_matrix(source_repo: Path, node_majors: list[int], work_root: Path, runtime_root: Path) -> list[dict[str, Any]]:
    results = []
    for major in node_majors:
        node_dir = ensure_node(major, runtime_root)
        runtime_version = run([str(node_dir / "bin" / "node"), "--version"], timeout=20)["output"].strip()
        workspace = Path(tempfile.mkdtemp(prefix=f"wid-node{major}-", dir=work_root))
        repo_copy = workspace / "repo"
        shutil.copytree(source_repo, repo_copy, ignore=shutil.ignore_patterns(".git", "node_modules", ".next", "dist", "build"))
        env = sanitized_env(node_dir, workspace)

        lock = (repo_copy / "package-lock.json").exists()
        install_cmd = [str(node_dir / "bin" / "npm"), "ci"] if lock else [str(node_dir / "bin" / "npm"), "install"]
        install = run(install_cmd, cwd=repo_copy, env=env, timeout=INSTALL_TIMEOUT, limits=True)
        build = None
        pkg = load_json(repo_copy / "package.json")
        has_build = "build" in (pkg.get("scripts") or {})
        if install["code"] == 0 and has_build:
            build = run([str(node_dir / "bin" / "npm"), "run", "build"], cwd=repo_copy, env=env, timeout=BUILD_TIMEOUT, limits=True)

        status = "pass"
        fail_output = ""
        if install["code"] != 0:
            status = "fail_install"
            fail_output = install["output"]
        elif build and build["code"] != 0:
            status = "fail_build"
            fail_output = build["output"]

        results.append(
            {
                "node_major": major,
                "node_version": runtime_version,
                "status": status,
                "install": install,
                "build": build,
                "failure_signatures": classify_failure(fail_output),
            }
        )
        shutil.rmtree(workspace, ignore_errors=True)
    return results


def summarize(static: dict[str, Any], matrix: list[dict[str, Any]] | None) -> str:
    if not matrix:
        return f"Static risk score {static['risk_score']}/100; {len(static['native_or_risky_dependencies'])} runtime-sensitive package(s) detected."
    passed = [r["node_major"] for r in matrix if r["status"] == "pass"]
    failed = [r["node_major"] for r in matrix if r["status"] != "pass"]
    if failed and passed:
        return f"Compatibility split detected: passes Node {', '.join(map(str, passed))}; fails Node {', '.join(map(str, failed))}."
    if failed:
        return f"Build failed on all tested runtimes: Node {', '.join(map(str, failed))}."
    return f"Build passed on all tested runtimes: Node {', '.join(map(str, passed))}."


def scan_repo(repo_url: str, branch: str | None, mode: str, node_majors: list[int], work_root: Path, runtime_root: Path) -> dict[str, Any]:
    slug = repo_slug(repo_url)
    session = Path(tempfile.mkdtemp(prefix="wid-clone-", dir=work_root))
    clone_dir = session / "repo"
    try:
        size_mb = clone_repo(repo_url, branch, clone_dir)
        static = static_analysis(clone_dir)
        matrix = full_build_matrix(clone_dir, node_majors, work_root, runtime_root) if mode == "full" else None
        return {
            "repo": slug,
            "repo_url": repo_url,
            "branch": branch,
            "mode": mode,
            "repo_size_mb": size_mb,
            "static": static,
            "matrix": matrix,
            "summary": summarize(static, matrix),
            "generated_at": int(time.time()),
        }
    finally:
        shutil.rmtree(session, ignore_errors=True)


def scan_fixture(fixture_dir: Path, node_majors: list[int], work_root: Path, runtime_root: Path) -> dict[str, Any]:
    static = static_analysis(fixture_dir)
    matrix = full_build_matrix(fixture_dir, node_majors, work_root, runtime_root)
    return {
        "repo": "bundled/hello-node",
        "repo_url": "local fixture",
        "branch": None,
        "mode": "full",
        "repo_size_mb": folder_size_mb(fixture_dir),
        "static": static,
        "matrix": matrix,
        "summary": summarize(static, matrix),
        "generated_at": int(time.time()),
    }
