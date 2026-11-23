#!/bin/sh
set -e
cd "$(dirname "$0")"
python3 -I -m unittest discover -s tests
for f in examples/tail_tie.py examples/verified_evidence.py examples/interval_overlap.py examples/illegal_lower_bound.py; do
  echo "== $f"
  python3 -I "$f" > /dev/null
done
python3 -I large/build_large.py
