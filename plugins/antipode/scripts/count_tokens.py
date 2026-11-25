#!/usr/bin/env python3
"""Count the tokens a donation used, from agent session logs.

Claude Code: JSONL transcripts, one line per content block; every line of one API
response repeats the same message id and usage, so usage is counted once per id.
Codex: rollout JSONL files whose token_count events carry cumulative totals, so
the last total of each file is the file's usage.

Usage: count_tokens.py [--since EPOCH] PATH [PATH ...]   (files or folders, searched for *.jsonl)
Prints JSON totals per model and overall. `weighted` prices each kind of token like the
API does relative to plain input: a cache read costs a tenth of an input token, an output
token five times one. `cost_usd` also prices the model (API list prices by family), which
tracks the plan limits far better than the raw `total` when models differ.
--since keeps only Claude Code messages written at or after that Unix time.
"""
import json
import os
import sys
from datetime import datetime

CLAUDE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Relative cost of each kind of token, input = 1 (same ratios on every current Claude model).
WEIGHTS = {
    "input_tokens": 1.0,
    "output_tokens": 5.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
}


# API input price per million tokens, by model family (Anthropic list prices, June 2026).
FAMILY_INPUT_PRICE = (("fable", 10.0), ("mythos", 10.0), ("opus", 5.0), ("sonnet", 3.0), ("haiku", 1.0))


def weighted(usage):
    return round(sum(usage.get(k, 0) * w for k, w in WEIGHTS.items()))


def input_price(model):
    name = (model or "").lower()
    for family, price in FAMILY_INPUT_PRICE:
        if family in name:
            return price
    return None


def timestamp(record):
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iter_files(paths):
    for path in paths:
        if os.path.isdir(path):
            for root, _, names in os.walk(path):
                for name in sorted(names):
                    if name.endswith(".jsonl"):
                        yield os.path.join(root, name)
        elif os.path.isfile(path):
            yield path
        # A missing folder (no sessions yet) counts as zero, not as a crash.


def add(totals, model, usage):
    bucket = totals.setdefault(model, {k: 0 for k in CLAUDE_FIELDS})
    for key in CLAUDE_FIELDS:
        bucket[key] += int(usage.get(key) or 0)


def count_claude_line(record, seen, totals, since=None):
    if record.get("type") != "assistant":
        return
    if since is not None:
        ts = timestamp(record)
        if ts is None or ts < since:
            return
    message = record.get("message") or {}
    usage = message.get("usage")
    if not usage:
        return
    key = message.get("id") or record.get("requestId") or record.get("uuid")
    if key in seen:
        return
    seen.add(key)
    add(totals, message.get("model", "unknown"), usage)


def codex_usage(record):
    """Return (model, cumulative usage) from a Codex token_count event, else None."""
    payload = record.get("payload") or {}
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info") or {}
    total = info.get("total_token_usage")
    if not total:
        return None
    cached = int(total.get("cached_input_tokens") or 0)
    return {
        # Codex reports cached tokens inside input_tokens; split them like Claude does.
        "input_tokens": int(total.get("input_tokens") or 0) - cached,
        "output_tokens": int(total.get("output_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def count(paths, since=None):
    totals = {}
    seen = set()
    for path in iter_files(paths):
        codex_model = "codex"
        codex_last = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "turn_context":
                    codex_model = (record.get("payload") or {}).get("model", codex_model)
                usage = codex_usage(record)
                if usage:
                    codex_last = usage
                    continue
                count_claude_line(record, seen, totals, since)
        if codex_last:
            add(totals, codex_model, codex_last)
    overall = {k: sum(m[k] for m in totals.values()) for k in CLAUDE_FIELDS}
    overall["total"] = sum(overall.values())
    overall["weighted"] = weighted(overall)
    cost = 0.0
    for model, bucket in totals.items():
        bucket["weighted"] = weighted(bucket)
        price = input_price(model)
        # Unknown models (Codex, local) are priced like Sonnet so they still count
        bucket["cost_usd"] = round(bucket["weighted"] * (price or 3.0) / 1e6, 6)
        cost += bucket["cost_usd"]
    overall["cost_usd"] = round(cost, 6)
    return {"by_model": totals, "overall": overall}


def main():
    args = sys.argv[1:]
    since = None
    if args[:1] == ["--since"] and len(args) > 1:
        since, args = float(args[1]), args[2:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 1
    print(json.dumps(count(args, since), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
