#!/usr/bin/env python3
"""Interactive, repeatable AutoRouter setup for supported harnesses."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autorouter.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
