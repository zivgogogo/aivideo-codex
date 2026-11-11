#!/bin/zsh
set -eu
INIT_WORKSPACE="${0:A:h}"
cd "$INIT_WORKSPACE"
exec python3 scripts/setup_project.py
