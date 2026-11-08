#!/bin/sh
# Install the inbox collector on a Mac. Does not open a browser or the shop.
set -eu
cd "$(dirname "$0")/.."
python3.11 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[browser]"
python -m playwright install chromium
echo "Installed into $(pwd)/.venv"
echo "Next, from this same directory:"
echo "  source .venv/bin/activate"
echo "  python -m inbox_collector check-config --config config.login-prep.example.json"
echo "  python -m inbox_collector login-prep --config config.login-prep.example.json"
echo "login-prep only opens a new browser profile. It does not collect, draft, or send."
echo "Do not run the repository root app.py."
