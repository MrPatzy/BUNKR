#!/usr/bin/env bash
# Start BUNKR and open it in the browser.
cd "$(dirname "$0")" && exec python3 -m bunkr "$@"
