#!/bin/bash
# Runs without production mounts, credentials or network services.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export AI_URL='' AI_MODEL=''
python -B -m unittest discover -s scripts -p test_configure.py
python -B -m unittest discover -s bookhub/tests
python -B -m unittest discover -s myshelf/tests
(cd reader-sync && python -B -m unittest discover -s tests)
node myshelf/tests/test_frontend.js
