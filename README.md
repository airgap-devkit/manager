# airgap-devkit-manager

> Web-based package manager UI for [airgap-cpp-devkit](https://github.com/NimaShafie/airgap-cpp-devkit) — built with **FastAPI + HTMX**, zero JavaScript framework, zero build step.

Designed for **air-gapped / network-restricted environments**. Runs entirely offline once deployed.
Works on **Windows 11** (Git Bash / MINGW64) and **RHEL 8/9** (Bash 4.x).

---

## Features

### Tool Management
- **Dashboard** — visual grid of all tools grouped by category, with installed / not-installed status
- **Install & Rebuild** — click to install any tool; live terminal output streams in real time via SSE
- **Uninstall** — remove any installed tool directly from the UI
- **Install Profiles** — one-click install of curated sets: `cpp-dev`, `devops`, `minimal`, `full`
- **Smoke Tests** — run the full post-install test suite from the dashboard

### Package Manager
- **Add Package wizard** — 2-step guided upload: drop any `.zip`, the wizard auto-generates `devkit.json`, `setup.sh`, and a SHA-256 manifest — no knowledge of the file format required
- **Remove packages** — delete user-uploaded packages from the UI
- **Source badges** — `◈ repo` for built-in tools, `⬆ custom` for user-uploaded packages
- **SHA-256 checksums** — displayed in the info popup for every package

### Plugins (Sub-packages)
- **Per-item install / uninstall** for plugin-style tools (Python pip packages, VS Code extensions)
- Live status per item — Install button if not present, Uninstall if already installed
- VS Code extensions: installs from local `.vsix` file if available, falls back to marketplace

### Info & Receipts
- **ℹ popup** per tool — version, install date, install path, setup script path, log file
- **▶ Run** button to re-run a setup script directly from the popup
- **⊞ Open** button to open the install log in the OS default editor
- **Package Origin** section for user-uploaded packages (uploaded by, upload date)

### System
- **Prebuilt-binaries submodule** status card — initialise or re-sync from the UI
- **Install prefix editor** — switch between user and system-wide install paths
- **Manifest-driven tool discovery** — add a `devkit.json` to any tool directory and it appears automatically; no Python edits required

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | 3.8+ |
| OS | Windows 10/11 or RHEL 8/9 |
| Shell | Git Bash (Windows) / Bash 4.x (Linux) |

Python dependencies (`fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `aiofiles`) are
auto-installed by the launcher on first run. For air-gapped machines, pre-download wheels
into `vendor/` first (see [Air-Gap Install](#air-gap-install)).

---

## Quick Start

This repo is a submodule of [airgap-cpp-devkit](https://github.com/NimaShafie/airgap-cpp-devkit).
Clone the parent repo with submodules:

```bash
git clone --recurse-submodules git@github.com:NimaShafie/airgap-cpp-devkit.git
cd airgap-cpp-devkit
bash launch.sh
```

Or start the manager directly:

```bash
cd airgap-devkit-manager
python devkit.py
```

Opens automatically at **http://127.0.0.1:8080**

### Options

```bash
python devkit.py --port 8080        # default port
python devkit.py --host 0.0.0.0     # listen on all interfaces (LAN access)
python devkit.py --no-browser       # don't auto-open browser
```

---

## Air-Gap Install

Pre-download wheels on a machine with internet access, then copy `vendor/` to the air-gapped machine:

```bash
pip download fastapi uvicorn python-multipart jinja2 aiofiles \
  --dest airgap-devkit-manager/vendor/ \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 38
```

The launcher detects `vendor/` automatically and installs from there using `--no-index`.

---

## File Structure

```
airgap-devkit-manager/
  devkit.py              — launcher (start here)
  requirements.txt       — Python dependencies
  vendor/                — pre-downloaded wheels for air-gap (gitignored, optional)
  .devkit-prefix         — local install prefix override (gitignored, machine-specific)
  app/
    main.py              — FastAPI application, all endpoints, tool discovery
    templates/
      dashboard.html     — main UI (HTMX, inline CSS/JS, no build step)
      logs.html          — install log browser
    static/
      htmx.min.js        — vendored HTMX v1.x (~14 KB, no CDN required)
```

---

## Adding a Tool

Tools are discovered automatically from `devkit.json` manifests — no Python edits needed.

1. Create a directory for your tool in the parent repo
2. Add `devkit.json` with the fields below
3. Restart the manager — your tool appears in the dashboard

### `devkit.json` reference

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique slug (used in API and install paths) |
| `name` | yes | Display name |
| `version` | yes | Version string |
| `category` | yes | `Toolchains` / `Build Tools` / `Languages` / `Developer Tools` / `Plugins` / `Frameworks` |
| `platform` | yes | `"both"`, `"windows"`, or `"linux"` |
| `description` | yes | One-sentence description |
| `setup` | yes | Path to `setup.sh` relative to repo root |
| `receipt_name` | yes | Subdirectory under install prefix where `INSTALL_LOG.txt` lives |
| `estimate` | no | Human-readable time estimate (e.g. `~30s`) |
| `uses_prebuilt` | no | `true` if the prebuilt-binaries submodule is required |
| `sort_order` | no | Integer display order (lower = first) |
| `version_label` | no | Override the version string shown in the UI |

---

## API

The manager exposes a REST API used by the UI — useful for scripted installs or CI integration.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tools` | GET | List all tools with status |
| `/api/tool/{id}` | GET | Single tool with receipt details |
| `/install/{id}` | GET | SSE stream — install a tool |
| `/uninstall/{id}` | DELETE | Remove installed tool directory |
| `/install-profile/{id}` | GET | SSE stream — install a profile |
| `/run-tests` | GET | SSE stream — run smoke tests |
| `/api/subpkg-status` | GET | Per-item status for pip/extension tools |
| `/subpkg-install` | GET | SSE stream — install/uninstall one sub-package |
| `/packages/preflight` | POST | Upload zip, analyse, return pre-fill hints |
| `/packages/finalize` | POST | Create package from wizard form data |
| `/packages/{id}` | DELETE | Remove user-uploaded package |
| `/open-file` | GET | Open a file in the OS default application |
| `/health` | GET | Health check — returns OS and prefix |

---

## License

Copyright (c) 2024-present Nima Shafie. Source-available — see [LICENSE](LICENSE) for terms.
Commercial use requires written permission from the author.
