"""happyhorse-1.1-i2v usage script; see --help."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai.video.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["i2v", *sys.argv[1:]]))
