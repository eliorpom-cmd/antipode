#!/bin/sh
# Status line wrapper for Claude Code.
# Claude Code sends session JSON on stdin, including rate_limits for Pro, Max and Ultra plans.
# A skill cannot read that stream, so this wrapper caches rate_limits to a file
# the skill can read, then hands the same JSON to the user's previous status line.
#
# Install (done by the skill, with the user's consent):
#   settings.json → "statusLine": {"type": "command", "command": "<path>/usage_statusline.sh"}
#   previous command, if any, stored in ~/.config/antipode/next_statusline

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/antipode"
NEXT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/antipode/next_statusline"
mkdir -p "$CACHE_DIR"

input=$(cat)
PY=$(command -v python3 || command -v python)

# Keep only rate_limits plus a timestamp; ignore sessions where the field is absent.
printf '%s' "$input" | "$PY" -c '
import json, sys, time, os
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
limits = data.get("rate_limits")
if not limits:
    sys.exit(0)
path = os.path.join(sys.argv[1], "usage.json")
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"captured_at": int(time.time()), "rate_limits": limits}, f)
os.replace(tmp, path)
' "$CACHE_DIR"

# Never chain to itself (a second install would loop forever).
if [ -s "$NEXT_FILE" ] && ! grep -qE "usage_statusline|antipode/statusline" "$NEXT_FILE"; then
  printf '%s' "$input" | sh -c "$(cat "$NEXT_FILE")"
else
  printf '%s' "$input" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
week = (d.get("rate_limits") or {}).get("seven_day") or {}
pct = week.get("used_percentage")
model = (d.get("model") or {}).get("display_name", "")
print(f"{model} · week {pct:.0f}%" if pct is not None else model)
'
fi
