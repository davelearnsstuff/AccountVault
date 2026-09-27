#!/bin/bash
cd "$(dirname "$0")"

# Prefer the project virtual environment — on Debian-based systems PySide6
# can't be installed into the system Python.
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python main.py
fi

exec python3 main.py
