"""Qwen image command line entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai.image.client import main

if __name__ == "__main__":
    raise SystemExit(main())
