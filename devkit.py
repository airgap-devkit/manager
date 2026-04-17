#!/usr/bin/env python3
"""
DEPRECATED — devkit.py is no longer the primary entry point.

Use the 'airgap-devkit' console command instead:

  pip install airgap-devkit
  airgap-devkit [--port 8080] [--host 127.0.0.1] [--no-browser]

Or for air-gapped installs:

  pip install --no-index --find-links=vendor/ airgap-devkit
  airgap-devkit

This shim is kept so that existing scripts using 'python devkit.py' continue to work.
"""
import sys
import warnings

warnings.warn(
    "devkit.py is deprecated. Use the 'airgap-devkit' console command instead "
    "(pip install airgap-devkit).",
    DeprecationWarning,
    stacklevel=1,
)
print("[devkit] WARNING: devkit.py is deprecated — use 'airgap-devkit' instead.", file=sys.stderr)

from airgap_devkit.launcher import main  # noqa: E402

if __name__ == "__main__":
    main()
