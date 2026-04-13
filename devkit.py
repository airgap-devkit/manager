#!/usr/bin/env python3
"""
airgap-cpp-devkit — DevKit Manager Launcher
Starts the local web server and opens the browser.

USAGE:
  python devkit.py [--port 8080] [--no-browser]

OPTIONS:
  --port <n>      Port to listen on (default: 8080)
  --no-browser    Don't open browser automatically
  --host <addr>   Host to bind (default: 127.0.0.1)
"""
import argparse
import os
import platform
import subprocess
import sys
import time
import threading
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LAUNCHER_DIR = Path(__file__).parent
APP_DIR = LAUNCHER_DIR / "app"
VENV_DIR = LAUNCHER_DIR / ".venv-devkit"
REQUIREMENTS = ["fastapi>=0.110.0", "uvicorn>=0.27.0", "python-multipart>=0.0.9", "jinja2>=3.1.0", "aiofiles>=23.0.0"]


def _os() -> str:
    s = platform.system().lower()
    return "windows" if ("windows" in s or os.environ.get("MSYSTEM")) else "linux"


OS = _os()


# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------
def _pip_install(packages: list, python_bin: str):
    print("[devkit] Installing dependencies...")
    pip_flags = [
        "--quiet",
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--root-user-action=ignore",
    ]
    base_cmd = [python_bin, "-m", "pip", "install"] + pip_flags + packages
    # Try local vendor dir first (air-gap)
    vendor_dir = LAUNCHER_DIR / "vendor"
    if vendor_dir.exists():
        cmd = base_cmd + ["--no-index", f"--find-links={vendor_dir}"]
    else:
        cmd = base_cmd
    result = subprocess.run(cmd)
    if result.returncode != 0:
        # Retry without no-index (network available)
        cmd2 = [python_bin, "-m", "pip", "install"] + pip_flags + packages
        subprocess.run(cmd2, check=True)


def _ensure_deps():
    """Ensure FastAPI + uvicorn are available."""
    try:
        import fastapi  # noqa
        import uvicorn  # noqa
        return sys.executable
    except ImportError:
        pass
    _pip_install(REQUIREMENTS, sys.executable)
    return sys.executable


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def _start_server(host: str, port: int):
    # Re-execute with correct python if needed
    python_bin = _ensure_deps()

    if python_bin != sys.executable:
        # Re-launch with venv python
        os.execv(python_bin, [python_bin, __file__, "--host", host, "--port", str(port), "--no-browser-relaunch"])

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
        app_dir=str(LAUNCHER_DIR),
    )


def _open_browser(host: str, port: int, delay: float = 1.5):
    """Open browser after server starts."""
    time.sleep(delay)
    url = f"http://{host}:{port}"
    print(f"[devkit] Opening {url}")
    webbrowser.open(url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="DevKit Manager")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-browser-relaunch", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    print(f"[devkit] airgap-cpp-devkit Manager starting at http://{args.host}:{args.port}")
    print("[devkit] Press Ctrl+C to stop")

    if not args.no_browser and not args.no_browser_relaunch:
        t = threading.Thread(target=_open_browser, args=(args.host, args.port), daemon=True)
        t.start()

    _start_server(args.host, args.port)


if __name__ == "__main__":
    main()