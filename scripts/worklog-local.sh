#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$PROJECT_ROOT/.python-packages:$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m rlwrld_worklog "$@"
