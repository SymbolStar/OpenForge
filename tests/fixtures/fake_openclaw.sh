#!/bin/bash
# Tiny mock for `openclaw agent --local --json …` used by tests.
# Always exits 0 with a valid envelope after FAKE_SLEEP seconds (default 0).
SLEEP=${FAKE_SLEEP:-0}
AGENT="" MSG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent)   AGENT="$2"; shift 2 ;;
    --message) MSG="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ "$SLEEP" != "0" ] && sleep "$SLEEP"
printf '{"payloads":[{"type":"text","text":"[mock %s] reply"}]}\n' "$AGENT"
