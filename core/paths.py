"""Absolute paths shared by AI clients, APIs and local tools."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "production/projects/default"
