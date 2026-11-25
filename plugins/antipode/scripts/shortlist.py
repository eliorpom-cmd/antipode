#!/usr/bin/env python3
"""Draw the 5 requests shown to a donor.

Input: JSON from `gh issue list --json number,title,body,labels,createdAt,author`,
plus the central repo's stats.json. Rules (DEFINITION.md §5):
- only requests labelled open or relay, compatible with the donor's agent,
  whose size fits the donor's ceiling, not written by the donor;
- relays come first (half-done work is worth more than new work);
- weighted random draw: clarity score × 1.5 if the requester donated in the last
  30 days × 1.3 if the request is less than 7 days old;
- at most one request per requester.
"""
import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_request import parse  # noqa: E402

SLOTS = 5
# Minimum donation ceiling (% of the weekly limit) per size. Rough guess, to calibrate in beta.
MIN_CEILING = {"S": 0.0, "M": 5.0, "L": 10.0}
DAY = 86400


def labels_of(issue):
    return {label["name"] for label in issue.get("labels", [])}


def clarity(labels):
    for name in labels:
        if name.startswith("clarity:"):
            return int(name.split(":")[1])
    return 3


def agent_ok(request_agent, donor_agent):
    if request_agent in (None, "Either"):
        return True
    return request_agent.lower().startswith(donor_agent)


def languages(issue):
    return {name[5:] for name in labels_of(issue) if name.startswith("lang:")}


def weight(issue, stats, now, prefer=()):
    w = float(clarity(labels_of(issue)))
    if languages(issue) & set(prefer):
        w *= 1.5  # same stack as the donor's current work: their session starts with useful context
    author = issue["author"]["login"]
    last = (stats.get("people", {}).get(author) or {}).get("last_donation_at")
    if last and now - parse_time(last) <= 30 * DAY:
        w *= 1.5
    if now - parse_time(issue["createdAt"]) <= 7 * DAY:
        w *= 1.3
    return w


def language_slug(language):
    """Same normalisation as the bot's lang: labels."""
    return re.sub(r"[^a-z0-9+#.-]+", "-", (language or "").strip().lower()).strip("-")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def eligible(issues, donor, agent, ceiling):
    for issue in issues:
        labels = labels_of(issue)
        if not labels & {"open", "relay"} or labels & {"claimed", "review", "flagged", "question"}:
            continue
        if issue["author"]["login"].lower() == donor.lower():
            continue
        request = parse(issue.get("body", ""))
        if not agent_ok(request.get("agent"), agent):
            continue
        if ceiling < MIN_CEILING.get(request.get("size") or "L", 10.0):
            continue
        yield issue, request


def weighted_order(items, weights, rng):
    # Efraimidis–Spirakis: sampling without replacement proportional to weight.
    keyed = [(rng.random() ** (1.0 / w), item) for item, w in zip(items, weights) if w > 0]
    return [item for _, item in sorted(keyed, key=lambda pair: pair[0], reverse=True)]


def draw(issues, stats, donor, agent, ceiling, now, rng, prefer=()):
    candidates = list(eligible(issues, donor, agent, ceiling))
    relays = [c for c in candidates if "relay" in labels_of(c[0])]
    fresh = [c for c in candidates if "relay" not in labels_of(c[0])]
    ordered = []
    for group in (relays, fresh):
        ordered += weighted_order(group, [weight(c[0], stats, now, prefer) for c in group], rng)
    picked, requesters = [], set()
    for issue, request in ordered:
        author = issue["author"]["login"].lower()
        if author in requesters:
            continue
        requesters.add(author)
        picked.append({
            "number": issue["number"],
            "title": issue["title"],
            "repo": request.get("repo"),
            "pitch": request.get("pitch"),
            "goal": request.get("goal"),
            "size": request.get("size"),
            "model": request.get("model"),
            "relay": "relay" in labels_of(issue),
            "languages": sorted(languages(issue)),
            "matches_your_stack": bool(languages(issue) & set(prefer)),
        })
        if len(picked) == SLOTS:
            break
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issues", required=True, help="gh issue list JSON file, or - for stdin")
    parser.add_argument("--stats", help="stats.json of the central repo")
    parser.add_argument("--donor", required=True, help="donor's GitHub login")
    parser.add_argument("--agent", choices=("claude", "codex"), required=True)
    parser.add_argument("--ceiling", type=float, required=True, help="%% of weekly limit")
    parser.add_argument("--prefer", default="", help="comma-separated languages of the donor's work, e.g. swift,typescript")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--now", type=float)
    args = parser.parse_args()
    # gh failures leave empty or error files: treat them as "nothing to show", not a crash.
    try:
        source = sys.stdin if args.issues == "-" else open(args.issues)
        issues = json.load(source)
    except (OSError, ValueError):
        issues = []
    if not isinstance(issues, list):
        issues = []
    stats = {}
    try:
        with open(args.stats) as f:
            stats = json.load(f) if args.stats else {}
    except (OSError, ValueError, TypeError):
        stats = {}
    if args.ceiling <= 0:
        print("[]")
        return
    now = args.now or datetime.now(timezone.utc).timestamp()
    prefer = tuple(filter(None, (language_slug(x) for x in args.prefer.split(","))))
    picked = draw(issues, stats, args.donor, args.agent, args.ceiling, now, random.Random(args.seed), prefer)
    print(json.dumps(picked, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
