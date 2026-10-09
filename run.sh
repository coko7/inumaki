#!/usr/bin/env bash

# Creates .venv on first run, then starts voice control. Args pass through.

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "Created bare config.toml from config.example.toml."
fi

exec .venv/bin/python -m inumaki "$@"
