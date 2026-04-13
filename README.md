# DevKit Manager

Web-based GUI for managing airgap-cpp-devkit tool installations.
Built with FastAPI + HTMX. Works on Windows 10/11, RHEL 8, RHEL 9.

## Requirements

- Python 3.8+ (system Python, no venv required if deps already installed)
- Internet access OR pre-vendored wheels in `vendor/` for air-gap

## Quick Start

```bash
# From repo root
cd dev-tools/devkit-ui
python devkit.py
```

Opens automatically at http://127.0.0.1:8080

## Options

```
python devkit.py --port 8080        # default port
python devkit.py --host 0.0.0.0     # listen on all interfaces (LAN access)
python devkit.py --no-browser       # don't auto-open browser
```

## Air-Gap Install

Pre-download wheels to `vendor/` before shipping to air-gapped machines:

```bash
pip download fastapi uvicorn python-multipart jinja2 aiofiles \
  --dest dev-tools/devkit-ui/vendor/ \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 38
```

The launcher detects `vendor/` automatically and installs from there.

## Features

- **Dashboard** — visual grid of all tools with installed/not-installed status
- **Install** — click Install or Rebuild on any tool, watch live output in terminal
- **Profiles** — one-click install of curated tool sets (cpp-dev, devops, minimal, full)
- **Logs** — browse all install logs with inline viewer
- **Receipt info** — view install date, path, user, log file for any tool

## File Structure

```
dev-tools/devkit-ui/
  devkit.py          — launcher (start here)
  requirements.txt   — Python dependencies
  vendor/            — pre-downloaded wheels for air-gap (optional)
  app/
    main.py          — FastAPI application
    templates/
      dashboard.html — main UI
      logs.html      — logs browser
    static/
      htmx.min.js    — vendored HTMX (~14KB)
```
