#!/usr/bin/env bash
# usage: all.sh [runs-dir]
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export K7E_CHASSIS_RUNS="${1:-${K7E_CHASSIS_RUNS:-${TMPDIR:-/tmp}/k7e-chassis-runs}}"
cd "$here" || exit 1
python3 sim.py base >/dev/null 2>&1
python3 sim.py frozen --frozen >/dev/null 2>&1
python3 sim.py nocompile --no-compile >/dev/null 2>&1
python3 sim.py alldurable --all-durable >/dev/null 2>&1
python3 sim.py obedient --obedient >/dev/null 2>&1
python3 sim.py oracle --oracle >/dev/null 2>&1
python3 summ.py base frozen nocompile alldurable obedient oracle
