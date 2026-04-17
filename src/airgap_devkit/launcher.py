"""
airgap-devkit launcher — console entry point.

USAGE:
  airgap-devkit [--port 8080] [--host 127.0.0.1] [--no-browser] [--tools .]
"""
from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser
from pathlib import Path

from airgap_devkit.config import DevkitConfig


def _open_browser(host: str, port: int, delay: float = 1.5) -> None:
    time.sleep(delay)
    url = f"http://{host}:{port}"
    print(f"[airgap-devkit] Opening {url}")
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="airgap-devkit — web-based tool dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: 8080)")
    parser.add_argument("--host", default=None, help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    parser.add_argument(
        "--tools",
        default=".",
        metavar="PATH",
        help="Root directory that contains tool subdirs with devkit.json (default: .)",
    )
    args = parser.parse_args()

    # Load config file (CWD/devkit.config.json) — CLI args take precedence
    cfg = DevkitConfig.load()

    host = args.host if args.host is not None else cfg.hostname
    port = args.port if args.port is not None else cfg.port

    tools_root = str(Path(args.tools).resolve())
    os.environ["DEVKIT_TOOLS_ROOT"] = tools_root

    print(f"[airgap-devkit] Starting at http://{host}:{port}")
    print(f"[airgap-devkit] Tools root: {tools_root}")
    print("[airgap-devkit] Press Ctrl+C to stop")

    if not args.no_browser:
        t = threading.Thread(target=_open_browser, args=(host, port), daemon=True)
        t.start()

    import uvicorn

    uvicorn.run(
        "airgap_devkit.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
