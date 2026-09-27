from __future__ import annotations

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
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

MAX_LOG_CHARS = 120_000
MAX_REPO_MB = int(os.getenv("MAX_REPO_MB", "250"))
INSTALL_TIMEOUT = int(os.getenv("INSTALL_TIMEOUT_SECONDS", "480"))
BUILD_TIMEOUT = int(os.getenv("BUILD_TIMEOUT_SECONDS", "480"))
REGISTRY_TIMEOUT = int(os.getenv("REGISTRY_TIMEOUT_SECONDS", "8"))
REGISTRY_MAX_PACKAGES = int(os.getenv("STATIC_REGISTRY_MAX_PACKAGES", "160"))
REGISTRY_MAX_DEPTH = int(os.getenv("STATIC_REGISTRY_MAX_DEPTH", "4"))
BUILD_UID = int(os.getenv("BUILD_UID", "65534"))
BUILD_GID = int(os.getenv("BUILD_GID", "65534"))

KNOWN_NATIVE_OR_RISKY = {
    "deasync": "Native addon; older releases can break across Node/Python/node-gyp changes.",
    "node-sass": "Deprecated native addon; historically sensitive to Node ABI changes.",
    "bcrypt": "Native addon in many versions; runtime compatibility depends on prebuilt binaries/toolchain.",
    "bcryptjs": "Pure-JS bcrypt implementation; lower runtime risk than native bcrypt, retained for comparison.",
    "sharp": "Native dependency; usually ships prebuilds but runtime/platform support matters.",
    "canvas": "Native dependency; may require system libraries and compilation.",
    "sqlite3": "Native addon; prebuilt binary availability varies by runtime/platform.",
    "better-sqlite3": "Native addon; Node ABI compatibility matters.",
    "grpc": "Legacy native package; modern projects usually use @grpc/grpc-js.",
    "fsevents": "Platform-specific native dependency (macOS only).",
    "ffi-napi": "Native FFI addon; can be sensitive to Node ABI/toolchain changes.",
    "ref-napi": "Native FFI support package; can be sensitive to Node ABI/toolchain changes.",
    "serialport": "Often contains native bindings or prebuilt binaries tied to runtime/platform support.",
    "usb": "Native USB bindings; requires compatible prebuilds/toolchain/system libraries.",
    "leveldown": "Native LevelDB binding; runtime/platform compatibility matters.",
    "lmdb": "Native database binding; typically relies on prebuilt binaries or compilation.",
    "isolated-vm": "Native V8 addon tightly coupled to Node/V8 versions.",
    "cpu-features": "Native addon used by some SSH/crypto stacks.",
}

PURE_JS_INFORMATIONAL = {"bcryptjs"}

_registry_cache: dict[str, dict[str, Any] | None] = {}


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 120, limits: bool = False, drop_privileges: bool = False) -> dict[str, Any]:
    started = time.time()

    def preexec():
        os.setsid()
        if drop_privileges and os.geteuid() == 0:
            try:
                os.setgroups([])
                os.setgid(BUILD_GID)
                os.setuid(BUILD_UID)
            except Exception:
                pass
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
        raise ValueError("Only public GitHub repository URLs are supported")
    return f"{m.group(1)}/{m.group(2)}"


def folder_size_mb(path: Path) -> float:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules"}]
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
        raise RuntimeError(f"Repository is {size} MB after shallow clone; limit is {MAX_REPO_MB} MB")
    return size


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def discover_node_projects(repo: Path, max_depth: int = 4) -> list[str]:
    projects = []
    ignored = {".git", "node_modules", ".next", "dist", "build", "coverage", ".cache", "vendor"}
    for root, dirs, files in os.walk(repo):
        rel = Path(root).relative_to(repo)
        if len(rel.parts) > max_depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]
        if "package.json" in files:
            projects.append("." if str(rel) == "." else rel.as_posix())
    return sorted(set(projects), key=lambda p: (0 if p == "." else len(Path(p).parts), p))


def choose_project(repo: Path, requested: str | None = None) -> tuple[Path | None, list[str], str | None]:
    projects = discover_node_projects(repo)
    if requested:
        clean = requested.strip().strip("/") or "."
        target = repo if clean == "." else repo / clean
        target = target.resolve()
        repo_resolved = repo.resolve()
        if repo_resolved != target and repo_resolved not in target.parents:
            raise RuntimeError("Project path must stay inside the repository")
        if not (target / "package.json").exists():
            raise RuntimeError(f"No package.json found at project path: {clean}")
        return target, projects, clean
    if (repo / "package.json").exists():
        return repo, projects, "."
    if projects:
        chosen = projects[0]
        return repo / chosen, projects, chosen
    return None, [], None


def declared_dependencies(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    deps: dict[str, dict[str, Any]] = {}
    for bucket in ("dependencies", "optionalDependencies", "devDependencies", "peerDependencies"):
        values = package.get(bucket) or {}
        if not isinstance(values, dict):
            continue
        for name, version in values.items():
            item = deps.setdefault(name, {"declared": str(version), "buckets": []})
            item["buckets"].append(bucket)
    return deps


def package_name_from_lock_path(path: str) -> str | None:
    if not path or "node_modules/" not in path:
        return None
    tail = path.rsplit("node_modules/", 1)[-1]
    if tail.startswith("@"):
        parts = tail.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return tail.split("/", 1)[0]


def walk_v1_dependencies(tree: dict[str, Any], prefix: list[str] | None = None):
    prefix = prefix or []
    for name, meta in (tree or {}).items():
        if not isinstance(meta, dict):
            continue
        version = str(meta.get("version") or "")
        path = prefix + [f"{name}@{version}" if version else name]
        yield name, version, path
        nested = meta.get("dependencies")
        if isinstance(nested, dict):
            yield from walk_v1_dependencies(nested, path)


def lock_inventory(lock: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    inventory: dict[str, list[dict[str, Any]]] = {}
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if not isinstance(meta, dict):
                continue
            name = meta.get("name") or package_name_from_lock_path(str(path))
            if not name:
                continue
            inventory.setdefault(str(name), []).append({
                "version": str(meta.get("version") or ""),
                "lock_path": str(path),
                "source": "lockfile",
            })
    deps = lock.get("dependencies") if isinstance(lock, dict) else None
    if isinstance(deps, dict):
        for name, version, path in walk_v1_dependencies(deps):
            inventory.setdefault(name, []).append({"version": version, "path": path, "source": "lockfile-v1"})
    return inventory


def find_versions_in_inventory(inventory: dict[str, list[dict[str, Any]]], name: str) -> list[str]:
    return sorted({str(x.get("version") or "") for x in inventory.get(name, []) if x.get("version")})


def parse_version(value: str) -> tuple[int, int, int, str] | None:
    v = str(value).strip().lstrip("v")
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]([0-9A-Za-z.-]+))?", v)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), m.group(4) or ""


def stable_versions(metadata: dict[str, Any]) -> list[tuple[tuple[int, int, int, str], str]]:
    out = []
    for version in (metadata.get("versions") or {}).keys():
        parsed = parse_version(version)
        if parsed and not parsed[3]:
            out.append((parsed, version))
    return sorted(out, key=lambda x: x[0][:3])


def cmp_core(v: tuple[int, int, int, str], target: tuple[int, int, int, str]) -> int:
    a, b = v[:3], target[:3]
    return (a > b) - (a < b)


def comparator_match(version: tuple[int, int, int, str], token: str) -> bool:
    token = token.strip()
    if not token or token in {"*", "latest"}:
        return True
    m = re.fullmatch(r"(>=|<=|>|<|=)?\s*v?(\d+)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?", token)
    if not m:
        return True
    op = m.group(1) or "="
    parts = [m.group(2), m.group(3), m.group(4)]
    wildcard = any(p in {None, "x", "X", "*"} for p in parts[1:])
    nums = [int(parts[0]), int(parts[1]) if parts[1] and parts[1].isdigit() else 0, int(parts[2]) if parts[2] and parts[2].isdigit() else 0]
    target = (nums[0], nums[1], nums[2], "")
    if wildcard and op == "=":
        if parts[1] in {None, "x", "X", "*"}:
            return version[0] == nums[0]
        return version[0] == nums[0] and version[1] == nums[1]
    c = cmp_core(version, target)
    return {">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0, "=": c == 0}.get(op, True)


def spec_matches(version: tuple[int, int, int, str], spec: str) -> bool:
    raw = str(spec or "*").strip()
    if raw.startswith("npm:"):
        raw = raw.rsplit("@", 1)[-1] if "@" in raw[4:] else "*"
    if raw in {"", "*", "latest"}:
        return True
    if any(raw.startswith(prefix) for prefix in ("git+", "github:", "http:", "https:", "file:", "link:", "workspace:")):
        return False
    for alternative in raw.split("||"):
        s = alternative.strip()
        if not s:
            continue
        if s.startswith("^"):
            base = parse_version(s[1:])
            if not base:
                continue
            if base[0] > 0:
                upper = (base[0] + 1, 0, 0, "")
            elif base[1] > 0:
                upper = (0, base[1] + 1, 0, "")
            else:
                upper = (0, 0, base[2] + 1, "")
            if cmp_core(version, base) >= 0 and cmp_core(version, upper) < 0:
                return True
            continue
        if s.startswith("~"):
            base = parse_version(s[1:])
            if not base:
                continue
            upper = (base[0], base[1] + 1, 0, "")
            if cmp_core(version, base) >= 0 and cmp_core(version, upper) < 0:
                return True
            continue
        tokens = [t for t in re.split(r"\s+", s) if t]
        if tokens and all(comparator_match(version, t) for t in tokens):
            return True
    return False


def registry_metadata(name: str) -> dict[str, Any] | None:
    if name in _registry_cache:
        return _registry_cache[name]
    try:
        encoded = urllib.parse.quote(name, safe="")
        req = urllib.request.Request(f"https://registry.npmjs.org/{encoded}", headers={"User-Agent": "WillItDeploy/0.2"})
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _registry_cache[name] = data if isinstance(data, dict) else None
    except Exception:
        _registry_cache[name] = None
    return _registry_cache[name]


def resolve_registry_version(name: str, spec: str) -> tuple[str | None, dict[str, Any] | None]:
    actual_name = name
    actual_spec = spec
    if str(spec).startswith("npm:"):
        alias = str(spec)[4:]
        if alias.startswith("@"):
            idx = alias.rfind("@")
            if idx > 0:
                actual_name, actual_spec = alias[:idx], alias[idx + 1:]
            else:
                actual_name, actual_spec = alias, "*"
        elif "@" in alias:
            actual_name, actual_spec = alias.rsplit("@", 1)
        else:
            actual_name, actual_spec = alias, "*"
    metadata = registry_metadata(actual_name)
    if not metadata:
        return None, None
    versions = stable_versions(metadata)
    candidates = [raw for parsed, raw in versions if spec_matches(parsed, actual_spec)]
    if not candidates:
        latest = (metadata.get("dist-tags") or {}).get("latest")
        if latest and actual_spec in {"", "*", "latest"}:
            candidates = [latest]
    if not candidates:
        return None, None
    version = candidates[-1]
    meta = (metadata.get("versions") or {}).get(version)
    return version, meta if isinstance(meta, dict) else None


def registry_risky_paths(package: dict[str, Any]) -> dict[str, Any]:
    seeds = []
    for bucket in ("dependencies", "optionalDependencies", "devDependencies"):
        values = package.get(bucket) or {}
        if isinstance(values, dict):
            priority = 0 if bucket in {"dependencies", "optionalDependencies"} else 1
            for name, spec in values.items():
                seeds.append((priority, name, str(spec), [f"ROOT ({bucket})"]))
    seeds.sort(key=lambda x: x[0])
    q = deque((name, spec, path, 1) for _, name, spec, path in seeds)
    seen: set[tuple[str, str]] = set()
    hits: dict[str, list[dict[str, Any]]] = {}
    visited = 0
    errors = 0
    truncated = False

    while q and visited < REGISTRY_MAX_PACKAGES:
        name, spec, parent_path, depth = q.popleft()
        key = (name, spec)
        if key in seen:
            continue
        seen.add(key)
        visited += 1
        version, meta = resolve_registry_version(name, spec)
        if not version or not meta:
            errors += 1
            continue
        step = f"{name}@{version}"
        path = parent_path + [step]
        if name in KNOWN_NATIVE_OR_RISKY:
            hits.setdefault(name, []).append({"version": version, "path": path, "source": "npm-registry"})
        if depth >= REGISTRY_MAX_DEPTH:
            continue
        children = {}
        for bucket in ("dependencies", "optionalDependencies"):
            vals = meta.get(bucket) or {}
            if isinstance(vals, dict):
                children.update({str(k): str(v) for k, v in vals.items()})
        for child, child_spec in children.items():
            q.append((child, child_spec, path, depth + 1))

    if q:
        truncated = True
    return {
        "hits": hits,
        "visited_packages": visited,
        "lookup_errors": errors,
        "truncated": truncated,
        "max_depth": REGISTRY_MAX_DEPTH,
        "max_packages": REGISTRY_MAX_PACKAGES,
    }


def read_text(path: Path, max_chars: int = 30_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def detect_runtime_pins(project: Path, repo: Path, package: dict[str, Any]) -> list[dict[str, str]]:
    pins: list[dict[str, str]] = []

    engines = package.get("engines") or {}
    if isinstance(engines, dict) and engines.get("node"):
        pins.append({"source": "package.json engines.node", "value": str(engines["node"])})
    volta = package.get("volta") or {}
    if isinstance(volta, dict) and volta.get("node"):
        pins.append({"source": "package.json volta.node", "value": str(volta["node"])})

    candidates = []
    for base in {project, repo}:
        candidates += [
            (base / ".nvmrc", ".nvmrc"),
            (base / ".node-version", ".node-version"),
            (base / ".tool-versions", ".tool-versions"),
            (base / "volta.json", "volta.json"),
            (base / "Dockerfile", "Dockerfile"),
            (base / "railway.toml", "railway.toml"),
            (base / "nixpacks.toml", "nixpacks.toml"),
            (base / "railway.json", "railway.json"),
        ]
    seen_files = set()
    for path, label in candidates:
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen_files or not path.exists():
            continue
        seen_files.add(key)
        text = read_text(path)
        if label in {".nvmrc", ".node-version"}:
            value = text.strip().splitlines()[0] if text.strip() else ""
            if value:
                pins.append({"source": label, "value": value})
        elif label == ".tool-versions":
            m = re.search(r"(?mi)^nodejs\s+([^\s]+)", text)
            if m:
                pins.append({"source": label, "value": m.group(1)})
        elif label == "volta.json":
            data = load_json(path)
            if data.get("node"):
                pins.append({"source": label, "value": str(data["node"])})
        elif label == "Dockerfile":
            for value in re.findall(r"(?mi)^\s*FROM\s+node:([^\s@]+)", text):
                pins.append({"source": "Dockerfile FROM node", "value": value})
        else:
            for value in re.findall(r"RAILPACK_NODE_VERSION\s*[=:]\s*[\"']?([^\s\"',}]+)", text):
                pins.append({"source": f"{label} RAILPACK_NODE_VERSION", "value": value})
            for value in re.findall(r"NIXPACKS_NODE_VERSION\s*[=:]\s*[\"']?([^\s\"',}]+)", text):
                pins.append({"source": f"{label} NIXPACKS_NODE_VERSION", "value": value})

    dedup = []
    used = set()
    for pin in pins:
        key = (pin["source"], pin["value"])
        if key not in used:
            used.add(key)
            dedup.append(pin)
    return dedup


def detect_package_manager(project: Path, package: dict[str, Any]) -> dict[str, Any]:
    pm = package.get("packageManager")
    if isinstance(pm, str) and pm:
        name = pm.split("@", 1)[0]
        return {"name": name, "source": "package.json packageManager", "value": pm}
    if (project / "pnpm-lock.yaml").exists():
        return {"name": "pnpm", "source": "pnpm-lock.yaml", "value": None}
    if (project / "yarn.lock").exists():
        return {"name": "yarn", "source": "yarn.lock", "value": None}
    if (project / "package-lock.json").exists() or (project / "npm-shrinkwrap.json").exists():
        return {"name": "npm", "source": "npm lockfile", "value": None}
    return {"name": "npm", "source": "default", "value": None}


def static_analysis(repo: Path, project: Path | None, discovered_projects: list[str], selected_project: str | None) -> dict[str, Any]:
    if project is None:
        return {
            "node_project_found": False,
            "selected_project": None,
            "discovered_projects": discovered_projects,
            "risk_score": 0,
            "risk_reasons": ["No package.json discovered in the repository within scan depth."],
            "native_or_risky_dependencies": [],
            "dependency_count": 0,
            "lockfile": None,
            "runtime_pins": [],
            "package_manager": None,
            "registry_resolution": None,
            "node_gyp_versions": [],
            "deasync_versions": [],
            "scripts": [],
            "config_files": [],
        }

    package_path = project / "package.json"
    package = load_json(package_path)
    deps = declared_dependencies(package)

    lock_path = None
    for name in ("package-lock.json", "npm-shrinkwrap.json"):
        if (project / name).exists():
            lock_path = project / name
            break
    lock = load_json(lock_path) if lock_path else {}
    inventory = lock_inventory(lock) if lock else {}

    native_flags = []
    for name, why in KNOWN_NATIVE_OR_RISKY.items():
        versions = find_versions_in_inventory(inventory, name)
        if name in deps or versions:
            native_flags.append({
                "package": name,
                "why": why,
                "declared": deps.get(name, {}).get("declared"),
                "versions": versions,
                "source": "direct/lockfile",
                "paths": [],
                "informational": name in PURE_JS_INFORMATIONAL,
            })

    registry_resolution = None
    # Lockfiles are stronger evidence. If none exists, query npm metadata recursively without executing package scripts.
    if not lock_path:
        registry_resolution = registry_risky_paths(package)
        for name, paths in registry_resolution["hits"].items():
            if any(x["package"] == name for x in native_flags):
                existing = next(x for x in native_flags if x["package"] == name)
                existing["paths"].extend([x.get("path") for x in paths if x.get("path")])
                existing["versions"] = sorted(set(existing.get("versions", []) + [x["version"] for x in paths if x.get("version")]))
            else:
                native_flags.append({
                    "package": name,
                    "why": KNOWN_NATIVE_OR_RISKY[name],
                    "declared": None,
                    "versions": sorted({x["version"] for x in paths if x.get("version")}),
                    "source": "npm-registry-transitive",
                    "paths": [x.get("path") for x in paths if x.get("path")],
                    "informational": name in PURE_JS_INFORMATIONAL,
                })

    scripts = package.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}
    runtime_pins = detect_runtime_pins(project, repo, package)
    package_manager = detect_package_manager(project, package)
    node_gyp_versions = find_versions_in_inventory(inventory, "node-gyp")
    deasync_versions = find_versions_in_inventory(inventory, "deasync")

    config_names = [
        "Dockerfile", "railway.json", "railway.toml", "nixpacks.toml", "Procfile",
        ".nvmrc", ".node-version", ".tool-versions", "volta.json"
    ]
    config_files = sorted({name for name in config_names if (project / name).exists() or (repo / name).exists()})

    risk = 0
    reasons = []
    real_native_flags = [x for x in native_flags if not x.get("informational")]
    if real_native_flags:
        risk += min(45, 9 * len(real_native_flags))
        transitive = [x for x in real_native_flags if x.get("source") == "npm-registry-transitive"]
        reasons.append(f"{len(real_native_flags)} native/runtime-sensitive package(s) detected")
        if transitive:
            reasons.append(f"{len(transitive)} risky package(s) were only visible transitively via npm metadata")
    if node_gyp_versions:
        old = [v for v in node_gyp_versions if major_of(v) is not None and major_of(v) < 10]
        if old:
            risk += 25
            reasons.append("node-gyp <10 appears in the lockfile; Python 3.12+ compatibility can be a problem")
    if not runtime_pins:
        risk += 10
        reasons.append("No Node runtime pin detected (engines/Volta/nvm/node-version/Docker/Railpack)")
    if not lock_path:
        risk += 10
        reasons.append("No npm lockfile found; fresh installs may drift")
    if package_manager["name"] != "npm":
        reasons.append(f"Package manager is {package_manager['name']}; v0.2 full-build execution is npm-first")
    if "build" not in scripts:
        reasons.append("No npm build script found; full scan will validate install only")
    if len(discovered_projects) > 1:
        reasons.append(f"Repository contains {len(discovered_projects)} Node project(s); scanning {selected_project}")
    risk = min(100, risk)

    return {
        "node_project_found": True,
        "selected_project": selected_project,
        "discovered_projects": discovered_projects,
        "name": package.get("name") or project.name,
        "version": package.get("version"),
        "engines": package.get("engines") or {},
        "volta": package.get("volta") or {},
        "runtime_pins": runtime_pins,
        "package_manager": package_manager,
        "scripts": sorted(scripts.keys()),
        "dependency_count": len(deps),
        "lockfile": lock_path.name if lock_path else None,
        "config_files": config_files,
        "native_or_risky_dependencies": sorted(native_flags, key=lambda x: (x.get("informational", False), x["package"])),
        "node_gyp_versions": node_gyp_versions,
        "deasync_versions": deasync_versions,
        "risk_score": risk,
        "risk_reasons": reasons,
        "registry_resolution": registry_resolution,
    }


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
            return item["version"], platform_key
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
    try:
        archive.unlink(missing_ok=True)
        # Remove a previous partial extraction before retrying.
        if target.exists() and not node_bin.exists():
            shutil.rmtree(target, ignore_errors=True)
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive, "r:xz") as tar:
            safe_extract(tar, runtime_root)
    except Exception:
        archive.unlink(missing_ok=True)
        if target.exists() and not node_bin.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        archive.unlink(missing_ok=True)
    if not node_bin.exists():
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Downloaded Node {version} but binary was not found")
    return target



def validate_npm_versions(values: list[str] | None) -> list[str]:
    """Normalize an npm toolchain matrix. 'bundled' uses the npm shipped with Node."""
    values = values or ["bundled"]
    out: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if value == "bundled":
            if value not in out:
                out.append(value)
            continue
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError(f"Invalid npm version: {value}")
        if value not in out:
            out.append(value)
    if not out:
        out = ["bundled"]
    if len(out) > 3:
        raise ValueError("Maximum 3 npm versions per scan")
    return out


def ensure_npm(version: str, npm_root: Path) -> Path:
    """Download an exact npm CLI release once and reuse it across Node runtimes."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Invalid npm version: {version}")
    target = npm_root / f"npm-{version}"
    cli = target / "bin" / "npm-cli.js"
    if cli.exists():
        return target

    npm_root.mkdir(parents=True, exist_ok=True)
    archive = npm_root / f"npm-{version}.tgz"
    temp_extract = Path(tempfile.mkdtemp(prefix=f"npm-{version}-", dir=npm_root))
    url = f"https://registry.npmjs.org/npm/-/npm-{version}.tgz"
    try:
        archive.unlink(missing_ok=True)
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive, "r:gz") as tar:
            safe_extract(tar, temp_extract)
        package_dir = temp_extract / "package"
        if not (package_dir / "bin" / "npm-cli.js").exists():
            raise RuntimeError(f"Downloaded npm {version} but npm-cli.js was not found")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(package_dir), str(target))
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(temp_extract, ignore_errors=True)

    if not cli.exists():
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"npm {version} extraction did not produce a usable CLI")
    return target


def npm_cli_for(node_dir: Path, npm_spec: str, npm_root: Path) -> tuple[list[str], str, str]:
    node_bin = str(node_dir / "bin" / "node")
    if npm_spec == "bundled":
        return [str(node_dir / "bin" / "npm")], "bundled", "bundled"
    npm_dir = ensure_npm(npm_spec, npm_root)
    return [node_bin, str(npm_dir / "bin" / "npm-cli.js")], npm_spec, "pinned"

def safe_extract(tar: tarfile.TarFile, path: Path):
    base = path.resolve()
    for member in tar.getmembers():
        dest = (path / member.name).resolve()
        if base not in dest.parents and dest != base:
            raise RuntimeError("Unsafe path in Node tar archive")
    tar.extractall(path)


def sanitized_env(node_dir: Path, workspace: Path) -> dict[str, str]:
    # Do NOT set NODE_ENV=production: build scripts commonly require devDependencies.
    allowed = {
        "PATH": f"{node_dir / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(workspace / ".home"),
        "TMPDIR": str(workspace / ".tmp"),
        "CI": "1",
        "NPM_CONFIG_CACHE": str(workspace / ".npm-cache"),
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "npm_config_update_notifier": "false",
    }
    Path(allowed["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(allowed["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return allowed



def chown_tree(path: Path, uid: int = BUILD_UID, gid: int = BUILD_GID):
    if os.geteuid() != 0:
        return
    for root, dirs, files in os.walk(path):
        try:
            os.chown(root, uid, gid)
        except OSError:
            pass
        for name in dirs:
            try:
                os.chown(Path(root) / name, uid, gid)
            except OSError:
                pass
        for name in files:
            try:
                os.chown(Path(root) / name, uid, gid)
            except OSError:
                pass


def classify_failure(output: str) -> list[dict[str, str]]:
    patterns = [
        (r"No module named ['\"]distutils['\"]", "python_distutils_removed", "Python 3.12+ removed distutils; older node-gyp versions commonly fail here."),
        (r"node-gyp|gyp ERR", "node_gyp", "Native addon compilation failed through node-gyp."),
        (r"Unsupported engine|EBADENGINE", "engine_mismatch", "A package declares an incompatible Node/npm engine range."),
        (r"ERR_OSSL_EVP_UNSUPPORTED", "openssl_compat", "Likely Node/OpenSSL compatibility issue."),
        (r"NODE_MODULE_VERSION|Module did not self-register", "node_abi", "Native addon ABI does not match this Node runtime."),
        (r"prebuild-install.*warn|No prebuilt binaries found", "missing_prebuild", "No compatible prebuilt native binary was available; compilation may be required."),
        (r"Cannot find module|ERR_MODULE_NOT_FOUND", "missing_module", "Build/runtime could not resolve a required module."),
        (r"ERESOLVE", "dependency_resolution", "npm dependency resolution conflict."),
        (r"npm ci.*package-lock|can only install packages when your package.json and package-lock", "lockfile_mismatch", "package.json and lockfile are not in sync for npm ci."),
        (r"GLIBC_|not found: make|not found: g\+\+|fatal error: .*\.h: No such file", "system_toolchain", "Native build appears to require a missing compiler/system library."),
        (r"SyntaxError: Unexpected token|Unexpected token ['\"]?\?\?", "runtime_syntax", "Runtime may be too old/new for syntax emitted or consumed by a dependency."),
        (r"ETIMEDOUT|ECONNRESET|ENETUNREACH|EAI_AGAIN", "network", "Network failure during install/build; may not be a compatibility bug."),
    ]
    hits = []
    for pattern, code, explanation in patterns:
        if re.search(pattern, output or "", flags=re.IGNORECASE | re.MULTILINE):
            hits.append({"code": code, "explanation": explanation})
    return hits


def full_build_matrix(
    source_repo: Path,
    selected_project: str,
    node_majors: list[int],
    work_root: Path,
    runtime_root: Path,
    npm_versions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run a controlled Node x npm matrix so only one toolchain variable changes at a time."""
    results: list[dict[str, Any]] = []
    npm_specs = validate_npm_versions(npm_versions)
    npm_root = runtime_root.parent / "npm-tools"

    for major in node_majors:
        node_dir = ensure_node(major, runtime_root)
        runtime_version = run([str(node_dir / "bin" / "node"), "--version"], timeout=20)["output"].strip()

        for npm_spec in npm_specs:
            safe_spec = npm_spec.replace(".", "-")
            workspace = Path(tempfile.mkdtemp(prefix=f"wid-node{major}-npm{safe_spec}-", dir=work_root))
            repo_copy = workspace / "repo"
            try:
                shutil.copytree(source_repo, repo_copy, ignore=shutil.ignore_patterns(".git", "node_modules", ".next", "dist", "build", "coverage"))
                project = repo_copy if selected_project == "." else repo_copy / selected_project
                env = sanitized_env(node_dir, workspace)
                chown_tree(workspace)

                pkg = load_json(project / "package.json")
                pm = detect_package_manager(project, pkg)
                npm_cli, requested_npm, npm_source = npm_cli_for(node_dir, npm_spec, npm_root)
                npm_probe = run(npm_cli + ["--version"], cwd=project, env=env, timeout=30)
                npm_version = npm_probe["output"].strip() if npm_probe["code"] == 0 else f"unavailable ({npm_spec})"

                if pm["name"] != "npm":
                    results.append({
                        "node_major": major,
                        "node_version": runtime_version,
                        "npm_requested": requested_npm,
                        "npm_source": npm_source,
                        "npm_version": npm_version,
                        "status": "unsupported_package_manager",
                        "package_manager": pm,
                        "install": {"code": 98, "duration_seconds": 0, "output": f"Full builds currently support npm projects; detected {pm['name']}.", "timed_out": False, "cmd": []},
                        "build": None,
                        "failure_signatures": [],
                    })
                    continue

                if npm_probe["code"] != 0:
                    results.append({
                        "node_major": major,
                        "node_version": runtime_version,
                        "npm_requested": requested_npm,
                        "npm_source": npm_source,
                        "npm_version": npm_version,
                        "status": "fail_toolchain",
                        "package_manager": pm,
                        "install": npm_probe,
                        "build": None,
                        "failure_signatures": classify_failure(npm_probe["output"]),
                    })
                    continue

                lock = (project / "package-lock.json").exists() or (project / "npm-shrinkwrap.json").exists()
                common_args = ["--include=dev", "--no-audit", "--no-fund"]
                install_cmd = npm_cli + (["ci"] if lock else ["install"]) + common_args
                install = run(install_cmd, cwd=project, env=env, timeout=INSTALL_TIMEOUT, limits=True, drop_privileges=True)

                build = None
                has_build = "build" in (pkg.get("scripts") or {})
                if install["code"] == 0 and has_build:
                    build = run(npm_cli + ["run", "build"], cwd=project, env=env, timeout=BUILD_TIMEOUT, limits=True, drop_privileges=True)

                status = "pass"
                fail_output = ""
                if install["code"] != 0:
                    status = "fail_install"
                    fail_output = install["output"]
                elif build and build["code"] != 0:
                    status = "fail_build"
                    fail_output = build["output"]

                results.append({
                    "node_major": major,
                    "node_version": runtime_version,
                    "npm_requested": requested_npm,
                    "npm_source": npm_source,
                    "npm_version": npm_version,
                    "status": status,
                    "package_manager": pm,
                    "install": install,
                    "build": build,
                    "failure_signatures": classify_failure(fail_output),
                })
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
    return results



def quick_npm_probe(
    repo_url: str,
    branch: str | None,
    work_root: Path,
    runtime_root: Path,
    node_majors: list[int] | None = None,
    npm_versions: list[str] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Fast research probe for npm compatibility.

    It shallow-clones the repo and runs npm ci --dry-run --ignore-scripts
    across a controlled Node x npm matrix. No package lifecycle scripts run.
    This is intentionally cheaper and safer than a full build and is meant
    for high-frequency screening before any deeper confirmation.
    """
    slug = repo_slug(repo_url)
    node_majors = node_majors or [22]
    npm_specs = validate_npm_versions(npm_versions or ["10.9.9", "11.19.0"])
    session = Path(tempfile.mkdtemp(prefix="wid-quick-", dir=work_root))
    clone_dir = session / "repo"
    try:
        size_mb = clone_repo(repo_url, branch, clone_dir)
        project, projects, selected = choose_project(clone_dir, project_path)
        if project is None or not selected:
            return {
                "repo": slug,
                "status": "not_applicable",
                "repo_size_mb": size_mb,
                "project_path": None,
                "projects": projects,
                "matrix": [],
                "classification": "no_node_project",
            }

        pkg = load_json(project / "package.json")
        pm = detect_package_manager(project, pkg)
        has_lock = (project / "package-lock.json").exists() or (project / "npm-shrinkwrap.json").exists()
        if pm["name"] != "npm":
            return {
                "repo": slug,
                "status": "unsupported_package_manager",
                "repo_size_mb": size_mb,
                "project_path": selected,
                "projects": projects,
                "package_manager": pm,
                "matrix": [],
                "classification": f"unsupported_{pm['name']}",
            }
        if not has_lock:
            return {
                "repo": slug,
                "status": "no_lockfile",
                "repo_size_mb": size_mb,
                "project_path": selected,
                "projects": projects,
                "package_manager": pm,
                "matrix": [],
                "classification": "no_npm_lockfile",
            }

        results: list[dict[str, Any]] = []
        npm_root = runtime_root.parent / "npm-tools"

        for major in node_majors:
            node_dir = ensure_node(major, runtime_root)
            runtime_version = run([str(node_dir / "bin" / "node"), "--version"], timeout=20)["output"].strip()

            for npm_spec in npm_specs:
                safe_spec = npm_spec.replace(".", "-")
                workspace = Path(tempfile.mkdtemp(prefix=f"wid-probe-node{major}-npm{safe_spec}-", dir=work_root))
                repo_copy = workspace / "repo"
                try:
                    shutil.copytree(
                        clone_dir,
                        repo_copy,
                        ignore=shutil.ignore_patterns(".git", "node_modules", ".next", "dist", "build", "coverage"),
                    )
                    selected_project = repo_copy if selected == "." else repo_copy / selected
                    env = sanitized_env(node_dir, workspace)
                    chown_tree(workspace)

                    npm_cli, requested_npm, npm_source = npm_cli_for(node_dir, npm_spec, npm_root)
                    npm_probe = run(npm_cli + ["--version"], cwd=selected_project, env=env, timeout=30)
                    npm_version = npm_probe["output"].strip() if npm_probe["code"] == 0 else f"unavailable ({npm_spec})"

                    if npm_probe["code"] != 0:
                        results.append({
                            "node_major": major,
                            "node_version": runtime_version,
                            "npm_requested": requested_npm,
                            "npm_source": npm_source,
                            "npm_version": npm_version,
                            "status": "fail_toolchain",
                            "probe": npm_probe,
                            "failure_signatures": classify_failure(npm_probe["output"]),
                        })
                        continue

                    cmd = npm_cli + [
                        "ci",
                        "--dry-run",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                        "--progress=false",
                    ]
                    probe = run(cmd, cwd=selected_project, env=env, timeout=min(INSTALL_TIMEOUT, 180), limits=True, drop_privileges=True)
                    results.append({
                        "node_major": major,
                        "node_version": runtime_version,
                        "npm_requested": requested_npm,
                        "npm_source": npm_source,
                        "npm_version": npm_version,
                        "status": "pass" if probe["code"] == 0 else "fail_install",
                        "probe": probe,
                        "failure_signatures": classify_failure(probe["output"]),
                    })
                finally:
                    shutil.rmtree(workspace, ignore_errors=True)

        by_npm: dict[str, list[str]] = {}
        for row in results:
            key = str(row.get("npm_requested") or row.get("npm_version"))
            by_npm.setdefault(key, []).append(row["status"])

        classification = "mixed"
        npm10 = by_npm.get("10.9.9", [])
        npm11 = by_npm.get("11.19.0", [])
        if npm10 and npm11:
            if all(x == "pass" for x in npm10) and all(x != "pass" for x in npm11):
                classification = "npm11_break_candidate"
            elif all(x == "pass" for x in npm10 + npm11):
                classification = "clean"
            elif all(x != "pass" for x in npm10 + npm11):
                classification = "baseline_fail"
            elif all(x != "pass" for x in npm10) and all(x == "pass" for x in npm11):
                classification = "npm11_fix_candidate"

        return {
            "repo": slug,
            "status": "completed",
            "repo_size_mb": size_mb,
            "project_path": selected,
            "projects": projects,
            "package_manager": pm,
            "matrix": results,
            "classification": classification,
        }
    finally:
        shutil.rmtree(session, ignore_errors=True)

def diagnose_matrix(matrix: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not matrix:
        return None
    considered = [r for r in matrix if r.get("status") != "unsupported_package_manager"]
    if not considered:
        return {"kind": "unsupported", "confidence": "low", "headline": "No supported npm toolchain was tested.", "evidence": []}

    def outcome(r):
        if r.get("status") == "pass":
            return "pass"
        sig_codes = {x.get("code") for x in r.get("failure_signatures", [])}
        if "network" in sig_codes:
            return "network"
        return "fail"

    stable = [r for r in considered if outcome(r) != "network"]
    if not stable:
        return {"kind": "network", "confidence": "low", "headline": "Only network failures were observed; compatibility is inconclusive.", "evidence": []}

    nodes = sorted({int(r["node_major"]) for r in stable})
    npms: list[str] = []
    for r in stable:
        n = str(r.get("npm_requested") or r.get("npm_version") or "bundled")
        if n not in npms:
            npms.append(n)

    lookup = {(int(r["node_major"]), str(r.get("npm_requested") or r.get("npm_version") or "bundled")): outcome(r) for r in stable}
    pass_count = sum(1 for r in stable if outcome(r) == "pass")
    fail_count = sum(1 for r in stable if outcome(r) == "fail")

    if fail_count == 0:
        return {"kind": "clean", "confidence": "high", "headline": "No compatibility break reproduced in the tested Node x npm matrix.", "evidence": [f"{pass_count}/{len(stable)} non-network toolchains passed."]}
    if pass_count == 0:
        return {"kind": "baseline", "confidence": "medium", "headline": "The repository fails across every tested toolchain; this is not yet a version-specific break.", "evidence": [f"{fail_count}/{len(stable)} non-network toolchains failed."]}

    if len(npms) >= 2:
        directions: dict[tuple[str, str], list[int]] = {}
        for a in npms:
            for b in npms:
                if a == b:
                    continue
                matching = [node for node in nodes if lookup.get((node, a)) == "pass" and lookup.get((node, b)) == "fail"]
                if matching:
                    directions[(a, b)] = matching
        if directions:
            (a, b), matching = max(directions.items(), key=lambda kv: len(kv[1]))
            if len(matching) >= 2:
                return {
                    "kind": "npm",
                    "confidence": "high",
                    "headline": f"npm compatibility split isolated: npm {a} passes while npm {b} fails on Node {', '.join(map(str, matching))}.",
                    "evidence": [f"Same Node runtime, different npm result on {len(matching)} Node versions."],
                }

    if len(nodes) >= 2:
        directions2: dict[tuple[int, int], list[str]] = {}
        for a in nodes:
            for b in nodes:
                if a == b:
                    continue
                matching = [npm for npm in npms if lookup.get((a, npm)) == "pass" and lookup.get((b, npm)) == "fail"]
                if matching:
                    directions2[(a, b)] = matching
        if directions2:
            (a, b), matching = max(directions2.items(), key=lambda kv: len(kv[1]))
            if len(matching) >= 2:
                return {
                    "kind": "node",
                    "confidence": "high",
                    "headline": f"Node runtime split isolated: Node {a} passes while Node {b} fails under npm {', '.join(matching)}.",
                    "evidence": [f"Same npm version, different Node result across {len(matching)} npm toolchains."],
                }

    failing = [f"Node {r['node_major']} + npm {r.get('npm_requested') or r.get('npm_version')}" for r in stable if outcome(r) == "fail"]
    passing = [f"Node {r['node_major']} + npm {r.get('npm_requested') or r.get('npm_version')}" for r in stable if outcome(r) == "pass"]
    return {
        "kind": "interaction",
        "confidence": "medium",
        "headline": "A toolchain interaction was reproduced, but it is not explained by Node or npm alone.",
        "evidence": ["Pass: " + "; ".join(passing[:4]), "Fail: " + "; ".join(failing[:4])],
    }


def summarize(static: dict[str, Any], matrix: list[dict[str, Any]] | None) -> str:
    if not static.get("node_project_found"):
        return "No Node project discovered; repository recorded as not applicable instead of a failed experiment."
    if not matrix:
        return f"Static risk {static['risk_score']}/100; {len([x for x in static['native_or_risky_dependencies'] if not x.get('informational')])} runtime-sensitive package(s) detected."
    diagnosis = diagnose_matrix(matrix)
    return diagnosis["headline"] if diagnosis else "Build matrix completed."


def scan_repo(repo_url: str, branch: str | None, mode: str, node_majors: list[int], work_root: Path, runtime_root: Path, project_path: str | None = None, npm_versions: list[str] | None = None) -> dict[str, Any]:
    slug = repo_slug(repo_url)
    session = Path(tempfile.mkdtemp(prefix="wid-clone-", dir=work_root))
    clone_dir = session / "repo"
    try:
        size_mb = clone_repo(repo_url, branch, clone_dir)
        project, projects, selected = choose_project(clone_dir, project_path)
        static = static_analysis(clone_dir, project, projects, selected)
        matrix = None
        if mode == "full":
            if project is None or not selected:
                matrix = []
            else:
                matrix = full_build_matrix(clone_dir, selected, node_majors, work_root, runtime_root, npm_versions=npm_versions)
        return {
            "repo": slug,
            "repo_url": repo_url,
            "branch": branch,
            "project_path": selected,
            "mode": mode,
            "repo_size_mb": size_mb,
            "static": static,
            "matrix": matrix,
            "diagnosis": diagnose_matrix(matrix),
            "summary": summarize(static, matrix),
            "generated_at": int(time.time()),
        }
    finally:
        shutil.rmtree(session, ignore_errors=True)


def scan_fixture(fixture_dir: Path, node_majors: list[int], work_root: Path, runtime_root: Path) -> dict[str, Any]:
    project, projects, selected = choose_project(fixture_dir)
    static = static_analysis(fixture_dir, project, projects, selected)
    matrix = full_build_matrix(fixture_dir, selected or ".", node_majors, work_root, runtime_root, npm_versions=["bundled"])
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


def regression_checks(fixtures_root: Path) -> list[dict[str, Any]]:
    checks = []

    transitive = fixtures_root / "transitive-deasync"
    project, projects, selected = choose_project(transitive)
    result = static_analysis(transitive, project, projects, selected)
    hit = next((x for x in result["native_or_risky_dependencies"] if x["package"] == "deasync"), None)
    checks.append({
        "name": "transitive deasync detection",
        "pass": bool(hit),
        "detail": "deasync detected from lockfile" if hit else "deasync was missed",
    })

    volta = fixtures_root / "volta-pin"
    project, projects, selected = choose_project(volta)
    result = static_analysis(volta, project, projects, selected)
    pin = next((x for x in result["runtime_pins"] if x["source"] == "package.json volta.node"), None)
    checks.append({
        "name": "Volta Node pin detection",
        "pass": bool(pin and pin["value"] == "20.15.0"),
        "detail": f"detected {pin['value']}" if pin else "Volta pin was missed",
    })
    return checks
