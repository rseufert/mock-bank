#!/usr/bin/env bash
#
# A guided tour of mock-bank in curl. It grows with each release; today it
# covers the control plane.
#
#   bash examples/demo.sh                    # against http://127.0.0.1:8080
#   BASE=http://host:9000 bash examples/demo.sh
#
# Start the mock first:
#
#   python3 -m mockbank --port 8080
set -eu

BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Is it up?"
curl -sf "$BASE/_mock/health"

say "What does it know?"
curl -sf "$BASE/_mock/state"

say "Which messages does it speak? The dictionary it reads and writes by"
curl -sf "$BASE/_mock/dictionary" | head -c 600; echo

say "Ask for something it does not do yet: it says what it does"
curl -s -X POST --data-binary '<Document/>' "$BASE/payments"

say "Start again"
curl -sf -X POST "$BASE/_mock/reset"
