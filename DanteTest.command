#!/bin/bash
# Double-click to open DanteTest on a Mac.
cd "$(dirname "$0")"
exec .venv/bin/python src/dantetest.py --device "Dante|USB Audio Device" "$@"
