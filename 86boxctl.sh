#!/bin/sh
# Compatibility entry point. The controller itself is dependency-free Python.
set -eu
exec python3 "$(dirname "$0")/86boxctl.py" "$@"
