#!/usr/bin/env python3
"""Run the Bailian atomic LLM tool from any working directory."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai.llm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
