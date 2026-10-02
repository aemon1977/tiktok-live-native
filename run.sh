#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"; mkdir -p logs config
python3 selftest.py >logs/selftest.log 2>&1 || { cat logs/selftest.log; exit 1; }
exec python3 app.py
