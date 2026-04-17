# airgap-devkit

> Web-based package manager UI for [airgap-cpp-devkit](https://github.com/NimaShafie/airgap-cpp-devkit) — built with **FastAPI + HTMX**, zero JavaScript framework, zero build step.

Designed for **air-gapped / network-restricted environments**. Runs entirely offline once deployed.
Works on **Windows 11** (Git Bash / MINGW64) and **RHEL 8/9** (Bash 4.x).

> **Note:** This repo is the engine. Tool content (devkit.json manifests, setup scripts) lives in [airgap-cpp-devkit](https://github.com/NimaShafie/airgap-cpp-devkit).

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

### Updates & Connectivity
- **Internet connectivity detection** — dashboard badge shows online vs. air-gapped mode automatically
- **Pip update checker** — scans installed pip sub-packages for newer versions; one-click upgrade with live SSE output
- **VS Code extension updater** — offline-first workflow: installs from a local `.vsix` if present, falls back to marketplace when online

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

---

## Installation

```bash
pip install airgap-devkit
```

Then run from the directory that contains your tool tree:

```bash
airgap-devkit
```

---

## Air-Gap Install

Pre-download wheels on a machine with internet access, then copy `vendor/` to the air-gapped machine:

```bash
pip download airgap-devkit \
  --dest vendor/ \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 38
```

Install on the air-gapped machine:

```bash
pip install --no-index --find-links=vendor/ airgap-devkit
```

---

## Quick Start

This repo is a submodule of [airgap-cpp-devkit](https://github.com/NimaShafie/airgap-cpp-devkit).
Clone the parent repo with submodules:

```bash
git clone --recurse-submodules git@github.com:NimaShafie/airgap-cpp-devkit.git
cd airgap-cpp-devkit
bash launch.sh
```

Or install and run standalone:

```bash
pip install airgap-devkit
airgap-devkit
```

Opens automatically at **http://127.0.0.1:8080**

### Options

```bash
airgap-devkit --port 8080           # default port
airgap-devkit --host 0.0.0.0        # listen on all interfaces (LAN access)
airgap-devkit --no-browser          # don't auto-open browser
airgap-devkit --tools /path/to/repo # point at a different tool tree
```

### Configuration file

Copy `devkit.config.json.example` to `devkit.config.json` in your working directory
to customise branding, default port, and profile without CLI flags:

```json
{
  "team_name": "Platform Team",
  "devkit_name": "Internal DevKit",
  "theme_color": "#0d3349",
  "port": 9090
}
```

---

## File Structure

```
airgap-devkit/
  pyproject.toml             — package metadata and build config
  devkit.config.json.example — configuration template
  requirements.txt           — deprecated, see pyproject.toml
  devkit.py                  — deprecated shim (calls airgap-devkit entry point)
  MANIFEST.in                — package data inclusion rules
  src/
    airgap_devkit/
      __init__.py            — package version
      main.py                — FastAPI application, all endpoints, tool discovery
      launcher.py            — console entry point (airgap-devkit command)
      config.py              — DevkitConfig dataclass, loads devkit.config.json
      templates/
        dashboard.html       — main UI (HTMX, inline CSS/JS, no build step)
        logs.html            — install log browser
      static/
        htmx.min.js          — vendored HTMX v1.x (~14 KB, no CDN required)
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
| `sub_packages` | no | Array of sub-package objects for plugin-style tools (see below) |

### Sub-packages (`sub_packages` array)

For tools with individually installable items (pip packages, VS Code extensions), add a `sub_packages` array:

```json
{
  "id": "python-tools",
  "name": "Python Tools",
  "category": "Plugins",
  "sub_packages": [
    { "id": "black", "name": "Black", "type": "pip" },
    { "id": "ms-python.python", "name": "Python Extension", "type": "vscode" }
  ]
}
```

Supported `type` values: `pip`, `vscode`

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
| `/packages/staging/{id}` | DELETE | Cancel and clean up an in-progress upload session |
| `/packages/{id}` | DELETE | Remove user-uploaded package |
| `/api/prefix` | GET | Get current install prefix |
| `/api/prefix` | POST | Set install prefix |
| `/api/prefix` | DELETE | Reset install prefix to default |
| `/api/submodule` | GET | Get prebuilt-binaries submodule status |
| `/init-submodule` | POST | Initialise or re-sync the prebuilt-binaries submodule |
| `/api/connectivity` | GET | Cached connectivity flag (online / airgapped) |
| `/api/internet-check` | GET | Live internet connectivity probe |
| `/api/check-updates` | GET | Check pip + VS Code extension updates |
| `/updates/pip` | GET | SSE stream — upgrade a pip sub-package |
| `/updates/vscode-extensions` | GET | SSE stream — update a VS Code extension |
| `/api/log` | GET | Fetch contents of a tool install log |
| `/logs` | GET | Browse install logs (HTML page) |
| `/open-file` | GET | Open a file in the OS default application |
| `/health` | GET | Health check — returns OS, prefix, and Python info |

---

## License

Copyright (c) 2024–2026 Nima Shafie. Source-available — see [LICENSE](LICENSE) for terms.
Commercial use requires written permission from the author.
