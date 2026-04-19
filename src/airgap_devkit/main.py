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
import tarfile
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
APP_DIR = Path(__file__).parent    # .../manager/src/airgap_devkit/
# If launched via launch.sh, DEVKIT_TOOLS_ROOT=<repo>/tools is set; derive repo root from it.
# Fallback: go up one extra level for standalone/pip-installed use.
_env_tools = os.environ.get("DEVKIT_TOOLS_ROOT")
REPO_ROOT = Path(_env_tools).parent if _env_tools else APP_DIR.parent.parent.parent
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


def _win_local_appdata() -> str:
    """Return the true Windows %LOCALAPPDATA% path even when MSYS2 has stripped it.

    MSYS2 bash sets USERPROFILE and HOME to POSIX paths (/home/<user>) before
    spawning Python, so os.environ["LOCALAPPDATA"] may be absent and
    Path.home() may return a non-Windows path.  Fall back to the Windows
    Shell API (SHGetFolderPathW) which is immune to MSYS2 environment mangling.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    # Accept only a proper Windows absolute path (drive letter + colon)
    if local and len(local) > 1 and local[1] == ":":
        return local
    # Shell API fallback — always returns the real Windows path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_LOCAL_APPDATA = 0x001c  →  C:\Users\<user>\AppData\Local
        ctypes.windll.shell32.SHGetFolderPathW(0, 0x001c, 0, 0, buf)
        if buf.value and len(buf.value) > 1 and buf.value[1] == ":":
            return buf.value
    except Exception:
        pass
    # Last resort: derive from USERPROFILE if it looks like a Windows path
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile and len(userprofile) > 1 and userprofile[1] == ":":
        return userprofile + "\\AppData\\Local"
    return str(Path.home() / "AppData" / "Local")


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
        return Path(_win_local_appdata()) / "airgap-cpp-devkit"
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


def _find_bash() -> str:
    """Return the path to the correct bash interpreter.

    On Windows, WSL bash (C:\\Windows\\System32\\bash.exe) is often found before
    Git Bash on the PATH and runs scripts in a Linux environment, breaking all
    Windows platform detection.  Walk PATH explicitly and skip System32.
    """
    if OS != "windows":
        return "bash"
    path_sep = ";"
    for d in os.environ.get("PATH", "").split(path_sep):
        d = d.strip()
        if not d:
            continue
        if "system32" in d.lower():
            continue
        for name in ("bash.exe", "bash"):
            candidate = Path(d) / name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    # Hardcoded fallbacks for standard Git for Windows installations
    for loc in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(loc).exists():
            return loc
    return "bash"


BASH_EXE = _find_bash()


def _to_posix_bash_path(p: Path) -> str:
    """Convert a Windows absolute path to POSIX form for use inside a bash command string.
    E.g. C:\\Users\\foo\\bar -> /c/Users/foo/bar  (needed for inline PATH= assignments in bash -c)."""
    s = str(p).replace("\\", "/")
    if OS == "windows" and len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _install_env(tool: dict) -> dict:
    """Build the subprocess environment for a setup script invocation.

    Passes INSTALL_PREFIX as a native OS path (backslashes on Windows).
    Env vars bypass MSYS2's argv path-conversion, so the script sees the
    correct Windows path even in a Python-spawned subprocess where the
    /c/ virtual filesystem mount is not accessible.
    """
    tool_prefix = _current_prefix() / tool["receipt_name"]
    native_prefix = str(tool_prefix)  # backslash path on Windows
    return {
        **os.environ,
        "AIRGAP_OS": OS,
        "INSTALL_PREFIX": native_prefix,
        # install-mode.sh reads INSTALL_PREFIX_OVERRIDE for the same purpose
        "INSTALL_PREFIX_OVERRIDE": native_prefix,
    }


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
            # Resolve setup path relative to the devkit.json directory so tool
            # authors can write "setup": "setup.sh" without knowing the repo layout.
            setup_val = data.get("setup", "")
            if setup_val:
                abs_setup = (Path(manifest_path).parent / setup_val).resolve()
                try:
                    data["setup"] = str(abs_setup.relative_to(REPO_ROOT.resolve())).replace("\\", "/")
                except ValueError:
                    pass  # outside repo root — leave as-is
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
            data.setdefault("check_cmd", None)
            data.setdefault("receipt_name", tool_id)
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
    """Check whether the prebuilt submodule is initialised and up to date."""
    submodule_dir = REPO_ROOT / "prebuilt"
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
            ["git", "-C", str(REPO_ROOT), "submodule", "status", "prebuilt"],
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
        _has_installed_at = False
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Support both "Key: value" (new) and "key=value" (legacy setup.sh) formats
            colon = line.find(":")
            eq = line.find("=")
            if colon != -1 and (eq == -1 or colon < eq):
                key, val = line[:colon].strip(), line[colon + 1:].strip()
            elif eq != -1:
                key, val = line[:eq].strip(), line[eq + 1:].strip()
            else:
                continue
            k = key.lower().replace(" ", "_").replace("-", "_")
            if k == "version":
                data["version"] = val
            elif k == "status":
                data["status"] = val
            elif k == "date":
                data["date"] = _normalise_date(val)
            elif k == "installed_at":
                data["date"] = _normalise_date(val)
                _has_installed_at = True
            elif k in ("install_path", "install_prefix"):
                data["install_path"] = val
            elif k == "user":
                data["user"] = val
            elif k == "hostname":
                data["hostname"] = val
            elif k in ("log_file", "log"):
                data["log_file"] = val
        # setup.sh scripts write installed_at but no status — treat presence as success
        if data["status"] == "not_installed" and _has_installed_at:
            data["status"] = "success"
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


def _probe_system_exe(tool: dict) -> tuple:
    """Return (sys_found, sys_path, sys_in_devkit) via shutil.which — no subprocess."""
    check_cmd = (tool.get("check_cmd") or "").strip()
    exe_name = check_cmd.split()[0] if check_cmd else ""
    if not exe_name:
        return False, None, False
    prefix = _current_prefix()
    suffixes = [".exe", ".EXE", ""] if OS == "windows" else [""]
    for suf in suffixes:
        found = shutil.which(exe_name + suf)
        if found:
            try:
                in_devkit = Path(found).resolve().is_relative_to(prefix.resolve())
            except (AttributeError, ValueError, OSError):
                in_devkit = str(prefix).lower() in found.lower()
            return True, found, in_devkit
    return False, None, False


def get_tool_status(tool: dict) -> dict:
    receipt_path = _get_receipt_path(tool["receipt_name"])
    receipt = _parse_receipt(receipt_path)
    installed = receipt["status"] == "success"
    # Platform check
    available = tool["platform"] == "both" or tool["platform"] == OS
    setup_rel = tool.get("setup", "")
    uploaded_at_raw = tool.get("uploaded_at", "")
    sys_found, sys_path, sys_in_devkit = _probe_system_exe(tool)
    return {
        **tool,
        "installed": installed,
        "available": available,
        "receipt": receipt,
        "receipt_path": str(receipt_path),
        "setup_abs": str(REPO_ROOT / setup_rel) if setup_rel else "",
        "manifest": _load_manifest(tool),
        "uploaded_at_display": _normalise_date(uploaded_at_raw) if uploaded_at_raw else "",
        "sys_found": sys_found,
        "sys_path": sys_path or "",
        "sys_in_devkit": sys_in_devkit,
    }


def get_all_tools_status() -> list:
    return [get_tool_status(t) for t in TOOLS]


# ---------------------------------------------------------------------------
# Config + connectivity
# ---------------------------------------------------------------------------
_config = DevkitConfig.load()

# ---------------------------------------------------------------------------
# Process registry — all active child processes tracked here so they can be
# killed on Ctrl+C, /shutdown, or browser-window-close.
# ---------------------------------------------------------------------------
_running_procs: set = set()


def _kill_all_procs() -> None:
    """Forcefully kill every tracked child process."""
    for proc in list(_running_procs):
        try:
            if proc.returncode is None:
                proc.kill()
        except Exception:
            pass
    _running_procs.clear()


async def _spawn(*args, **kwargs) -> asyncio.subprocess.Process:
    """Create a subprocess and register it in _running_procs."""
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    _running_procs.add(proc)
    return proc


async def _reap(proc: asyncio.subprocess.Process) -> int:
    """Wait for a registered process to finish and remove it from the registry."""
    try:
        await proc.wait()
    finally:
        _running_procs.discard(proc)
    return proc.returncode


def _make_exception_handler(loop):
    def _handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        loop.default_exception_handler(context)
    return _handler


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_make_exception_handler(loop))
    app.state.mode = detect_mode()
    try:
        yield
    finally:
        # Kill all child processes when the server shuts down (Ctrl+C, SIGTERM, /shutdown)
        _kill_all_procs()


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
    """Stream output of git submodule update --init --recursive prebuilt."""
    async def stream():
        yield "data: Initialising prebuilt submodule...\n\n"
        cmd = [
            "git", "-C", str(REPO_ROOT),
            "submodule", "update", "--init", "--recursive", "prebuilt",
        ]
        proc = None
        try:
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            if rc == 0:
                yield "data: ✓ prebuilt initialised successfully\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ git exited with code {rc}\n\n"
                yield "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/run-tests")
async def run_tests(verbose: bool = False):
    """Stream output of tests/run-tests.sh."""
    async def stream():
        yield "data: Running smoke tests...\n\n"
        cmd = [BASH_EXE, "tests/run-tests.sh", "--os", OS, "--prefix", _to_bash_path(_current_prefix())]
        if verbose:
            cmd.append("--verbose")
        proc = None
        try:
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_to_bash_path(REPO_ROOT),
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            yield "data: DONE:success\n\n" if rc == 0 else "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
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
    try:
        return get_tool_status(tool)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": f"Server error: {exc}"}, status_code=500)


@app.get("/install/{tool_id:path}")
async def install_tool(tool_id: str, rebuild: bool = False):
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    async def stream():
        yield f"data: Installing {tool['name']} {tool['version']}...\n\n"
        cmd = [BASH_EXE, tool["setup"]] + tool.get("setup_args", [])
        if rebuild:
            cmd.append("--rebuild")
        _env = _install_env(tool)
        proc = None
        try:
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env=_env,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            if rc == 0:
                yield "data: ✓ Installation complete\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ Installation failed (exit {rc})\n\n"
                yield "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
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
            cmd = [BASH_EXE, tool["setup"]] + tool.get("setup_args", [])
            if rebuild:
                cmd.append("--rebuild")
            _env = _install_env(tool)
            proc = None
            try:
                proc = await _spawn(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(REPO_ROOT),
                    env=_env,
                )
                async for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        yield f"data: {text}\n\n"
                rc = await _reap(proc)
                yield f"data: {'✓' if rc == 0 else '✗'} {tool['name']} done\n\n"
            except asyncio.CancelledError:
                if proc and proc.returncode is None:
                    proc.kill()
                _running_procs.discard(proc)
                raise
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
        f'install_mode_init "{tool_id}" "{version}" "$@"',
        "",
        "set -x",
        "REBUILD=false",
        'for _arg in "$@"; do [[ "$_arg" == "--rebuild" ]] && REBUILD=true; done',
        "",
        'TOOL_DIR="$INSTALL_PREFIX"',
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
            '# PowerShell Start-Process -Wait properly handles UAC elevation and waits for',
            '# the full installer process tree to exit (unlike cmd.exe start /wait).',
            '_INSTALLER_WIN="$(cygpath -w "$INSTALLER")"',
            'INSTALL_RC=0',
            'powershell.exe -NonInteractive -ExecutionPolicy Bypass -Command \\',
            '  "\\$p = Start-Process -FilePath \'$_INSTALLER_WIN\' -ArgumentList \'/S\' -PassThru -Wait; exit \\$p.ExitCode" \\',
            '  || INSTALL_RC=$?',
            'if [[ $INSTALL_RC -ne 0 ]]; then',
            f'  echo "⚠ Installer exited with code $INSTALL_RC — {exe_name} may still have installed correctly."',
            '  echo "  Check Add/Remove Programs to confirm, then re-run if needed."',
            'fi',
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

_ACCEPTED_EXTS = (".zip", ".tar.xz", ".tar.gz", ".tar.bz2", ".tgz")


def _archive_ext(filename: str) -> str:
    """Return the matched archive extension, or empty string if not accepted."""
    fn = filename.lower()
    for ext in _ACCEPTED_EXTS:
        if fn.endswith(ext):
            return ext
    return ""


def _extract_archive(content: bytes, ext: str, dest: Path) -> None:
    """Extract zip or tar.* archive bytes into dest directory.

    Path-traversal safety is checked in the preflight validation pass before
    this function is called, so we skip the redundant getmembers() scan here.
    Removing it avoids a backward-seek on streaming LZMA decompressors which
    can cause extraction to fail on second upload of the same archive.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if ext == ".zip":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            zf.extractall(str(dest))
    else:
        mode_map = {".tar.xz": "r:xz", ".tar.gz": "r:gz", ".tgz": "r:gz", ".tar.bz2": "r:bz2"}
        mode = mode_map.get(ext, "r:*")
        with tarfile.open(fileobj=io.BytesIO(content), mode=mode) as tf:
            # Use filter='data' on Python 3.12+ to silence the DeprecationWarning
            # and apply safe extraction defaults; fall back gracefully on older Python.
            try:
                tf.extractall(str(dest), filter="data")
            except TypeError:
                tf.extractall(str(dest))


@app.post("/packages/preflight")
async def packages_preflight(file: UploadFile = File(...)):
    """Step 1: Upload archive, analyse contents, return pre-fill hints for the metadata form."""
    try:
        return await _packages_preflight_inner(file)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": f"Unexpected server error: {exc}"}, status_code=500)


async def _packages_preflight_inner(file: UploadFile):
    fname = (file.filename or "").strip()
    ext = _archive_ext(fname)
    if not ext:
        accepted = ", ".join(_ACCEPTED_EXTS)
        return JSONResponse({"error": f"Unsupported file type. Accepted: {accepted}"}, status_code=400)

    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        return JSONResponse({"error": "Archive must be under 200 MB"}, status_code=400)

    try:
        if ext == ".zip":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
            for name in names:
                p = Path(name)
                if p.is_absolute() or ".." in p.parts:
                    return JSONResponse({"error": f"Unsafe path in archive: {name}"}, status_code=400)
        else:
            mode_map = {".tar.xz": "r:xz", ".tar.gz": "r:gz", ".tgz": "r:gz", ".tar.bz2": "r:bz2"}
            with tarfile.open(fileobj=io.BytesIO(content), mode=mode_map.get(ext, "r:*")) as tf:
                names = [m.name for m in tf.getmembers()]
            for name in names:
                p = Path(name)
                if p.is_absolute() or ".." in p.parts:
                    return JSONResponse({"error": f"Unsafe path in archive: {name}"}, status_code=400)
    except (zipfile.BadZipFile, tarfile.TarError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid or corrupt archive: {exc}"}, status_code=400)

    _cleanup_staging()

    staging_id = str(uuid.uuid4())
    staging_path = STAGING_DIR / staging_id
    staging_path.mkdir(parents=True, exist_ok=True)

    zip_sha256 = _sha256_bytes(content)
    (staging_path / f"upload{ext}").write_bytes(content)

    # Extract to contents/
    contents_base = staging_path / "contents"
    try:
        _extract_archive(content, ext, contents_base)
    except Exception as exc:
        shutil.rmtree(str(staging_path), ignore_errors=True)
        return JSONResponse({"error": f"Failed to extract archive: {exc}"}, status_code=400)

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
            for field in ("id", "name", "version", "description", "category", "platform", "estimate", "check_cmd"):
                if existing.get(field):
                    detected[field] = existing[field]
        except Exception:
            pass

    # Auto-suggest check_cmd from detected executables if not already set
    if not detected.get("check_cmd"):
        exes = [f for f in contents_root.rglob("*.exe") if f.is_file()]
        if not exes:
            # Linux: look for files with no extension that might be executables
            exes = [f for f in contents_root.rglob("*") if f.is_file() and not f.suffix and f.name.islower()]
        if exes:
            exe_name = exes[0].stem  # e.g. "filezilla" from "FileZilla.exe"
            detected["check_cmd"] = f"{exe_name} --version"

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
            {"error": "Upload session expired — please re-upload the archive"},
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
        "setup": "setup.sh",
        "receipt_name": tool_id,
        "uses_prebuilt": False,
        "uploaded_by": uploader,
        "uploaded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if body.get("estimate", "").strip():
        devkit["estimate"] = body["estimate"].strip()
    if body.get("check_cmd", "").strip():
        devkit["check_cmd"] = body["check_cmd"].strip()
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


@app.patch("/api/tool/{tool_id:path}/check-cmd")
async def update_check_cmd(tool_id: str, request: Request):
    """Save or clear the check_cmd for a user-uploaded package."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)
    if tool.get("source") != "user":
        return JSONResponse({"error": "Only user packages can be edited"}, status_code=403)

    body = await request.json()
    new_cmd = str(body.get("check_cmd", "")).strip()

    setup_rel = tool.get("setup", "")
    devkit_path = REPO_ROOT / Path(setup_rel).parent / "devkit.json"
    if not devkit_path.exists():
        return JSONResponse({"error": "devkit.json not found"}, status_code=404)

    try:
        data = json.loads(devkit_path.read_text(encoding="utf-8"))
        if new_cmd:
            data["check_cmd"] = new_cmd
        else:
            data.pop("check_cmd", None)
        devkit_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    _reload_tools()
    return {"ok": True, "check_cmd": new_cmd}


@app.get("/check/{tool_id:path}")
async def check_tool(tool_id: str, cmd: Optional[str] = None):
    """SSE: run a version-check or user-supplied command against an installed tool."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    run_cmd = (cmd or "").strip() or (tool.get("check_cmd") or "").strip()
    receipt_name = tool.get("receipt_name", tool_id)
    prefix = _current_prefix()
    tool_bin = prefix / receipt_name / "bin"
    tool_dir = prefix / receipt_name

    async def stream():
        if not run_cmd:
            receipt_path = _get_receipt_path(receipt_name)
            if receipt_path.exists():
                try:
                    for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.strip():
                            yield f"data: {line}\n\n"
                except Exception as e:
                    yield f"data: ERROR reading receipt: {e}\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield "data: ✗ No check command configured and no install receipt found.\n\n"
                yield "data: DONE:failed\n\n"
            return

        # For simple commands (no shell operators) search for the executable
        # directly in the tool's install dirs and invoke it as a native subprocess —
        # no bash PATH translation needed at all, which avoids all POSIX/Windows path
        # conversion issues.  Complex commands (pipes, &&, etc.) fall back to bash
        # with the tool dirs prepended to PATH using the OS-native separator.
        _SHELL_OPS = ('|', '&&', '||', ';', '`', '$(', '>', '<')
        is_simple = not any(op in run_cmd for op in _SHELL_OPS)

        def _find_exe_path(name: str) -> Optional[Path]:
            suffixes = [".exe", ""] if OS == "windows" else [""]
            for search_dir in [tool_bin, tool_dir]:
                for suf in suffixes:
                    c = search_dir / (name + suf)
                    if c.exists():
                        return c
            return None

        yield f"data: $ {run_cmd}\n\n"
        proc = None
        try:
            if is_simple:
                import shlex as _shlex
                parts = _shlex.split(run_cmd)
                resolved = _find_exe_path(parts[0]) if parts else None
                exec_argv = [str(resolved) if resolved else parts[0]] + parts[1:]
                if resolved:
                    yield f"data: # {resolved}\n\n"
                else:
                    import shutil as _shutil_w
                    sys_path = _shutil_w.which(parts[0]) if parts else None
                    yield f"data: # not in devkit prefix — using PATH: {sys_path or parts[0]}\n\n"
                proc = await _spawn(
                    *exec_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            else:
                env = os.environ.copy()
                extras = []
                if tool_bin.exists(): extras.append(str(tool_bin))
                if tool_dir.exists(): extras.append(str(tool_dir))
                if extras:
                    sep = ";" if OS == "windows" else ":"
                    env["PATH"] = sep.join(extras) + sep + env.get("PATH", "")
                proc = await _spawn(
                    BASH_EXE, "-c", run_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=_to_bash_path(REPO_ROOT),
                    env=env,
                )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            if rc == 0:
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ exit {rc}\n\n"
                yield "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/tool-probe/{tool_id:path}")
async def tool_probe(tool_id: str):
    """Fast non-subprocess probe: receipt status + shutil.which detection."""
    import shutil as _shutil_probe

    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    try:
        check_cmd = (tool.get("check_cmd") or "").strip()
        exe_name = check_cmd.split()[0] if check_cmd else ""

        receipt_name = tool.get("receipt_name", tool_id)
        receipt_path = _get_receipt_path(receipt_name)
        receipt = _parse_receipt(receipt_path)
        devkit_installed = receipt["status"] == "success"

        prefix = _current_prefix()
        system_path: Optional[str] = None
        system_found = False
        in_devkit_prefix = False

        if exe_name:
            suffixes = [".exe", ".EXE", ""] if OS == "windows" else [""]
            for suf in suffixes:
                found = _shutil_probe.which(exe_name + suf)
                if found:
                    system_found = True
                    system_path = found
                    try:
                        in_devkit_prefix = Path(found).resolve().is_relative_to(prefix.resolve())
                    except (AttributeError, ValueError, OSError):
                        in_devkit_prefix = str(prefix).lower() in found.lower()
                    break

        return JSONResponse({
            "devkit_installed": devkit_installed,
            "system_found": system_found,
            "system_path": system_path,
            "in_devkit_prefix": in_devkit_prefix,
            "exe_name": exe_name,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": f"Server error: {exc}"}, status_code=500)


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

        proc = None
        try:
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_to_bash_path(REPO_ROOT),
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            if rc == 0:
                yield f"data: ✓ {pkg_id} {'removed' if uninstall else 'installed'}\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ Failed (exit {rc})\n\n"
                yield "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
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


@app.post("/shutdown")
async def shutdown():
    """Gracefully stop the server — kill child processes then exit."""
    async def _stop():
        await asyncio.sleep(0.25)   # let the HTTP response flush
        _kill_all_procs()
        os._exit(0)
    asyncio.create_task(_stop())
    return JSONResponse({"ok": True})


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
        proc = None
        try:
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            rc = await _reap(proc)
            if rc == 0:
                yield "data: ✓ Upgrade complete\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ pip exited with code {rc}\n\n"
                yield "data: DONE:failed\n\n"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            _running_procs.discard(proc)
            raise
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


# ---------------------------------------------------------------------------
# PATH management
# ---------------------------------------------------------------------------

def _devkit_bin_dirs() -> list[tuple[str, Path]]:
    """Return (tool_name, bin_dir) for every installed tool that has a bin dir."""
    prefix = _current_prefix()
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for tool in TOOLS:
        receipt_path = _get_receipt_path(tool["receipt_name"])
        if _parse_receipt(receipt_path).get("status") != "success":
            continue
        tool_dir = prefix / tool["receipt_name"]
        for candidate in [tool_dir / "bin", tool_dir]:
            if candidate in seen or not candidate.exists():
                continue
            # Only include dirs that actually contain executables
            suffixes = {".exe"} if OS == "windows" else set()
            has_exe = any(
                f.is_file() and (not suffixes or f.suffix.lower() in suffixes)
                for f in candidate.iterdir()
            )
            if has_exe:
                seen.add(candidate)
                result.append((tool["name"], candidate))
                break
    return result


def _read_user_path_win() -> str:
    """Read the persistent user PATH from the Windows registry."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                            0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "PATH")
            return val or ""
    except FileNotFoundError:
        return ""


def _write_user_path_win(new_path: str) -> None:
    """Write the user PATH to the Windows registry and broadcast the change."""
    import winreg, ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                        0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
    # Notify running programs (Explorer, etc.) of the change
    HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 2, 5000, None
    )


@app.get("/api/path-status", response_class=JSONResponse)
async def api_path_status():
    """Return which devkit bin dirs are already at the front of the user PATH."""
    bins = _devkit_bin_dirs()
    if OS == "windows":
        try:
            current = _read_user_path_win()
        except Exception:
            current = os.environ.get("PATH", "")
    else:
        current = os.environ.get("PATH", "")

    path_sep = ";" if OS == "windows" else ":"
    path_parts = [p.lower().rstrip("\\/") for p in current.split(path_sep) if p.strip()]
    prefix_lower = str(_current_prefix()).lower().rstrip("\\/")

    entries = []
    needs_fix = False
    for name, d in bins:
        d_str = str(d)
        d_lower = d_str.lower().rstrip("\\/")
        on_path = d_lower in path_parts
        if on_path:
            idx = path_parts.index(d_lower)
            # Priority = no non-devkit dirs appear before this one
            has_priority = all(p.startswith(prefix_lower) for p in path_parts[:idx] if p)
        else:
            has_priority = False
        if not has_priority:
            needs_fix = True
        entries.append({"tool": name, "path": d_str, "on_path": on_path, "has_priority": has_priority})

    cmd_autorun_set = False
    if OS == "windows":
        try:
            import winreg as _winreg
            with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Command Processor",
                                 0, _winreg.KEY_READ) as key:
                val, _ = _winreg.QueryValueEx(key, "AutoRun")
                cmd_autorun_set = "devkit-env.cmd" in (val or "")
        except Exception:
            pass

    return {"entries": entries, "needs_fix": needs_fix,
            "os": OS, "prefix": str(_current_prefix()),
            "cmd_autorun_set": cmd_autorun_set}


@app.get("/fix-path-tool/{tool_id:path}")
async def fix_path_tool(tool_id: str):
    """SSE: add only this tool's bin dir to the user PATH."""
    tool = next((t for t in TOOLS if t["id"] == tool_id), None)
    if not tool:
        return JSONResponse({"error": "Tool not found"}, status_code=404)

    async def stream():
        prefix = _current_prefix()
        receipt_name = tool.get("receipt_name", tool_id)
        tool_dir = prefix / receipt_name
        tool_bin = tool_dir / "bin"

        bin_dir: Optional[Path] = None
        for candidate in [tool_bin, tool_dir]:
            if not candidate.exists():
                continue
            suffixes = {".exe"} if OS == "windows" else set()
            try:
                has_exe = any(
                    f.is_file() and (not suffixes or f.suffix.lower() in suffixes)
                    for f in candidate.iterdir()
                )
            except OSError:
                continue
            if has_exe:
                bin_dir = candidate
                break

        if bin_dir is None:
            # User-uploaded packages (GUI installers, etc.) often install to Program Files
            # and don't place executables in the devkit prefix — skip PATH wiring silently.
            if tool.get("source") == "user":
                yield f"data: ℹ PATH registration skipped — {tool['name']} uses a system installer.\n\n"
                yield f"data: ℹ If needed, add the install directory to PATH manually.\n\n"
                yield "data: DONE:success\n\n"
            else:
                yield f"data: ✗ No executable directory found for {tool['name']}\n\n"
                yield "data: DONE:failed\n\n"
            return

        bin_str = str(bin_dir)

        if OS == "windows":
            try:
                current = _read_user_path_win()
            except Exception as exc:
                yield f"data: ✗ Cannot read user PATH from registry: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return
            path_parts = [p.strip() for p in current.split(";") if p.strip()]
            bin_lower = bin_str.lower().rstrip("\\/")
            if bin_lower in [p.lower().rstrip("\\/") for p in path_parts]:
                yield f"data: ✓ Already on PATH: {bin_str}\n\n"
                yield "data: DONE:success\n\n"
                return
            new_path = bin_str + (";" + current if current else "")
            try:
                _write_user_path_win(new_path)
                yield f"data: ✓ Added to PATH: {bin_str}\n\n"
                yield "data: ✓ Open a new terminal to pick up the change.\n\n"
                yield "data: DONE:success\n\n"
            except Exception as exc:
                yield f"data: ✗ Failed to write registry: {exc}\n\n"
                yield "data: DONE:failed\n\n"
        else:
            bashrc = Path.home() / ".bashrc"
            marker_start = "# >>> airgap-devkit PATH >>>"
            marker_end   = "# <<< airgap-devkit PATH <<<"
            try:
                existing = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
            except Exception as exc:
                yield f"data: ✗ Cannot read ~/.bashrc: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return
            export_line = f'export PATH="{bin_str}:$PATH"'
            if bin_str in existing:
                yield f"data: ✓ Already in ~/.bashrc: {bin_str}\n\n"
                yield "data: DONE:success\n\n"
                return
            import re as _re
            if marker_start in existing:
                new_content = _re.sub(
                    rf"({_re.escape(marker_start)})(.*?)({_re.escape(marker_end)})",
                    rf"\1\2{export_line}\n\3",
                    existing, flags=_re.DOTALL,
                )
            else:
                new_content = existing.rstrip("\n") + f"\n{marker_start}\n{export_line}\n{marker_end}\n"
            try:
                bashrc.write_text(new_content, encoding="utf-8")
                yield f"data: ✓ Added to ~/.bashrc: {bin_str}\n\n"
                yield "data: ✓ Run: source ~/.bashrc  or open a new terminal.\n\n"
                yield "data: DONE:success\n\n"
            except Exception as exc:
                yield f"data: ✗ Cannot write ~/.bashrc: {exc}\n\n"
                yield "data: DONE:failed\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/fix-path")
async def fix_path():
    """SSE: prepend devkit tool bin dirs to the user PATH (registry on Windows, .bashrc on Linux)."""
    async def stream():
        bins = _devkit_bin_dirs()
        if not bins:
            yield "data: ✗ No installed devkit tools found — install some tools first.\n\n"
            yield "data: DONE:failed\n\n"
            return

        if OS == "windows":
            try:
                current = _read_user_path_win()
            except Exception as exc:
                yield f"data: ✗ Cannot read user PATH from registry: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return

            devkit_strs = [str(d) for _, d in bins]
            devkit_lower = {s.lower().rstrip("\\/"): s for s in devkit_strs}

            # Strip devkit dirs from current PATH; they'll be prepended at the front
            remaining = [p for p in current.split(";")
                         if p.strip() and p.lower().rstrip("\\/") not in devkit_lower]

            prefix_lower = str(_current_prefix()).lower().rstrip("\\/")
            path_parts = [p.lower().rstrip("\\/") for p in current.split(";") if p.strip()]

            promoted: list[str] = []
            added: list[str] = []
            already_first: list[str] = []

            for name, d in bins:
                d_str = str(d)
                d_lower = d_str.lower().rstrip("\\/")
                if d_lower in path_parts:
                    idx = path_parts.index(d_lower)
                    has_priority = all(p.startswith(prefix_lower) for p in path_parts[:idx] if p)
                    if has_priority:
                        already_first.append(f"  (already first) {name}: {d_str}")
                    else:
                        promoted.append(d_str)
                        yield f"data: ^ {name}: promoted to front\n\n"
                else:
                    added.append(d_str)
                    yield f"data: + {name}: {d_str}\n\n"

            for msg in already_first:
                yield f"data: {msg}\n\n"

            if not promoted and not added:
                # PATH is already correct; still ensure CMD AutoRun is set
                prefix = _current_prefix()
                env_cmd = prefix / "devkit-env.cmd"
                try:
                    import winreg as _winreg
                    with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER,
                                         r"Software\Microsoft\Command Processor",
                                         0, _winreg.KEY_READ) as key:
                        val, _ = _winreg.QueryValueEx(key, "AutoRun")
                        already_autorun = "devkit-env.cmd" in (val or "")
                except Exception:
                    already_autorun = False
                yield "data: \n\n"
                if already_autorun:
                    yield "data: ✓ All devkit tool directories are already first in your PATH.\n\n"
                    yield "data: ✓ CMD AutoRun is already configured.\n\n"
                else:
                    yield "data: ✓ All devkit tool directories are already first in user PATH.\n\n"
                    yield "data: ⚠ CMD AutoRun not yet set — applying fix...\n\n"
                    set_lines = "\n".join(f'set "PATH={d};%PATH%"' for d in reversed(devkit_strs))
                    env_cmd_text = (
                        "@echo off\n"
                        "rem airgap-devkit PATH — auto-generated, do not edit\n"
                        f"{set_lines}\n"
                    )
                    try:
                        env_cmd.parent.mkdir(parents=True, exist_ok=True)
                        env_cmd.write_text(env_cmd_text, encoding="utf-8")
                        with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER,
                                             r"Software\Microsoft\Command Processor",
                                             0, _winreg.KEY_SET_VALUE) as key:
                            autorun = f'@if exist "{env_cmd}" call "{env_cmd}"'
                            _winreg.SetValueEx(key, "AutoRun", 0, _winreg.REG_SZ, autorun)
                        yield "data: ✓ CMD AutoRun set — open a new CMD to pick up the change.\n\n"
                    except Exception as exc:
                        yield f"data: ⚠ CMD AutoRun not set: {exc}\n\n"
                yield "data: DONE:success\n\n"
                return

            new_path = ";".join(devkit_strs) + (";" + ";".join(remaining) if remaining else "")
            try:
                _write_user_path_win(new_path)
            except Exception as exc:
                yield f"data: ✗ Failed to write registry: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return

            # Write devkit-env.cmd and register it in CMD AutoRun so devkit bins
            # take priority even over system PATH entries (user PATH is always
            # appended after system PATH in CMD; AutoRun fires before any command).
            prefix = _current_prefix()
            env_cmd = prefix / "devkit-env.cmd"
            set_lines = "\n".join(f'set "PATH={d};%PATH%"' for d in reversed(devkit_strs))
            env_cmd_text = (
                "@echo off\n"
                "rem airgap-devkit PATH — auto-generated, do not edit\n"
                f"{set_lines}\n"
            )
            try:
                env_cmd.parent.mkdir(parents=True, exist_ok=True)
                env_cmd.write_text(env_cmd_text, encoding="utf-8")
                import winreg as _winreg
                cmd_key = r"Software\Microsoft\Command Processor"
                with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, cmd_key,
                                     0, _winreg.KEY_SET_VALUE) as key:
                    autorun = f'@if exist "{env_cmd}" call "{env_cmd}"'
                    _winreg.SetValueEx(key, "AutoRun", 0, _winreg.REG_SZ, autorun)
                yield f"data: ✓ CMD AutoRun set — devkit overrides system PATH in CMD too.\n\n"
            except Exception as exc:
                yield f"data: ⚠ CMD AutoRun not set: {exc}\n\n"

            n_changed = len(promoted) + len(added)
            yield "data: \n\n"
            yield f"data: ✓ {n_changed} director{'y' if n_changed==1 else 'ies'} set to front of user PATH.\n\n"
            yield "data: ✓ Open a new terminal (CMD / PowerShell / Git Bash) to pick up the change.\n\n"
            yield "data: DONE:success\n\n"

        else:
            # Linux: append export lines to ~/.bashrc
            bashrc = Path.home() / ".bashrc"
            marker_start = "# >>> airgap-devkit PATH >>>"
            marker_end   = "# <<< airgap-devkit PATH <<<"
            try:
                existing = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
            except Exception as exc:
                yield f"data: ✗ Cannot read ~/.bashrc: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return

            # Strip previous devkit block
            import re as _re
            cleaned = _re.sub(
                rf"{_re.escape(marker_start)}.*?{_re.escape(marker_end)}\n?",
                "", existing, flags=_re.DOTALL
            )

            exports = "\n".join(f'export PATH="{d}:$PATH"' for _, d in bins)
            block = f"\n{marker_start}\n{exports}\n{marker_end}\n"

            try:
                bashrc.write_text(cleaned.rstrip("\n") + block, encoding="utf-8")
            except Exception as exc:
                yield f"data: ✗ Cannot write ~/.bashrc: {exc}\n\n"
                yield "data: DONE:failed\n\n"
                return

            for name, d in bins:
                yield f"data: + {name}: {d}\n\n"
            yield "data: \n\n"
            yield "data: ✓ ~/.bashrc updated. Run:  source ~/.bashrc  or open a new terminal.\n\n"
            yield "data: DONE:success\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")