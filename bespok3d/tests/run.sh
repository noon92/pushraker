#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# The plugin's tests, standard library only (run from anywhere).
set -e
export PYTHONDONTWRITEBYTECODE=1
here=$(dirname "$0")
python3 "$here/test_manifest.py"
python3 "$here/test_print_hooks.py"
