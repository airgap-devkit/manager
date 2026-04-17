"""
DevKit configuration — loads devkit.config.json from the working directory.
All fields are optional with sensible defaults.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DevkitConfig:
    team_name: str = "My Team"
    devkit_name: str = "DevKit Manager"
    theme_color: str = "#1a1a2e"
    dashboard_title: str = "Tool Dashboard"
    hostname: str = "127.0.0.1"
    port: int = 8080
    default_profile: str = "minimal"

    @classmethod
    def load(cls, config_path: Path | None = None) -> "DevkitConfig":
        """Load from devkit.config.json in CWD (or explicit path). Missing file → defaults."""
        if config_path is None:
            config_path = Path.cwd() / "devkit.config.json"
        if not config_path.exists():
            return cls()
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
