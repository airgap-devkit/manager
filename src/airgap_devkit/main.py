"""
airgap-cpp-devkit — DevKit Manager
FastAPI + HTMX web UI for managing devkit tool installations.
"""
import asyncio
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import jinja2
from fastapi.templating import Jinja2Templates

from airgap_devkit.config import DevkitConfig
from airgap_devkit.connectivity import detect_mode

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).parent    # .../airgap-devkit-manager/app/
REPO_ROOT = APP_DIR.parent.parent  # app/ -> airgap-devkit-manager/ -> repo root
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
USER_PACKAGES_DIR = REPO_ROOT / "user-packages"
STAGING_DIR = REPO_ROOT / "user-packages" / ".staging"


def _detect_os() -> str:
    s = platform.system().lower()
    if "windows" in s or os.environ.get("MSYSTEM"):
        return "windows"
    return "linux"


OS = _detect_os()


_PREFIX_OVERRIDE_FILE = APP_DIR.parent / ".devkit-prefix"  # airgap-devkit-manager/.devkit-prefix


def _detect_prefix() -> Path:
    # 1. Persisted UI override
    if _PREFIX_OVERRIDE_FILE.exists():
        try:
            p = _PREFIX_OVERRIDE_FILE.read_text(encoding="utf-8").strip()
            if p:
                return Path(p)
        except Exception:
            pass
    # 2. Auto-detect
    if OS == "windows":
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(local) / "airgap-cpp-devkit"
    if Path("/opt/airgap-cpp-devkit").exists():
        return Path("/opt/airgap-cpp-devkit")
    return Path.home() / ".local" / "share" / "airgap-cpp-devkit"


INSTALL_PREFIX = _detect_prefix()


def _current_prefix() -> Path:
    """Return live prefix (re-reads override file each request)."""
    return _detect_prefix()


def _to_bash_path(p: Path) -> str:
    """Convert a path to forward slashes so Git Bash on Windows won't mangle \\n, \\t, \\a, etc."""
    if OS == "windows":
        return str(p).replace("\\", "/")
    return str(p)


def _detect_privilege() -> str:
    """Return 'admin' if the process has elevated/root privileges, else 'user'."""
    try:
        if OS == "windows":
            import ctypes
            return "admin" if ctypes.windll.shell32.IsUserAnAdmin() else "user"
        else:
            return "admin" if os.getuid() == 0 else "user"
    except Exception:
        return "user"


def _get_system_info() -> dict:
    import shutil as _shutil
    prefix = _current_prefix()
    disk_free = disk_total = None
    try:
        check = prefix if prefix.exists() else (prefix.parent if prefix.parent.exists() else Path("/"))
        stat = _shutil.disk_usage(str(check))
        disk_free = f"{stat.free / (1024**3):.1f} GB"
        disk_total = f"{stat.total / (1024**3):.1f} GB"
    except Exception:
        pass
    privilege = _detect_privilege()
    if privilege == "admin":
        admin_prefix_str = (
            r"C:\Program Files\airgap-cpp-devkit" if OS == "windows"
            else "/opt/airgap-cpp-devkit"
        )
    else:
        admin_prefix_str = None
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "privilege": privilege,
        "disk_free": disk_free,
        "disk_total": disk_total,
        "admin_prefix": admin_prefix_str,
    }

# ---------------------------------------------------------------------------
# Tool discovery — scans for devkit.json manifests in tool directories.
#
# Built-in tools:  devkit.json lives alongside setup.sh in the tool directory.
# User packages:   uploaded via the web UI and stored in user-packages/<id>/.
#
# To add a built-in tool: create devkit.json next to setup.sh — no Python edits.
# To add a user tool:     upload a .zip via the "Add Package" button in the UI.
#
# Scan order (first match by id wins):
#   tools/dev-tools/*/          tools/dev-tools/*/*/
#   tools/build-tools/*/
#   tools/languages/*/
#   tools/toolchains/*/         tools/toolchains/*/*/    tools/toolchains/*/*/*/
#   tools/frameworks/*/
#   packages/*/           ← built-in tools with no dedicated directory (repo root)
#   user-packages/*/      ← user-uploaded packages (gitignored, repo root)
# ---------------------------------------------------------------------------

# Each entry: (glob_pattern, source_tag)
_TOOL_SCAN_PATTERNS: list[tuple[str, str]] = [
    ("tools/dev-tools/*/devkit.json",          "builtin"),
    ("tools/dev-tools/*/*/devkit.json",        "builtin"),
    ("tools/build-tools/*/devkit.json",        "builtin"),
    ("tools/languages/*/devkit.json",          "builtin"),
    ("tools/toolchains/*/devkit.json",         "builtin"),
    ("tools/toolchains/*/*/devkit.json",       "builtin"),
    ("tools/toolchains/*/*/*/devkit.json",     "builtin"),
    ("tools/frameworks/*/devkit.json",         "builtin"),
    ("packages/*/devkit.json",                 "builtin"),
    ("user-packages/*/devkit.json",            "user"),
]

_REQUIRED_MANIFEST_FIELDS = ["id", "name", "version", "category", "platform",
                              "description", "setup", "receipt_name"]


def _load_tools() -> list:
    import glob as _glob
    tools: list = []
    seen_ids: set = set()
    for pattern, source in _TOOL_SCAN_PATTERNS:
        for manifest_path in sorted(_glob.glob(str(REPO_ROOT / pattern))):
            try:
                data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[devkit] Warning: cannot load {manifest_path}: {exc}", file=sys.stderr)
                continue
            tool_id = data.get("id", "").strip()
            if not tool_id or tool_id in seen_ids:
                continue
            seen_ids.add(tool_id)
            # Source is determined by scan location, not the manifest contents
            data["source"] = source
            # Apply defaults so templates never see missing keys
            data.setdefault("platform", "both")
            data.setdefault("category", "Developer Tools")
            data.setdefault("estimate", "~1min")
            data.setdefault("uses_prebuilt", False)
            data.setdefault("setup_args", [])
            data.setdefault("version", "")
            data.setdefault("version_label", None)
            tools.append(data)
    tools.sort(key=lambda t: (t.get("sort_order", 99), t.get("category", ""), t.get("name", "")))
    return tools


TOOLS = _load_tools()


def _reload_tools() -> None:
    """Refresh TOOLS in-place after a package is added or removed."""
    global TOOLS
    TOOLS[:] = _load_tools()

PROFILES = {
    "cpp-dev": {
        "name": "C++ Developer",
        "description": "Core C++ development tools",
        "tools": ["toolchains/clang", "cmake", "python", "conan", "vscode-extensions", "sqlite", "7zip"],
        "color": "blue",
    },
    "devops": {
        "name": "DevOps",
        "description": "Infrastructure and automation tools",
        "tools": ["cmake", "python", "conan", "sqlite", "7zip"],
        "color": "green",
    },
    "minimal": {
        "name": "Minimal",
        "description": "Required tools only",
        "tools": ["toolchains/clang", "cmake", "python", "style-formatter"],
        "color": "gray",
    },
    "full": {
        "name": "Full Install",
        "description": "All available tools",
        "tools": [t["id"] for t in TOOLS],
        "color": "purple",
    },
}

# ---------------------------------------------------------------------------
# Prebuilt-binaries submodule detection
# ---------------------------------------------------------------------------
def get_submodule_status() -> dict:
    """Check whether the prebuilt-binaries submodule is initialised and up to date."""
    submodule_dir = REPO_ROOT / "prebuilt-binaries"
    result = {
        "initialized": False,
        "stale": False,       # True if submodule pointer is ahead/behind
        "commit": None,
        "path": str(submodule_dir),
        "prebuilt_tool_count": sum(1 for t in TOOLS if t.get("uses_prebuilt")),
    }

    if not submodule_dir.exists():
        return result

    # Non-empty directory means the submodule has been checked out
    try:
        contents = list(submodule_dir.iterdir())
    except PermissionError:
        contents = []
    result["initialized"] = len(contents) > 0

    if not result["initialized"]:
        return result

    # Ask git for the submodule status line
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "submodule", "status", "prebuilt-binaries"],
            capture_output=True, text=True, timeout=5,
        )
        line = proc.stdout.strip()
        if line:
            # Leading char: ' ' = OK, '+' = stale (commit differs), '-' = not init
            result["stale"] = line[0] == "+"
            result["commit"] = line.lstrip(" +-").split()[0][:10]
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Receipt reader
# ---------------------------------------------------------------------------
def _normalise_date(raw: str) -> str:
    """Convert various date formats to MM/DD/YYYY HH:mm for display."""
    if not raw:
        return raw
    for fmt in (
        "%m/%d/%Y %H:%M",             # already in target format
        "%Y%m%d%H%M",                 # receipt file: date +%Y%m%d%H%M
        "%a %b %d %H:%M:%S %Z %Y",   # Sat Apr 12 18:07:00 UTC 2025
        "%a %b %d %H:%M:%S %Y",       # Sat Apr 12 18:07:00 2025
        "%a %b  %d %H:%M:%S %Y",      # single-digit day: Sat Apr  5 18:07:00 2025
        "%Y-%m-%dT%H:%M:%SZ",         # ISO with Z
        "%Y-%m-%dT%H:%M:%S",          # ISO no Z
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%m/%d/%Y %H:%M")
        except ValueError:
            pass
    return raw   # unknown format — return as-is


def _parse_receipt(path: Path) -> dict:
    data = {"status": "not_installed", "version": None, "date": None,
            "install_path": None, "user": None, "hostname": None, "log_file": None,
            "receipt_exists": False}
    if not path.exists():
        return data
    data["receipt_exists"] = True
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("Version"):
                data["version"] = line.split(":", 1)[-1].strip()
            elif line.startswith("Status"):
                data["status"] = line.split(":", 1)[-1].strip()
            elif line.startswith("Date"):
                data["date"] = _normalise_date(line.split(":", 1)[-1].strip())
            elif line.startswith("Install path"):
                data["install_path"] = line.split(":", 1)[-1].strip()
            elif line.startswith("User"):
                data["user"] = line.split(":", 1)[-1].strip()
            elif line.startswith("Hostname"):
                data["hostname"] = line.split(":", 1)[-1].strip()
            elif line.startswith("Log file"):
                data["log_file"] = line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return data


def _load_manifest(tool: dict) -> dict | None:
    """Return a normalised manifest dict for display, or None if nothing useful found.

    User packages have wizard-generated manifests with ``zip_sha256`` and ``files[]``.
    Built-in tools have bespoke manifests; we extract any sha256 values we can find
    and return them as a flat ``checksums`` list so the UI has a uniform structure.
    """
    setup = tool.get("setup", "")
    if not setup:
        return None
    manifest_path = REPO_ROOT / Path(setup).parent / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Wizard-generated user-package manifest — already in the right shape
    if raw.get("zip_sha256") or raw.get("files"):
        return raw

    # Built-in manifest — walk the object tree and collect all sha256 leaf values
    checksums: list[dict] = []

    def _walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, str) and len(obj) == 64 and all(c in "0123456789abcdef" for c in obj):
            # Looks like a SHA256 hex string
            label = path.split(".")[-2] if "." in path else path
            checksums.append({"path": path, "label": label, "sha256": obj})

    _walk(raw, "")
    if not checksums:
        return None
    return {"files": [{"path": c["label"], "sha256": c["sha256"]} for c in checksums]}


_RECEIPT_FILENAME        = "INSTALL_LOG.txt"
_RECEIPT_FILENAME_LEGACY = "INSTALL_RECEIPT.txt"


def _get_receipt_path(receipt_name: str) -> Path:
    """Return the receipt path, preferring the new name but falling back to the legacy name."""
    clean = receipt_name.replace("/", os.sep)
    tool_dir = _current_prefix() / clean
    new_path = tool_dir / _RECEIPT_FILENAME
    if new_path.exists():
        return new_path
    # Fall back to legacy name (existing installations)
    return tool_dir / _RECEIPT_FILENAME_LEGACY


def get_tool_status(tool: dict) -> dict:
    receipt_path = _get_receipt_path(tool["receipt_name"])
    receipt = _parse_receipt(receipt_path)
    installed = receipt["status"] == "success"
    # Platform check
    available = tool["platform"] == "both" or tool["platform"] == OS
    setup_rel = tool.get("setup", "")
    uploaded_at_raw = tool.get("uploaded_at", "")
    return {
        **tool,
        "installed": installed,
        "available": available,
        "receipt": receipt,
        "receipt_path": str(receipt_path),
        "setup_abs": str(REPO_ROOT / setup_rel) if setup_rel else "",
        "manifest": _load_manifest(tool),
        "uploaded_at_display": _normalise_date(uploaded_at_raw) if uploaded_at_raw else "",
    }


def get_all_tools_status() -> list:
    return [get_tool_status(t) for t in TOOLS]


# ---------------------------------------------------------------------------
# Config + connectivity
# ---------------------------------------------------------------------------
_config = DevkitConfig.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mode = detect_mode()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="DevKit Manager", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
)

def render(name: str, ctx: dict) -> HTMLResponse:
    t = _jinja_env.get_template(name)
    return HTMLResponse(t.render(**ctx))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    tools = get_all_tools_status()
    installed_count = sum(1 for t in tools if t["installed"])
    available_count = sum(1 for t in tools if t["available"])
    categories = {}
    for t in tools:
        cat = t["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(t)
    submodule = get_submodule_status()
    return render("dashboard.html", {
        "request": request,
        "tools": tools,
        "categories": categories,
        "profiles": PROFILES,
        "installed_count": installed_count,
        "available_count": available_count,
        "total_count": len(tools),
        "os": OS,
        "prefix": str(_current_prefix()),
        "hostname": platform.node(),
        "submodule": submodule,
        "system_info": _get_system_info(),
        "config": _config,
        "mode": request.app.state.mode,
    })


@app.get("/api/connectivity", response_class=JSONResponse)
async def api_connectivity(request: Request):
    return {"mode": request.app.state.mode}


@app.get("/api/prefix", response_class=JSONResponse)
async def api_get_prefix():
    return {
        "prefix": str(_current_prefix()),
        "is_override": _PREFIX_OVERRIDE_FILE.exists(),
        "default": str(_detect_prefix() if not _PREFIX_OVERRIDE_FILE.exists() else None),
    }


@app.post("/api/prefix")
async def api_set_prefix(request: Request):
    body = await request.json()
    new_prefix = body.get("prefix", "").strip()
    if not new_prefix:
        return JSONResponse({"error": "prefix cannot be empty"}, status_code=400)
    try:
        _PREFIX_OVERRIDE_FILE.write_text(new_prefix, encoding="utf-8")
        return {"prefix": new_prefix, "ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/prefix")
async def api_reset_prefix():
    """Remove override, revert to auto-detected prefix."""
    if _PREFIX_OVERRIDE_FILE.exists():
        _PREFIX_OVERRIDE_FILE.unlink()
    return {"prefix": str(_detect_prefix()), "ok": True}


@app.get("/api/submodule", response_class=JSONResponse)
async def api_submodule():
    return get_submodule_status()


@app.post("/init-submodule")
async def init_submodule():
    """Stream output of git submodule update --init --recursive prebuilt-binaries."""
    async def stream():
        yield "data: Initialising prebuilt-binaries submodule...\n\n"
        cmd = [
            "git", "-C", str(REPO_ROOT),
            "submodule", "update", "--init", "--recursive", "prebuilt-binaries",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield "data: ✓ prebuilt-binaries initialised successfully\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ git exited with code {proc.returncode}\n\n"
                yield "data: DONE:failed\n\n"
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/run-tests")
async def run_tests(verbose: bool = False):
    """Stream output of tests/run-tests.sh."""
    async def stream():
        yield "data: Running smoke tests...\n\n"
        cmd = ["bash", "tests/run-tests.sh", "--os", OS, "--prefix", _to_bash_path(_current_prefix())]
        if verbose:
            cmd.append("--verbose")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_to_bash_path(REPO_ROOT),
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield "data: DONE:success\n\n"
            else:
                yield "data: DONE:failed\n\n"
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/tools", response_class=JSONResponse)
async def api_tools():
    return get_all_tools_status()


@app.get("/api/tool/{tool_id:path}", response_class=JSONResponse)
async def api_tool(tool_id: str):
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)
    return get_tool_status(tool)


@app.get("/install/{tool_id:path}")
async def install_tool(tool_id: str, rebuild: bool = False):
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    setup_script = REPO_ROOT / tool["setup"]

    async def stream():
        yield f"data: Installing {tool['name']} {tool['version']}...\n\n"
        cmd = ["bash", _to_bash_path(setup_script)] + tool.get("setup_args", [])
        if rebuild:
            cmd.append("--rebuild")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_to_bash_path(REPO_ROOT),
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield "data: ✓ Installation complete\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ Installation failed (exit {proc.returncode})\n\n"
                yield "data: DONE:failed\n\n"
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/install-profile/{profile_id}")
async def install_profile(profile_id: str, rebuild: bool = False):
    profile = PROFILES.get(profile_id)
    if not profile:
        return JSONResponse({"error": "Profile not found"}, status_code=404)

    tool_ids = profile["tools"]
    # Filter for platform-available tools
    tools_to_install = [
        t for t in TOOLS
        if t["id"] in tool_ids and (t["platform"] == "both" or t["platform"] == OS)
    ]

    async def stream():
        yield f"data: Installing profile: {profile['name']} ({len(tools_to_install)} tools)\n\n"
        for tool in tools_to_install:
            yield f"data: \n\n"
            yield f"data: ── {tool['name']} {tool['version']}\n\n"
            setup_script = REPO_ROOT / tool["setup"]
            cmd = ["bash", _to_bash_path(setup_script)] + tool.get("setup_args", [])
            if rebuild:
                cmd.append("--rebuild")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=_to_bash_path(REPO_ROOT),
                )
                async for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        yield f"data: {text}\n\n"
                await proc.wait()
                status = "✓" if proc.returncode == 0 else "✗"
                yield f"data: {status} {tool['name']} done\n\n"
            except Exception as e:
                yield f"data: ERROR: {e}\n\n"
        yield "data: \n\n"
        yield "data: ✓ Profile installation complete\n\n"
        yield "data: DONE:success\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    log_dirs = []
    if OS == "windows":
        import tempfile
        log_base = Path(tempfile.gettempdir()) / "airgap-cpp-devkit" / "logs"
    else:
        log_base = Path("/var/log/airgap-cpp-devkit")
        if not log_base.exists():
            log_base = Path.home() / "airgap-cpp-devkit-logs"

    logs = []
    if log_base.exists():
        for f in sorted(log_base.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            logs.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "tool": f.parent.name,
            })

    return render("logs.html", {
        "request": request,
        "logs": logs,
        "log_base": str(log_base),
        "os": OS,
        "config": _config,
        "mode": request.app.state.mode,
    })


@app.get("/api/log")
async def get_log(path: str):
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"content": content})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.delete("/uninstall/{tool_id:path}")
async def uninstall_tool(tool_id: str):
    """Remove a tool's install directory from the prefix."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    receipt_path = _get_receipt_path(tool["receipt_name"])
    install_dir = receipt_path.parent  # <prefix>/<tool>/

    async def stream():
        yield f"data: Uninstalling {tool['name']}...\n\n"
        if not install_dir.exists():
            yield "data: Nothing to remove — directory does not exist.\n\n"
            yield "data: DONE:success\n\n"
            return
        try:
            import shutil
            shutil.rmtree(str(install_dir))
            yield f"data: ✓ Removed {install_dir}\n\n"
            yield "data: DONE:success\n\n"
        except Exception as e:
            yield f"data: ✗ ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Package upload helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_staging() -> None:
    """Remove staging dirs older than 1 hour."""
    if not STAGING_DIR.exists():
        return
    cutoff = time.time() - 3600
    for d in STAGING_DIR.iterdir():
        if d.is_dir():
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(str(d), ignore_errors=True)
            except Exception:
                pass


def _detect_package_type(dest: Path) -> str:
    """Detect the primary content type of an extracted package directory."""
    exes = [f for f in dest.rglob("*.exe") if f.is_file()]
    if exes:
        return "installer_exe" if len(exes) == 1 else "portable_exe"
    return "files"


def _generate_setup_sh(dest: Path, tool_id: str, name: str, version: str) -> None:
    """Write a template setup.sh appropriate for the package content."""
    pkg_type = _detect_package_type(dest)

    receipt_block = [
        "{",
        '  echo "Status: success"',
        f'  echo "Version: {version}"',
        '  echo "Date: $(date +%Y%m%d%H%M)"',
        f'  echo "Install path: $TOOL_DIR"',
        f'}} > "$TOOL_DIR/{_RECEIPT_FILENAME}"',
    ]

    header = [
        "#!/usr/bin/env bash",
        f"# setup.sh — generated by airgap DevKit Manager for {name}",
        "# Edit this script to customise how the tool is installed.",
        "set -euo pipefail",
        "",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"',
        "",
        'source "$REPO_ROOT/scripts/install-mode.sh"',
        "",
        'PREFIX="$DEFAULT_PREFIX"',
        "REBUILD=false",
        "",
        "while [[ $# -gt 0 ]]; do",
        "  case $1 in",
        '    --prefix) PREFIX="$2"; shift 2 ;;',
        "    --rebuild) REBUILD=true; shift ;;",
        "    *) shift ;;",
        "  esac",
        "done",
        "",
        f'TOOL_DIR="$PREFIX/{tool_id}"',
        'mkdir -p "$TOOL_DIR"',
        "",
    ]

    if pkg_type == "installer_exe":
        # Single .exe — find it and run it silently, then write receipt
        exes = sorted(f for f in dest.rglob("*.exe") if f.is_file())
        exe_name = exes[0].name if exes else "*.exe"
        body = [
            f'# This package contains a Windows installer: {exe_name}',
            "# The installer is run silently below.",
            "# Adjust flags if your installer uses a different silent-install switch.",
            "# Common switches:  /S  /silent  /quiet  /norestart  /sp-  /verysilent",
            "#",
            "# To install to a custom location add something like:",
            f'#   /D="$(cygpath -w "$TOOL_DIR")"',
            "",
            f'INSTALLER="$SCRIPT_DIR/{exe_name}"',
            'if [[ ! -f "$INSTALLER" ]]; then',
            f'  echo "✗ Installer not found: $INSTALLER" >&2; exit 1',
            "fi",
            "",
            f'echo "Running {exe_name} silently…"',
            '# Run via cmd.exe so Windows UAC / installer APIs work correctly',
            'cmd.exe /c "$(cygpath -w "$INSTALLER")" /S',
            "",
            "# If the installer places files in a known directory, copy or symlink them",
            "# into TOOL_DIR so this devkit can track them.",
            "# Example:",
            f'#   cp -r "/c/Program Files/{name}/"* "$TOOL_DIR/"',
        ]
    else:
        body = [
            "# TODO: customise the installation steps below.",
            "# By default all package files are copied to the install directory.",
            'cp -r "$SCRIPT_DIR/"* "$TOOL_DIR/"',
        ]

    lines = header + body + [""] + receipt_block + ["", f'echo "✓ {name} installed to $TOOL_DIR"']
    setup_path = dest / "setup.sh"
    setup_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        setup_path.chmod(0o755)
    except Exception:
        pass  # chmod is a no-op on Windows; Git Bash will still run it


# ---------------------------------------------------------------------------
# Package upload endpoints — 2-step guided wizard
# ---------------------------------------------------------------------------

@app.post("/packages/preflight")
async def packages_preflight(file: UploadFile = File(...)):
    """Step 1: Upload zip, analyse contents, return pre-fill hints for the metadata form."""
    if not (file.filename or "").lower().endswith(".zip"):
        return JSONResponse({"error": "Only .zip files are accepted"}, status_code=400)

    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        return JSONResponse({"error": "Zip must be under 200 MB"}, status_code=400)

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return JSONResponse({"error": "Invalid or corrupt zip file"}, status_code=400)

    for name in names:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            return JSONResponse({"error": f"Unsafe path in zip: {name}"}, status_code=400)

    _cleanup_staging()

    staging_id = str(uuid.uuid4())
    staging_path = STAGING_DIR / staging_id
    staging_path.mkdir(parents=True, exist_ok=True)

    zip_sha256 = _sha256_bytes(content)
    (staging_path / "upload.zip").write_bytes(content)

    # Extract to contents/
    contents_base = staging_path / "contents"
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        zf.extractall(str(contents_base))

    # Detect single top-level wrapper directory (common zip convention)
    top = [p for p in contents_base.iterdir()]
    contents_root = top[0] if len(top) == 1 and top[0].is_dir() else contents_base

    # Record resolved root so finalize can find it
    (staging_path / "contents_root.txt").write_text(str(contents_root), encoding="utf-8")

    # Build file inventory with SHA256s
    file_hashes: dict = {}
    for f in sorted(contents_root.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(contents_root)).replace("\\", "/")
            file_hashes[rel] = _sha256_file(f)

    # Pre-fill from existing devkit.json if present
    detected: dict = {}
    devkit_path = contents_root / "devkit.json"
    if devkit_path.exists():
        try:
            existing = json.loads(devkit_path.read_text(encoding="utf-8"))
            for field in ("id", "name", "version", "description", "category", "platform", "estimate"):
                if existing.get(field):
                    detected[field] = existing[field]
        except Exception:
            pass

    return {
        "staging_id": staging_id,
        "original_filename": file.filename,
        "zip_sha256": zip_sha256,
        "files": sorted(file_hashes.keys()),
        "file_hashes": file_hashes,
        "has_setup_sh": (contents_root / "setup.sh").exists(),
        "detected": detected,
    }


@app.post("/packages/finalize")
async def packages_finalize(request: Request):
    """Step 2: Generate devkit.json / setup.sh / manifest.json from form data and activate the package."""
    body = await request.json()

    # Validate staging session
    raw_sid = re.sub(r"[^\w\-]", "", str(body.get("staging_id", "")))
    staging_path = STAGING_DIR / raw_sid
    if not staging_path.exists():
        return JSONResponse(
            {"error": "Upload session expired — please re-upload the zip file"},
            status_code=400,
        )

    # Validate required form fields
    for field in ("name", "version", "description", "category", "platform"):
        if not str(body.get(field, "")).strip():
            return JSONResponse({"error": f"'{field}' is required"}, status_code=400)

    # Normalise tool ID
    raw_id = str(body.get("id", body.get("name", ""))).strip()
    tool_id = re.sub(r"[^a-z0-9\-]", "-", raw_id.lower()).strip("-")
    tool_id = re.sub(r"-{2,}", "-", tool_id)
    if not tool_id:
        return JSONResponse({"error": "Package ID is invalid"}, status_code=400)

    builtin_ids = {t["id"] for t in TOOLS if t.get("source") == "builtin"}
    if tool_id in builtin_ids:
        return JSONResponse(
            {"error": f"ID '{tool_id}' conflicts with a built-in tool"},
            status_code=409,
        )

    # Resolve staging contents
    cr_file = staging_path / "contents_root.txt"
    if not cr_file.exists():
        return JSONResponse({"error": "Staging data missing — please re-upload"}, status_code=400)
    contents_root = Path(cr_file.read_text(encoding="utf-8").strip())

    # Move contents to final destination
    dest = USER_PACKAGES_DIR / tool_id
    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(contents_root), str(dest))
    shutil.rmtree(str(staging_path), ignore_errors=True)

    name = body["name"].strip()
    version = body["version"].strip()

    import getpass as _getpass
    try:
        uploader = _getpass.getuser()
    except Exception:
        uploader = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"

    # Write devkit.json from form data (always authoritative over any existing one)
    devkit: dict = {
        "id": tool_id,
        "name": name,
        "version": version,
        "category": body["category"].strip(),
        "platform": body["platform"].strip(),
        "description": body["description"].strip(),
        "setup": f"user-packages/{tool_id}/setup.sh",
        "receipt_name": tool_id,
        "uses_prebuilt": False,
        "uploaded_by": uploader,
        "uploaded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if body.get("estimate", "").strip():
        devkit["estimate"] = body["estimate"].strip()
    (dest / "devkit.json").write_text(
        json.dumps(devkit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Generate setup.sh template if the zip didn't include one
    if not (dest / "setup.sh").exists():
        _generate_setup_sh(dest, tool_id, name, version)

    # Write manifest.json with file checksums
    file_hashes: dict = body.get("file_hashes", {})
    manifest = {
        "tool": tool_id,
        "version": version,
        "zip_sha256": body.get("zip_sha256", ""),
        "generated_by": "airgap DevKit Manager",
        "files": [{"path": p, "sha256": h} for p, h in sorted(file_hashes.items())],
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    _reload_tools()
    return {"ok": True, "id": tool_id, "name": name}


@app.delete("/packages/staging/{staging_id}")
async def cancel_staging(staging_id: str):
    """Clean up a preflight staging directory when the user cancels the wizard."""
    safe = re.sub(r"[^\w\-]", "", staging_id)
    staging_path = STAGING_DIR / safe
    if staging_path.exists():
        shutil.rmtree(str(staging_path), ignore_errors=True)
    return {"ok": True}


@app.delete("/packages/{tool_id:path}")
async def delete_package(tool_id: str):
    """Remove a user-uploaded package from user-packages/. Built-in tools are protected."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)
    if tool.get("source") != "user":
        return JSONResponse({"error": "Built-in tools cannot be removed via the UI"}, status_code=403)

    safe_id = re.sub(r"[^\w\-]", "-", tool_id)
    package_dir = USER_PACKAGES_DIR / safe_id
    if package_dir.exists():
        shutil.rmtree(str(package_dir))

    _reload_tools()
    return {"ok": True, "id": tool_id}


# ---------------------------------------------------------------------------
# Sub-package install / status (pip packages, VS Code extensions)
# ---------------------------------------------------------------------------

def _find_devkit_python(prefix: Path) -> Optional[str]:
    for p in [
        prefix / "python" / "python.exe",
        prefix / "python" / "bin" / "python3",
        prefix / "python" / "bin" / "python",
    ]:
        if p.exists():
            return str(p)
    return None


def _pip_vendor_dir() -> Optional[Path]:
    d = REPO_ROOT / "tools" / "languages" / "python" / "pip-packages"
    return d if d.exists() else None


@app.get("/api/subpkg-status")
async def subpkg_status(tool_id: str):
    """Return per-item install status for a plugin tool (pip packages / VS Code extensions)."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    prefix = _current_prefix()

    if tool_id == "pip-packages":
        python = _find_devkit_python(prefix)
        if not python:
            return {"available": False, "status": {},
                    "error": "DevKit Python not installed — install it from the Languages section first"}
        try:
            r = subprocess.run(
                [python, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=20,
            )
            pip_list = json.loads(r.stdout or "[]")
            # Map lowercase name → installed version
            installed: dict[str, str] = {p["name"].lower(): p["version"] for p in pip_list}
            return {
                "available": True,
                "status": {p["name"]: p["name"].lower() in installed
                           for p in tool.get("packages", [])},
                # Actual installed versions (may differ from devkit.json after upgrades)
                "installed_versions": {
                    p["name"]: installed.get(p["name"].lower())
                    for p in tool.get("packages", [])
                },
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    if tool_id == "vscode-extensions":
        try:
            r = subprocess.run(
                ["code", "--list-extensions"],
                capture_output=True, text=True, timeout=10,
            )
            installed = {x.strip().lower() for x in r.stdout.splitlines() if x.strip()}
            return {
                "available": True,
                "status": {e["id"]: e["id"].lower() in installed
                           for e in tool.get("extensions", [])},
            }
        except FileNotFoundError:
            return {"available": False, "status": {},
                    "error": "'code' not found on PATH — is VS Code installed?"}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    return {"available": True, "status": {}}


@app.get("/subpkg-install")
async def subpkg_install_stream(tool_id: str, pkg_id: str, uninstall: bool = False):
    """SSE stream for installing/uninstalling a single sub-package item."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    prefix = _current_prefix()
    verb = "Uninstalling" if uninstall else "Installing"

    async def stream():
        # ── pip packages ──────────────────────────────────────────────────────
        if tool_id == "pip-packages":
            python = _find_devkit_python(prefix)
            if not python:
                yield "data: ✗ DevKit Python not installed — install it from the Languages section first\n\n"
                yield "data: DONE:failed\n\n"
                return
            if uninstall:
                cmd = [python, "-m", "pip", "uninstall", "-y", pkg_id]
            else:
                vendor = _pip_vendor_dir()
                cmd = [python, "-m", "pip", "install", pkg_id]
                if vendor:
                    cmd += ["--no-index", f"--find-links={vendor}"]
            yield f"data: {verb} {pkg_id}…\n\n"

        # ── VS Code extensions ────────────────────────────────────────────────
        elif tool_id == "vscode-extensions":
            ext = next((e for e in tool.get("extensions", []) if e["id"] == pkg_id), None)
            if not ext:
                yield f"data: ✗ Extension '{pkg_id}' not found in manifest\n\n"
                yield "data: DONE:failed\n\n"
                return
            if uninstall:
                cmd = ["code", "--uninstall-extension", pkg_id]
            else:
                # Prefer a local .vsix file if available
                vsix_dir = REPO_ROOT / "tools" / "dev-tools" / "vscode-extensions"
                short = pkg_id.split(".")[-1].lower()
                vsix = next((f for f in vsix_dir.glob("*.vsix")
                             if short in f.name.lower()), None)
                cmd = (["code", "--install-extension", str(vsix)]
                       if vsix else ["code", "--install-extension", pkg_id])
            yield f"data: {verb} {ext.get('name', pkg_id)}…\n\n"

        else:
            yield f"data: ✗ Sub-package operations not supported for '{tool_id}'\n\n"
            yield "data: DONE:failed\n\n"
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_to_bash_path(REPO_ROOT),
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield f"data: ✓ {pkg_id} {'removed' if uninstall else 'installed'}\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ Failed (exit {proc.returncode})\n\n"
                yield "data: DONE:failed\n\n"
        except FileNotFoundError as exc:
            yield f"data: ✗ Command not found: {exc}\n\n"
            yield "data: DONE:failed\n\n"
        except Exception as exc:
            yield f"data: ERROR: {exc}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/open-file")
async def open_file(path: str):
    """Open a file in the OS default application (Notepad, gedit, etc.)."""
    file_path = Path(path)
    if not file_path.exists():
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
    try:
        if OS == "windows":
            os.startfile(str(file_path))
        else:
            subprocess.Popen(["xdg-open", str(file_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "os": OS, "prefix": str(_current_prefix())}


# ---------------------------------------------------------------------------
# Internet connectivity check
# ---------------------------------------------------------------------------

def _internet_check() -> dict:
    """Quick TCP reachability test. Returns {online, host, latency_ms}."""
    import socket
    candidates = [("pypi.org", 443), ("1.1.1.1", 53), ("8.8.8.8", 53)]
    for host, port in candidates:
        try:
            t0 = time.time()
            with socket.create_connection((host, port), timeout=3):
                pass
            return {"online": True, "host": host, "latency_ms": int((time.time() - t0) * 1000)}
        except Exception:
            continue
    return {"online": False, "host": None, "latency_ms": None}


@app.get("/api/internet-check")
async def api_internet_check():
    return _internet_check()


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------

@app.get("/api/check-updates")
async def api_check_updates():
    """Check for outdated pip packages and VS Code extension/version info."""
    result: dict = {
        "online": False, "online_host": None, "latency_ms": None,
        "pip": [], "pip_error": None,
        "vscode_version": None, "vscode_extensions": [], "vscode_error": None,
    }

    # Internet check
    inet = _internet_check()
    result["online"] = inet["online"]
    result["online_host"] = inet.get("host")
    result["latency_ms"] = inet.get("latency_ms")

    # pip outdated — run regardless of internet so local installs are visible
    prefix = _current_prefix()
    python = _find_devkit_python(prefix) or sys.executable
    try:
        r = subprocess.run(
            [python, "-m", "pip", "list", "--outdated", "--format=json"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            result["pip"] = json.loads(r.stdout or "[]")
        elif r.stderr:
            result["pip_error"] = r.stderr.strip()
    except Exception as exc:
        result["pip_error"] = str(exc)

    # VS Code version
    try:
        r = subprocess.run(["code", "--version"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().splitlines()
        result["vscode_version"] = lines[0] if lines else None
    except FileNotFoundError:
        result["vscode_error"] = "'code' not found — is VS Code on PATH?"
    except Exception as exc:
        result["vscode_error"] = str(exc)

    # VS Code extensions with installed versions
    try:
        r = subprocess.run(
            ["code", "--list-extensions", "--show-versions"],
            capture_output=True, text=True, timeout=10,
        )
        exts = []
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if "@" in line:
                ext_id, _, ver = line.partition("@")
                exts.append({"id": ext_id, "version": ver})
            elif line:
                exts.append({"id": line, "version": None})
        result["vscode_extensions"] = exts
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Update / upgrade endpoints (SSE streams — require internet for pip upgrades)
# ---------------------------------------------------------------------------

@app.get("/updates/pip")
async def updates_pip(pkg: Optional[str] = None, all: bool = False):
    """SSE: upgrade pip packages. ?all=1 upgrades all outdated; ?pkg=name upgrades one."""
    prefix = _current_prefix()
    python = _find_devkit_python(prefix) or sys.executable

    async def stream():
        if not pkg and not all:
            yield "data: ✗ No package specified\n\n"
            yield "data: DONE:failed\n\n"
            return

        if all:
            yield "data: Checking for outdated packages...\n\n"
            try:
                r = subprocess.run(
                    [python, "-m", "pip", "list", "--outdated", "--format=json"],
                    capture_output=True, text=True, timeout=30,
                )
                outdated = json.loads(r.stdout or "[]")
            except Exception as exc:
                yield f"data: ✗ Could not list outdated: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return
            if not outdated:
                yield "data: ✓ All packages are up to date\n\n"
                yield "data: DONE:success\n\n"
                return
            packages = [p["name"] for p in outdated]
            yield f"data: Upgrading {len(packages)} package(s): {', '.join(packages)}\n\n"
        else:
            packages = [pkg]
            yield f"data: Upgrading {pkg}...\n\n"

        cmd = [python, "-m", "pip", "install", "--upgrade"] + packages
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield "data: ✓ Upgrade complete\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ pip exited with code {proc.returncode}\n\n"
                yield "data: DONE:failed\n\n"
        except Exception as exc:
            yield f"data: ERROR: {exc}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/updates/vscode-extensions")
async def updates_vscode_extensions(ext_id: Optional[str] = None, all: bool = False):
    """SSE: VS Code extension updates are not supported in air-gapped mode.
    Extensions must be mirrored offline via scripts/fetch-vscode-extensions.py."""

    async def stream():
        yield "data: ✗ VS Code extension updates require internet access.\n\n"
        yield "data: \n\n"
        yield "data: To update extensions offline:\n\n"
        yield "data:   1. On an internet-connected machine run:\n\n"
        yield "data:      python3 scripts/fetch-vscode-extensions.py --from-installed\n\n"
        yield "data:   2. Copy the downloaded .vsix files into:\n\n"
        yield "data:      tools/dev-tools/vscode-extensions/vendor/\n\n"
        yield "data:   3. Update tools/dev-tools/vscode-extensions/manifest.json with the\n\n"
        yield "data:      generated manifest-new.json entries.\n\n"
        yield "data:   4. Run the VS Code Extensions install from this dashboard.\n\n"
        yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")