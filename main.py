#!/usr/bin/env python3
"""Compatibility entry point. The program lives in the dashcam/ package;
prefer `python -m dashcam` or the `dashcam` command installed by install.sh."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashcam.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
