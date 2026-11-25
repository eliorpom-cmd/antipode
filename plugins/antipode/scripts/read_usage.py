#!/usr/bin/env python3
"""Read the cached Claude Code rate limits and propose a donation ceiling.

Prints JSON: weekly used %, remaining %, reset time, cache age, and the proposed
ceiling (remaining minus a safety margin). Exit code 2 when no usable cache exists.
"""
import argparse
import json
import os
import sys
import time

SAFETY_MARGIN = 5.0  # percentage points kept for the donor's own use
MAX_CACHE_AGE = 30 * 60  # seconds; older data is reported as stale


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "antipode", "usage.json")


def epoch(value):
    """resets_at as epoch seconds, whether the status line sends a number or an ISO date."""
    if value is None or isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except ValueError:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def summarize(cache, now):
    week = cache.get("rate_limits", {}).get("seven_day")
    if not week or week.get("used_percentage") is None:
        return None
    used = float(week["used_percentage"])
    remaining = max(0.0, 100.0 - used)
    resets_at = epoch(week.get("resets_at"))
    age = now - int(cache.get("captured_at", 0))
    # A window whose reset time has passed no longer describes the current week.
    expired = resets_at is not None and resets_at <= now
    five = cache.get("rate_limits", {}).get("five_hour") or {}
    five_used = five.get("used_percentage")
    return {
        "five_hour_used_pct": round(float(five_used), 1) if five_used is not None else None,
        "five_hour_resets_at": epoch(five.get("resets_at")),
        "weekly_used_pct": round(used, 1),
        "weekly_remaining_pct": round(remaining, 1),
        "resets_at": resets_at,
        "hours_to_reset": round((resets_at - now) / 3600, 1) if resets_at else None,
        "cache_age_s": age,
        "stale": age > MAX_CACHE_AGE or expired,
        "proposed_ceiling_pct": round(max(0.0, remaining - SAFETY_MARGIN), 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=cache_path())
    parser.add_argument("--now", type=int, default=None, help="for tests")
    args = parser.parse_args()
    try:
        with open(args.cache) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        print(json.dumps({"error": "no_cache"}))
        return 2
    result = summarize(cache, args.now or int(time.time()))
    if result is None:
        print(json.dumps({"error": "no_weekly_window"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
