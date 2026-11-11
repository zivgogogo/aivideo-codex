"""Wan 2.7 image/video reference generation; see --help or --dry-run."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai.video.cli import main

if __name__ == '__main__':
    raise SystemExit(main(['wan-ref', *sys.argv[1:]]))
