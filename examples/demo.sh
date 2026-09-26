#!/usr/bin/env bash
#
# A guided tour of mock-bank in curl: send one payment file and watch the bank
# answer the way a real one does. CI's smoke job runs this on every push, so it
# is the documentation that cannot rot.
#
#   bash examples/demo.sh                             # http://127.0.0.1:8080
#   BASE=http://host:9000 bash examples/demo.sh
#   BANK_AUTH=user:password bash examples/demo.sh      # a mock started --auth
#
# Start the mock first:
#
#   python3 -m mockbank --port 8080
#
# It asks the mock what it supports and skips what is missing, naming the
# endpoint, rather than requiring a particular command line. A tour that dies
# against a mock started slightly differently is a tour nobody runs twice.
set -eu

BASE="${BASE:-http://127.0.0.1:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SAMPLE="$HERE/../tests/samples/pain001_four_payments.xml"

# A copy of the sample with a different MsgId, for the step that has to send a
# file the bank has not seen before. Removed on the way out, however we leave.
SECOND="$(mktemp -t mockbank-demo.XXXXXX)"
trap 'rm -f "$SECOND"' EXIT

# One place that knows how to reach the mock, so --auth is a variable rather
# than a second copy of the tour.
CURL=(curl -sS)
if [ -n "${BANK_AUTH:-}" ]; then
  CURL+=(-u "$BANK_AUTH")
fi

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  \033[2m%s\033[0m\n' "$*"; }

# JSON is read with the interpreter this project is written in. There is no jq
# here for the same reason there are no dependencies.
json() { python3 -c "$1"; }

say "Is it up?"
"${CURL[@]}" "$BASE/_mock/health"

# What this build answers. Every step below asks before it acts.
SUPPORTED="$("${CURL[@]}" "$BASE/_mock/state" | json '
import json, sys
print("\n".join(json.load(sys.stdin)["supported"]))')"

has()  { printf "%s\n" "$SUPPORTED" | grep -qxF "$1"; }
skip() { note "skipped: this mock does not answer $1"; }

say "What does it know? Its clock, its cutoff, and what it holds"
"${CURL[@]}" "$BASE/_mock/state" | json '
import json, sys
state = json.load(sys.stdin)
clock = state["clock"]
print("  bank time  %s (%s)" % (clock["now"], clock["timezone"]))
print("  cutoff     %s, past it already: %s" % (clock["cutoff"], clock["pastCutoff"]))
print("  accounts   %d, holding %s minor units" % (state["accounts"], state["balances"]))
print("  messages   %s" % state["messages"])'

say "Which accounts does it hold, and how does each one misbehave?"
"${CURL[@]}" "$BASE/_mock/accounts" | json '
import json, sys
print("  %-8s %-22s %12s  %-19s %s" % ("id", "iban", "balance", "behaviour", "closed"))
for a in json.load(sys.stdin):
    print("  %-8s %-22s %12d  %-19s %s"
          % (a["id"], a["iban"], a["balance"], a["behaviour"], a["closed"]))'
note "Balances are minor units: 12500000 is 125,000.00 EUR."
note "INITECH is closed; EURODIS sits at a BIC that resolves to nothing."
note "Each one is a PATCH away from being something else."

if has "POST /_mock/validate"; then
  say "Read the file without sending it: the bank's own reading, as prose"
  "${CURL[@]}" -X POST --data-binary "@$SAMPLE" "$BASE/_mock/validate" | head -4
  note "Nothing was stored. This is the fastest way to show a mapping bug."
else
  skip "POST /_mock/validate"
fi

say "Send it: one file, four payments from ACME"
ANSWER="$("${CURL[@]}" -X POST --data-binary "@$SAMPLE" \
  -H 'Content-Type: application/xml' "$BASE/payments")"
printf '%s' "$ANSWER" | json '
import json, sys
answer = json.load(sys.stdin)
print("  file status %s  (%d accepted, %d rejected)"
      % (answer["status"], answer["accepted"], answer["rejected"]))
print("  %-16s %-9s %-6s %s" % ("EndToEndId", "outcome", "code", "settles"))
for p in answer["payments"]:
    print("  %-16s %-9s %-6s %s" % (p["end_to_end_id"], p["outcome"],
                                    p["reason"] or "-", p["settlement_date"] or "-"))'
note "PART: some accepted, some not. AC04 is the closed creditor account and"
note "RC01 the bank that does not resolve - ISO 20022 codes, not inventions."

# The settlement date the bank chose, which is what the clock is moved to.
SETTLES="$(printf '%s' "$ANSWER" | json '
import json, sys
dates = sorted(p["settlement_date"] for p in json.load(sys.stdin)["payments"]
               if p["settlement_date"])
print(dates[0] if dates else "")')"

if has "GET /_mock/mailbox"; then
  say "Collect the status report the bank wrote"
  "${CURL[@]}" "$BASE/_mock/mailbox?type=pain.002&leave" | json '
import json, sys
for m in json.load(sys.stdin):
    print("  %s, %d bytes, released %s" % (m["type"], m["bytes"], m["releasedAt"]))'

  say "The same report raw, which is what your client parses"
  "${CURL[@]}" "$BASE/_mock/mailbox?type=pain.002&raw" \
    | grep -oE '<(GrpSts|OrgnlEndToEndId|Cd)>[^<]*' \
    | sed -e 's/^</  /' -e 's/>/: /' | head -12
  note "PART at group level, then each rejected payment with its reason code,"
  note "against the EndToEndId your own file gave it."
else
  skip "GET /_mock/mailbox"
fi

if has "POST /_mock/advance" && [ -n "$SETTLES" ]; then
  say "Nothing waits: move bank time to the settlement date, $SETTLES"
  "${CURL[@]}" -X POST "$BASE/_mock/advance?to=$SETTLES" | json '
import json, sys
moved = json.load(sys.stdin)
print("  %s -> %s" % (moved["from"], moved["to"]))
print("  business days crossed: %d" % len(moved["businessDaysCrossed"]))'
  note "Advancing books what came due and releases what the bank owes for it."

  say "The debit notification for what settled"
  "${CURL[@]}" "$BASE/_mock/mailbox?type=camt.054&raw&leave" \
    | grep -oE '<(Amt|EndToEndId)[^>]*>[^<]*' \
    | sed -e 's/^</  /' -e 's/>/: /' | head -8
  note "One camt.054 per account per booking, an entry per payment."

  say "Now let that day end: the statement closes when the clock passes it"
  "${CURL[@]}" -X POST "$BASE/_mock/advance?days=1" | json '
import json, sys
moved = json.load(sys.stdin)
print("  %s -> %s" % (moved["from"], moved["to"]))'
  note "The settlement day had not ended yet, so its statement had not closed."
  note "This is the step that makes the entries appear on one."

  say "End of day: the statement, and balances that have to reconcile"
  "${CURL[@]}" "$BASE/_mock/mailbox?type=camt.053&raw&leave" \
    | python3 "$HERE/statement.py"
else
  skip "POST /_mock/advance"
fi

say "Make it fail on purpose: leave ACME unable to cover what it sends"
"${CURL[@]}" -X PATCH -H 'Content-Type: application/json' \
  --data-binary '{"behaviour": "insufficient-funds", "balance": 100}' \
  "$BASE/_mock/accounts/ACME" | json '
import json, sys
a = json.load(sys.stdin)
print("  ACME is now %s, holding %d minor units" % (a["behaviour"], a["balance"]))'

say "Send the very same file again: a bank does not take one MsgId twice"
"${CURL[@]}" -X POST --data-binary "@$SAMPLE" "$BASE/payments" | json '
import json, sys
answer = json.load(sys.stdin)
print("  file status %s, reason %s" % (answer["status"], answer["reason"]))
print("  %s" % answer["reason_text"])'
note "DUPL, at group level, before any payment is looked at - which is why the"
note "insufficient funds below needs a file the bank has not seen."

say "So give it a new MsgId and send that"
sed 's/ACME-20261001-0001/ACME-20261001-0002/' "$SAMPLE" > "$SECOND"
"${CURL[@]}" -X POST --data-binary "@$SECOND" "$BASE/payments" | json '
import json, sys
answer = json.load(sys.stdin)
print("  file status %s  (%d accepted, %d rejected)"
      % (answer["status"], answer["accepted"], answer["rejected"]))
for p in answer["payments"]:
    print("  %-16s %-9s %s" % (p["end_to_end_id"], p["outcome"], p["reason"] or "-"))'
note "AM04 is insufficient funds, on the two that would have gone through."
note "The file did not change; the bank did."

if has "GET /_mock/requests"; then
  say "What did your client actually send?"
  "${CURL[@]}" "$BASE/_mock/requests?path=/payments" | json '
import json, sys
for r in json.load(sys.stdin):
    print("  %s %s -> %d  at %s" % (r["method"], r["path"], r["status"], r["at"]))'
else
  skip "GET /_mock/requests"
fi

say "Start again: the seed comes back"
"${CURL[@]}" -X POST "$BASE/_mock/reset"

printf '\n\033[1mThat is the tour.\033[0m examples/client.py is the same choreography\n'
printf 'as a client an integrator would copy, matching each answer to its payment.\n'
