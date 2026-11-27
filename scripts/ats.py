#!/usr/bin/env python3
"""Automation for the Antipode central repository, run by GitHub Actions.

Subcommands:
  command   handle a slash command posted as an issue comment (/claim, /release, ...)
  scan      automatic checks on a new or edited request; writes a JSON report
  (review)  peer review happens through /review commands from donors' agents
  sweep     hourly: free stale claims, expire old requests, follow delivered PRs
  harvest   every 10 minutes: turn the Antipode-* trailers pushed by donors' sessions into
            pull requests and records
  stats     rebuild stats.json from the bot's records

Every state change leaves a bot comment carrying a hidden record
(<!-- ats:record {...} -->). stats.json and the sweep read only records written by the
bot accounts, so nobody can forge them by commenting.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_request import FORMAT_URL, parse, problems  # noqa: E402

BOT = "antipode-bot"  # writes every visible comment and opens the pull requests
# Records are trusted only from these accounts; github-actions wrote them before the bot existed.
BOTS = (BOT, "github-actions[bot]", "github-actions")
CLAIM_HOURS = 6
EXPIRE_DAYS = 30
MAX_OPEN_PER_AUTHOR = 5
ACTIVE = {"review", "open", "claimed", "relay"}
# Only the record that ends a bot comment counts: user text echoed above it can't add one.
RECORD_RE = re.compile(r"<!-- ats:record (\{[^\n]*\}) -->\s*\Z")
FROZEN_MARK = "<!-- ats:frozen"

# Paths whose presence in a target repository deserves a closer look at review.
RISKY_PATHS = [
    (re.compile(r"(^|/)\.claude/"), "Claude Code configuration"),
    (re.compile(r"(^|/)\.mcp\.json$"), "MCP server configuration"),
    (re.compile(r"(^|/)\.codex/"), "Codex configuration"),
    (re.compile(r"(^|/)\.husky/"), "git hooks"),
    (re.compile(r"(^|/)\.githooks/"), "git hooks"),
    (re.compile(r"(^|/)\.vscode/tasks\.json$"), "editor tasks"),
    (re.compile(r"(^|/)\.devcontainer/"), "dev container definition"),
    (re.compile(r"(^|/)(CLAUDE|AGENTS|GEMINI)\.md$"), "instructions for AI agents"),
    (re.compile(r"(^|/)\.(cursor|gemini|windsurf)/"), "AI agent configuration"),
    (re.compile(r"(^|/)\.envrc$"), "direnv script"),
    (re.compile(r"(^|/)conftest\.py$"), "pytest startup code"),
    (re.compile(r"\.(exe|dll|so|dylib|bin|jar)$"), "binary"),
]
LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepublish")


# ---------------------------------------------------------------- pure logic

def record_comment(text, record):
    return f"{text}\n\n<!-- ats:record {json.dumps(record, separators=(',', ':'))} -->"


def is_bot(comment):
    return (comment.get("author") or {}).get("login") in BOTS


def records(comments):
    """Records from bot comments, oldest first: at most one per comment, the one that ends it."""
    found = []
    for comment in comments:
        if not is_bot(comment):
            continue
        match = RECORD_RE.search(comment.get("body", ""))
        if match:
            try:
                rec = json.loads(match.group(1))
            except ValueError:
                continue
            if isinstance(rec, dict):
                found.append(rec)
    return follow_renames(found, comments)


def follow_renames(recs, comments):
    """Records keep the login a person had when the bot answered. GitHub shows a comment under its
    author's current login, so a record tied to a comment (its cid) reveals a rename: every record
    of this request then uses the current login, and stats, locks and reviews stay one person."""
    authors = {comment_id(c): (c.get("author") or {}).get("login", "")
               for c in comments if not is_bot(c)}
    renamed = {}
    for rec in recs:
        old = rec.get("donor") or rec.get("by")
        now = authors.get(rec.get("cid", ""), "")
        if old and now and now.lower() not in (old.lower(), "ghost"):
            renamed[old.lower()] = now
    if renamed:
        for rec in recs:
            for key in ("donor", "by"):
                if isinstance(rec.get(key), str) and rec[key].lower() in renamed:
                    rec[key] = renamed[rec[key].lower()]
    return recs


def safe(text, limit=600):
    """User text echoed in a bot comment: no HTML (so no hidden record or frozen mark), bounded."""
    return str(text or "").replace("<", "&lt;").replace(">", "&gt;")[:limit]


def body_hash(body):
    import hashlib
    return hashlib.sha256((body or "").replace("\r\n", "\n").strip().encode()).hexdigest()[:16]


def parse_tokens(text):
    match = re.search(r"tokens=(\d+)", text)
    return min(int(match.group(1)), MAX_TOKENS) if match else 0


def claim_is_live(claim, now):
    """True while a claim record is inside its CLAIM_HOURS lock."""
    return now - (claim.get("at") or 0) <= CLAIM_HOURS * 3600


def too_young(ctx, now):
    created = ctx.get("author_created_at")
    return created is not None and now - created < MIN_ACCOUNT_AGE_DAYS * 86400


REVIEWS_NEEDED = 2
# Throwaway accounts could approve their owner's own request or grief the queue: reviewing, claiming
# and flagging need an account this old. At least one approval must come from a trusted reviewer
# (a maintainer, or a donor with a merged pull request for someone else).
MIN_ACCOUNT_AGE_DAYS = 90
MIN_REQUESTER_AGE_DAYS = 30  # throwaway accounts can't flood the queue with requests
MAX_TOKENS = 50_000_000  # self-reported per comment; anything above is a typo or a lie
RELAY_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}):(claude/ats-\d+)$")
REFUSE_KINDS = ("security", "policy", "unclear")


def handle_command(text, author, labels, assignees, target_repo, now, ctx=None):
    """Decide what a slash command does. Returns a plan dict, or None to ignore the comment.

    ctx carries what peer review needs: {"requester", "records", "request"}.
    """
    ctx = ctx or {}
    # GitHub emails the issue author on comments; the mention makes sure they're notified.
    who = f"@{ctx['requester']} " if ctx.get("requester") else ""
    text = text.strip()
    if not text.startswith("/"):
        return None
    verb = text.split()[0][1:].lower()
    is_assignee = author in assignees
    plan = {"add": [], "remove": [], "assign": [], "unassign": [], "reply": None, "close": False}

    last_claim = next((r for r in reversed(ctx.get("records", [])) if r.get("event") == "claim"), None)
    if verb == "claim":
        # The hourly sweep is best effort (GitHub skips scheduled runs for hours): a lock past its
        # time is free as the bot promised, whether or not the sweep has reopened it yet.
        stale = "claimed" in labels and last_claim is not None and not claim_is_live(last_claim, now)
        if author.lower() == (ctx.get("requester") or "").lower():
            plan["reply"] = f"@{author} you can’t take your own request."
        elif too_young(ctx, now):
            plan["reply"] = f"@{author} taking requests needs a GitHub account older than {MIN_ACCOUNT_AGE_DAYS} days."
        elif ctx.get("claims_elsewhere"):
            plan["reply"] = f"@{author} you already hold another request. Deliver, relay or release it first."
        elif "claimed" in labels and not stale:
            plan["reply"] = f"@{author} someone already took this request."
        elif not stale and not labels & {"open", "relay"}:
            plan["reply"] = f"@{author} this request is not open."
        else:
            until = datetime.fromtimestamp(now + CLAIM_HOURS * 3600, timezone.utc)
            plan["add"] = [] if stale else ["claimed"]
            plan["remove"] = ["open"]
            plan["assign"] = [author]
            previous = last_claim.get("donor") if stale else None
            if previous and previous.lower() != author.lower():
                plan["unassign"] = [previous]
            plan["reply"] = record_comment(
                f"{who}Taken by @{author}’s agent. Free again at {until:%Y-%m-%d %H:%M} UTC without news.",
                {"event": "claim", "donor": author, "at": int(now)},
            )
        return plan

    # A long session can outlive its 6-hour lock: its donor may still deliver if nobody took it since.
    if (verb == "delivered" and not is_assignee and "claimed" not in labels and last_claim
            and last_claim.get("donor", "").lower() == author.lower()):
        is_assignee = True
    if verb in ("release", "delivered", "relay", "question") and not is_assignee:
        plan["reply"] = f"@{author} only the donor who took this request can do that."
        return plan

    if verb == "release":
        plan["remove"] = ["claimed"]
        plan["unassign"] = [author]
        if "relay" not in labels:
            plan["add"] = ["open"]
        reason = safe(text[len("/release"):].strip().split("\n", 1)[0], 200) or "no reason given"
        plan["reply"] = record_comment(
            f"Released by @{author}: {reason}. Open for the next donor.",
            {"event": "release", "donor": author, "at": int(now)},
        )
        return plan

    if verb == "delivered":
        match = re.search(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", text)
        if not match or match.group(1).lower() != (target_repo or "").lower():
            plan["reply"] = f"@{author} expected `/delivered https://github.com/{target_repo}/pull/<n> tokens=<n>`."
            return plan
        head = ctx.get("pr_head")
        if head is not None and (head.get("owner", "").lower() != author.lower()
                                 or head.get("ref") != f"claude/ats-{ctx.get('issue')}"):
            plan["reply"] = (f"@{author} that pull request must come from your fork, branch "
                             f"`claude/ats-{ctx.get('issue')}`.")
            return plan
        plan["remove"] = ["claimed", "relay"]
        plan["add"] = ["delivered"]
        plan["reply"] = record_comment(
            f"{who}Pull request ready from @{author}’s agent: {match.group(0)}\n\n"
            "Have your own agent review it before you merge.",
            {"event": "delivered", "donor": author, "tokens": parse_tokens(text),
             "pr": match.group(0), "at": int(now)},
        )
        return plan

    if verb == "relay":
        branch = re.search(r"branch=(\S+)", text)
        valid = branch and RELAY_RE.match(branch.group(1))
        if not valid or valid.group(2).lower() != (target_repo or "/").split("/")[-1].lower():
            plan["reply"] = (f"@{author} expected `/relay branch=<you>/<{(target_repo or '/').split('/')[-1]}>:claude/ats-<n> "
                             "tokens=<n>` (your fork of the target) and a note.")
            return plan
        plan["remove"] = ["claimed"]
        plan["add"] = ["relay"]
        plan["unassign"] = [author]
        plan["reply"] = record_comment(
            f"{who}Partly done by @{author}’s agent, from `{branch.group(1)}`. The next donor continues from there.",
            {"event": "relay", "donor": author, "tokens": parse_tokens(text),
             "branch": branch.group(1), "at": int(now)},
        )
        return plan

    if verb == "approve":
        # The maintainer can list a request alone: needed while the beta has too few donors to review,
        # and to clear a request after checking a security flag.
        if author.lower() not in {m.lower() for m in ctx.get("maintainers", [])}:
            plan["reply"] = f"@{author} only a maintainer can approve directly. Use `/review`."
            return plan
        if not labels & {"review", "flagged"}:
            plan["reply"] = f"@{author} this request isn’t waiting for review."
            return plan
        scans = [r for r in ctx.get("records", []) if r.get("event") == "scan_ok"]
        if not scans:
            plan["reply"] = f"@{author} the automatic checks haven’t passed yet."
            return plan
        match = re.search(r"clarity=([1-5])\b", text)
        clarity = int(match.group(1)) if match else 3
        reason = safe(text.strip().partition("\n")[2].strip())
        plan["remove"], plan["add"] = ["review", "flagged"], ["open", f"clarity:{clarity}"]
        plan["reply"] = record_comment(
            frozen_comment(ctx.get("request", {}), scans[-1]["sha"], clarity,
                           [f"Approved by maintainer @{author}.", reason]),
            {"event": "approve", "by": author, "sha": scans[-1]["sha"], "clarity": clarity, "listed": True,
             **frozen_fields(ctx.get("request", {}), scans[-1]), "at": int(now)})
        return plan

    if verb == "unflag":
        # After checking a security or policy flag (often fixed by an edit): back to donors' review.
        if author.lower() not in {m.lower() for m in ctx.get("maintainers", [])}:
            plan["reply"] = f"@{author} only a maintainer can clear a flag."
            return plan
        if "flagged" not in labels:
            plan["reply"] = f"@{author} this request isn’t flagged."
            return plan
        reason = safe(text[len("/unflag"):].strip().split("\n", 1)[0], 200) or "checked"
        plan["remove"], plan["add"] = ["flagged"], ["review"]
        plan["reply"] = record_comment(
            f"{who}Flag cleared by maintainer @{author}: {reason}. Back to donors’ review.",
            {"event": "unflag", "by": author, "at": int(now)})
        return plan

    if verb == "review":
        return review_plan(text, author, labels, ctx, now, plan)

    if verb == "question":
        # The donor's agent hit a choice the request doesn't answer and the requester asked to be asked.
        question = safe(text[len("/question"):].split("\n", 1)[0].strip(), 300)
        if not question:
            plan["reply"] = f"@{author} expected `/question <the question>` on the first line."
            return plan
        plan["remove"] = ["claimed", "open"]
        plan["add"] = ["question"]
        plan["unassign"] = [author]
        branch = re.search(r"branch=(\S+)", text)
        branch = branch.group(1) if branch and RELAY_RE.match(branch.group(1)) else None
        if branch:
            plan["add"].append("relay")
        plan["reply"] = record_comment(
            f"{who}@{author}’s agent needs an answer before going further: **{question}**\n\n"
            "Requester: edit the request and add the answer under Decisions. It goes back through review, "
            "then back online, and the next donor continues from the pushed branch.",
            {"event": "question", "donor": author, "question": question, "branch": branch, "at": int(now)},
        )
        return plan

    if verb == "flag":
        if too_young(ctx, now):
            maintainers = " ".join(f"@{m}" for m in ctx.get("maintainers", []) if m)
            plan["reply"] = record_comment(
                f"{maintainers} @{author} flagged this request. It stays listed until a maintainer checks it.",
                {"event": "flag_noted", "by": author, "at": int(now)})
            return plan
        plan["remove"] = ["open", "claimed"]
        plan["add"] = ["review", "flagged"]
        plan["reply"] = record_comment(
            f"Flagged by @{author}. Hidden from donors until a maintainer checks it.",
            {"event": "flag", "by": author, "at": int(now)},
        )
        return plan

    return None


def parse_review(text):
    """`/review ok clarity=4` or `/review refuse security|policy|unclear`, reason on the next lines."""
    first, _, rest = text.strip().partition("\n")
    words = first.split()[1:]
    reason = safe(rest.strip(), 300)
    if words[:1] == ["ok"]:
        match = re.search(r"clarity=([1-5])\b", first)
        return {"verdict": "ok", "clarity": int(match.group(1)), "reason": reason} if match else None
    if words[:1] == ["refuse"] and len(words) > 1 and words[1] in REFUSE_KINDS:
        return {"verdict": "refuse", "kind": words[1], "reason": reason}
    return None


def frozen_fields(request, scan):
    """What donors' tools launch from: taken from the scan, never re-read from the live issue body."""
    return {"repo": scan.get("repo") or request.get("repo"), "base": scan.get("base") or request.get("base"),
            "model": safe(request.get("model"), 80), "body_hash": scan.get("body_hash")}


def frozen_comment(request, sha, clarity, reasons):
    frozen = {"sha": sha, "repo": request.get("repo"), "base": request.get("base")}
    notes = "\n".join(f"- {r}" for r in reasons if r)
    return (f"{FROZEN_MARK} {json.dumps(frozen)} -->\n"
            f"Reviewed and listed. Donors work from commit `{sha}` of "
            f"`{request.get('repo')}` and from this copy of the request.\n\nClarity: {clarity}/5.\n{notes}\n\n"
            "<details><summary>Frozen request</summary>\n\n"
            + "\n\n".join(f"**{k}**: {safe(v, 4000)}" for k, v in request.items() if v and k != "terms")
            + "\n\n</details>")


def review_plan(text, author, labels, ctx, now, plan):
    """Peer review: two donors' agents approve before a request is listed; one security flag stops it."""
    requester = ctx.get("requester", "")
    recs = ctx.get("records", [])
    if "review" not in labels:
        plan["reply"] = f"@{author} this request isn’t waiting for review."
        return plan
    if "flagged" in labels:
        plan["reply"] = f"@{author} this request was flagged. Only a maintainer can clear it."
        return plan
    if author.lower() == requester.lower():
        plan["reply"] = f"@{author} you can’t review your own request."
        return plan
    if too_young(ctx, now):
        plan["reply"] = (f"@{author} reviews count from GitHub accounts older than "
                         f"{MIN_ACCOUNT_AGE_DAYS} days. You can still give and ask.")
        return plan
    scan_at = [i for i, r in enumerate(recs) if r.get("event") == "scan_ok"]
    if not scan_at:
        plan["reply"] = f"@{author} the automatic checks haven’t passed yet."
        return plan
    scan = recs[scan_at[-1]]
    sha = scan["sha"]
    if ctx.get("body_hash") and scan.get("body_hash") and ctx["body_hash"] != scan["body_hash"]:
        plan["reply"] = f"@{author} the request changed since the last checks. Wait for them and review again."
        return plan
    # Each scan starts a new round: only reviews posted after the latest scan count.
    reviews = [r for r in recs[scan_at[-1] + 1:] if r.get("event") == "review" and r.get("sha") == sha]
    if any(r["by"].lower() == author.lower() for r in reviews):
        plan["reply"] = f"@{author} you already reviewed this version."
        return plan
    verdict = parse_review(text)
    if not verdict:
        plan["reply"] = f"@{author} expected `/review ok clarity=<1-5>` or `/review refuse <security|policy|unclear>`, then the reason."
        return plan
    record = {"event": "review", "by": author, "sha": sha, "at": int(now), **verdict}
    reviews.append(record)
    if verdict["verdict"] == "refuse" and verdict["kind"] in ("security", "policy"):
        plan["remove"], plan["add"] = ["review"], ["flagged"]
        plan["reply"] = record_comment(
            f"Flagged by @{author}’s agent ({verdict['kind']}): {verdict['reason'] or 'no reason given'}\n\n"
            "Hidden from donors until a maintainer checks it.", record)
        return plan
    unclear = [r for r in reviews if r["verdict"] == "refuse"]
    oks = [r for r in reviews if r["verdict"] == "ok"]
    if len(unclear) >= REVIEWS_NEEDED:
        plan["remove"], plan["add"] = ["review"], ["rejected"]
        reasons = "\n".join(f"- {r['reason']}" for r in unclear if r.get("reason"))
        plan["reply"] = record_comment(
            "Two reviewers found the request too open for an agent working alone:\n" + reasons
            + "\n\nAdd the missing steps or decisions and edit the request; it goes back to review.", record)
        return plan
    trusted = {t.lower() for t in ctx.get("trusted", [])}
    if len(oks) >= REVIEWS_NEEDED and not any(r["by"].lower() in trusted for r in oks):
        plan["reply"] = record_comment(
            f"Review by @{author}’s agent recorded. {len(oks)} approvals; one must come from a trusted reviewer "
            "(the maintainer, or a donor with a merged pull request) before it’s listed.", record)
        return plan
    if len(oks) >= REVIEWS_NEEDED:
        clarity = round(sum(r["clarity"] for r in oks) / len(oks))
        plan["remove"], plan["add"] = ["review"], ["open", f"clarity:{clarity}"]
        plan["reply"] = record_comment(frozen_comment(ctx.get("request", {}), sha, clarity,
                                                      [r.get("reason") for r in oks]),
                                       {**record, "listed": True, **frozen_fields(ctx.get("request", {}), scan)})
        return plan
    left = REVIEWS_NEEDED - len(oks)
    plan["reply"] = record_comment(f"Review by @{author}’s agent recorded. {left} more approval needed.", record)
    return plan


def risky_files(paths, package_json=None):
    notes = []
    for path in paths:
        for pattern, why in RISKY_PATHS:
            if pattern.search(path):
                notes.append(f"`{path}`: {why}")
                break
    if package_json:
        scripts = package_json.get("scripts") or {}
        for name in LIFECYCLE_SCRIPTS:
            if name in scripts:
                notes.append(f"package.json `{name}` script: `{scripts[name]}`")
    return notes


def sweep_plan(issue, recs, now):
    """What the hourly sweep should do with one issue. Returns a list of (action, detail)."""
    labels = {label["name"] for label in issue["labels"]}
    actions = []
    if "claimed" in labels:
        claims = [r for r in recs if r.get("event") == "claim"]
        if not claims:
            actions.append(("free_claim", None))  # label set but the claim comment never posted
        elif now - claims[-1]["at"] > CLAIM_HOURS * 3600:
            actions.append(("free_claim", claims[-1]["donor"]))
    if labels & {"open", "relay"} and "claimed" not in labels:
        # Count from the latest listing, so a request edited and listed again gets its 30 days.
        listed = [r["at"] for r in recs if r.get("listed") and r.get("at")]
        since = listed[-1] if listed else datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00")).timestamp()
        if now - since > EXPIRE_DAYS * 86400:
            actions.append(("expire", None))
    if "delivered" in labels:
        delivered = [r for r in recs if r.get("event") == "delivered"]
        if delivered:
            actions.append(("check_pr", delivered[-1]["pr"]))
    return actions


def build_stats(issues, now):
    people, totals = {}, {"donated_tokens": 0, "delivered": 0, "merged": 0, "requests": 0}

    def person(login):
        return people.setdefault(login, {"donated_tokens": 0, "received_tokens": 0,
                                         "delivered": 0, "merged": 0, "last_donation_at": None})

    for issue in issues:
        requester = issue["author"]["login"]
        totals["requests"] += 1
        for rec in records(issue.get("comments", [])):
            event = rec.get("event")
            if event in ("delivered", "relay"):
                tokens = int(rec.get("tokens") or 0)
                donor = person(rec["donor"])
                donor["donated_tokens"] += tokens
                at = datetime.fromtimestamp(rec["at"], timezone.utc).isoformat()
                donor["last_donation_at"] = max(filter(None, [donor["last_donation_at"], at]))
                person(requester)["received_tokens"] += tokens
                totals["donated_tokens"] += tokens
                if event == "delivered":
                    donor["delivered"] += 1
                    totals["delivered"] += 1
            elif event == "merged":
                person(rec["donor"])["merged"] += 1
                totals["merged"] += 1
    return {"generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "totals": totals, "people": people}


# ---------------------------------------------------------------- GitHub side

def gh(*args, input_text=None, check=True):
    result = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def gh_json(*args, check=True):
    out = gh(*args, check=check)
    return json.loads(out) if out.strip() else None


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def load_issue(number):
    return gh_json("issue", "view", str(number), "-R", repo(),
                   "--json", "number,title,body,labels,assignees,author,createdAt,comments")


def comment_as_bot(number, body):
    if os.environ.get("ANTIPODE_BOT_TOKEN"):
        bot_gh("issue", "comment", str(number), "-R", repo(), "--body-file", "-", input_text=body)
    else:  # forks of this repository and local runs: the workflow token still works
        gh("issue", "comment", str(number), "-R", repo(), "--body-file", "-", input_text=body)


def apply_plan(number, plan):
    if plan["reply"]:
        comment_as_bot(number, plan["reply"])
    if plan["add"] or plan["remove"]:
        args = ["issue", "edit", str(number), "-R", repo()]
        for name in plan["add"]:
            args += ["--add-label", name]
        for name in plan["remove"]:
            args += ["--remove-label", name]
        gh(*args, check=False)  # removing an absent label is not an error worth failing on
    for login in plan["assign"]:
        gh("issue", "edit", str(number), "-R", repo(), "--add-assignee", login, check=False)
    for login in plan["unassign"]:
        gh("issue", "edit", str(number), "-R", repo(), "--remove-assignee", login, check=False)
    if plan["close"]:
        gh("issue", "close", str(number), "-R", repo())


def empty_plan(**changes):
    plan = {"add": [], "remove": [], "assign": [], "unassign": [], "reply": None, "close": False}
    plan.update(changes)
    return plan


def account_created_at(login):
    """Creation time of a GitHub account, as epoch seconds. Unknown counts as brand new (fail closed)."""
    try:
        info = gh_json("api", f"users/{login}", check=False) or {}
        return datetime.fromisoformat(info["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return time.time()


def comment_id(comment):
    return (comment.get("url") or "").rpartition("#issuecomment-")[2] or comment.get("id", "")


def trusted_reviewers():
    """Maintainers, plus donors with at least one merged pull request (from the bot-built stats.json)."""
    trusted = {os.environ.get("GITHUB_REPOSITORY_OWNER", "")}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stats.json")) as f:
            people = json.load(f).get("people", {})
        trusted |= {login for login, p in people.items() if p.get("merged", 0) > 0}
    except (OSError, ValueError):
        pass
    return sorted(t for t in trusted if t)


def held_elsewhere(held, number, now):
    """Other requests a donor still holds: a lock past its time no longer counts, sweep or not."""
    live = []
    for other in held:
        if other["number"] == number:
            continue
        claims = [r for r in records(other.get("comments", [])) if r.get("event") == "claim"]
        if not claims or claim_is_live(claims[-1], now):
            live.append(other["number"])
    return live


def command_context(issue, author, body):
    request = parse(issue["body"])
    ctx = {"requester": issue["author"]["login"], "records": records(issue["comments"]),
           "request": request, "issue": issue["number"], "body_hash": body_hash(issue["body"]),
           "maintainers": [os.environ.get("GITHUB_REPOSITORY_OWNER", "")], "trusted": trusted_reviewers()}
    verb = body.strip().split()[0][1:].lower() if body.strip().startswith("/") else ""
    if verb in ("review", "claim", "flag"):
        ctx["author_created_at"] = account_created_at(author)
    if verb == "claim":
        held = gh_json("issue", "list", "-R", repo(), "--assignee", author, "--label", "claimed",
                       "--state", "open", "--json", "number,comments", check=False) or []
        ctx["claims_elsewhere"] = held_elsewhere(held, issue["number"], time.time())
    if verb == "delivered":
        match = re.search(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", body)
        pr = gh_json("api", f"repos/{match.group(1)}/pulls/{match.group(2)}", check=False) if match else None
        head = (pr or {}).get("head") or {}
        ctx["pr_head"] = {"owner": ((head.get("repo") or {}).get("owner") or {}).get("login", ""),
                          "ref": head.get("ref", "")}
    return ctx


def cmd_command(args):
    """Handle every slash comment the bot hasn't answered yet, oldest first.

    Runs can be replaced while queued (GitHub keeps one pending run per group), so each run catches
    up on everything instead of trusting that its own comment is the only one.
    """
    done = set()
    for _ in range(30):
        issue = load_issue(args.issue)
        answered = {r.get("cid") for r in records(issue["comments"]) if r.get("cid")}
        pending = [c for c in issue["comments"]
                   if not is_bot(c) and (c.get("body") or "").lstrip().startswith("/")
                   and comment_id(c) not in answered and comment_id(c) not in done]
        if not pending:
            return
        comment = pending[0]
        cid, author, body = comment_id(comment), comment["author"]["login"], comment["body"]
        done.add(cid)
        labels = {label["name"] for label in issue["labels"]}
        assignees = {a["login"] for a in issue["assignees"]}
        ctx = command_context(issue, author, body)
        plan = handle_command(body, author, labels, assignees, ctx["request"].get("repo") or "", time.time(), ctx)
        if not plan:
            continue
        plan["reply"] = acknowledge(plan["reply"] or f"@{author} done.", cid)
        apply_plan(args.issue, plan)


def acknowledge(reply, cid):
    """Tag the bot's answer with the comment it answers, inside its record."""
    match = RECORD_RE.search(reply)
    if match:
        rec = json.loads(match.group(1))
        rec["cid"] = cid
        return reply[:match.start()] + f"<!-- ats:record {json.dumps(rec, separators=(',', ':'))} -->"
    return record_comment(reply, {"event": "reply", "cid": cid})


def cmd_scan(args):
    issue = load_issue(args.issue)
    request = parse(issue["body"])
    found = problems(request)
    report = {"issue": args.issue, "request": request, "problems": found, "risky": [], "sha": None}
    author = issue["author"]["login"]
    info = None
    maintainer = author.lower() == os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
    if not maintainer and time.time() - account_created_at(author) < MIN_REQUESTER_AGE_DAYS * 86400:
        found.append(f"posting a request needs a GitHub account older than {MIN_REQUESTER_AGE_DAYS} days")
    if not found:
        info = gh_json("api", f"repos/{request['repo']}", check=False)
        # A private or missing repository answers {"message": "Not Found"}, not an error exit
        if not info or "owner" not in info:
            found.append("repository not found (it must exist and be public)")
        elif info.get("private"):
            found.append("repository is private")
        elif info["owner"]["login"].lower() != author.lower() and not (
                is_org_member(info["owner"], author) and "antipode" in (info.get("topics") or [])):
            found.append("only the repository owner can post (for an organization repository: a public "
                         "member, with the `antipode` topic added to the repository)")
        else:
            from urllib.parse import quote
            branch = gh_json("api", f"repos/{request['repo']}/branches/{quote(request['base'], safe='')}", check=False)
            if not branch or "commit" not in branch:
                found.append(f"branch {request['base']} not found")
            else:
                report["sha"] = branch["commit"]["sha"]
                tree = gh_json("api", f"repos/{request['repo']}/git/trees/{report['sha']}?recursive=1", check=False) or {}
                paths = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob"]
                if tree.get("truncated") or "tree" not in tree:
                    report["risky"].append("repository too large to list every file: read it with extra care")
                report["risky"] += risky_files(paths, fetch_package_json(request["repo"], report["sha"], paths))
    mine = gh_json("issue", "list", "-R", repo(), "--author", author, "--state", "open",
                   "--json", "number,labels", "--limit", "100")
    active = [i for i in mine if {l["name"] for l in i["labels"]} & ACTIVE]
    if len(active) > MAX_OPEN_PER_AUTHOR:
        found.append(f"more than {MAX_OPEN_PER_AUTHOR} active requests; wait for one to finish")
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    if found:
        text = "This request can't be listed yet:\n" + "\n".join(f"- {p}" for p in found)
        text += ("\n\nEdit the issue to fix it; the checks run again. Keep the headings of the form exactly"
                 f" ({FORMAT_URL}). If an agent edits it, have it run `scripts/parse_request.py` on the new body"
                 " first: it lists these same problems (`/antipode:ask` does this). If the task changed, change"
                 " the title too.")
        apply_plan(args.issue, empty_plan(add=["rejected"], remove=["review"],
                                          reply=record_comment(text, {"event": "scan_failed", "at": int(time.time())})))
    else:
        risky = "\n".join(f"- {r}" for r in report["risky"]) or "- none"
        # Tag the request with the target's language, so donors' shortlists can favour their own stack.
        lang = language_label((info or {}).get("language"))
        old = [l["name"] for l in issue["labels"] if l["name"].startswith("lang:") and l["name"] != lang]
        if lang:
            gh("label", "create", lang, "-R", repo(), "--color", "ededed", "--force", check=False)
        apply_plan(args.issue, empty_plan(remove=old, reply=record_comment(
            f"Automatic checks passed on commit `{report['sha']}`. "
            + ("Still flagged: waiting for a maintainer.\n\n" if "flagged" in {l["name"] for l in issue["labels"]}
               else f"Waiting for {REVIEWS_NEEDED} donors’ agents to review it.\n\n")
            + f"Files that run on install, build or by agents, to read first:\n{risky}",
            {"event": "scan_ok", "sha": report["sha"], "repo": request["repo"], "base": request["base"],
             "body_hash": body_hash(issue["body"]), "risky": [safe(r, 200) for r in report["risky"][:50]],
             "at": int(time.time())}), add=["review"] + ([lang] if lang else [])))
    print(json.dumps({"ok": not found}))


def language_label(language):
    """GitHub's primary language as a label donors' shortlists can match: "C++" → "lang:c++"."""
    slug = re.sub(r"[^a-z0-9+#.-]+", "-", (language or "").lower()).strip("-")
    return f"lang:{slug}"[:50] if slug else None


def is_org_member(owner, login):
    if owner.get("type") != "Organization":
        return False
    result = subprocess.run(["gh", "api", f"orgs/{owner['login']}/public_members/{login}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def fetch_package_json(target, sha, paths):
    if "package.json" not in paths:
        return None
    raw = gh("api", f"repos/{target}/contents/package.json?ref={sha}",
             "-H", "Accept: application/vnd.github.raw", check=False)
    try:
        return json.loads(raw)
    except ValueError:
        return None


STATE_LABELS = ACTIVE | {"question", "rejected", "flagged", "delivered", "expired"}


def edited_while_flagged():
    """An edit can't clear a flag by itself, or a flagged request could be edited back online."""
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    return ("Edited: back to the automatic checks. The request is still flagged, so donors can’t review it yet: "
            f"@{owner} checks the new version, then `/unflag` sends it back to donors’ review."
            "\n\nIf the task itself changed, change the title too: donors pick requests by their title.")


def cmd_intake(args):
    """Every new, edited or reopened issue. Requests are known by their title or by a state label, so
    renaming the title can't dodge the checks."""
    issue = load_issue(args.issue)
    labels = {label["name"] for label in issue["labels"]}
    if not issue["title"].startswith("[request]") and not labels & STATE_LABELS:
        return
    if args.action != "opened":
        if "delivered" in labels or "merged" in labels:
            return  # done; edits no longer matter
        recs = records(issue["comments"])
        scans = [r for r in recs if r.get("event") == "scan_ok"]
        if scans and scans[-1].get("body_hash") == body_hash(issue["body"]) and args.action == "edited":
            return  # title-only edit: nothing to review again
        remove = ["open", "rejected", "claimed", "question", "expired"] + [f"clarity:{n}" for n in range(1, 6)]
        text = ("Edited: back to the automatic checks and review. Donors work only from a reviewed version."
                "\n\nIf the task itself changed, change the title too: donors pick requests by their title.")
        if "flagged" in labels:
            text = edited_while_flagged()
        apply_plan(args.issue, empty_plan(
            add=["review"], remove=remove, unassign=[a["login"] for a in issue["assignees"]],
            reply=record_comment(text, {"event": "edited", "at": int(time.time())})))
    cmd_scan(args)


def cmd_sweep(args):
    now = time.time()
    issues = gh_json("issue", "list", "-R", repo(), "--state", "open", "--limit", "500",
                     "--json", "number,labels,createdAt,comments")
    for issue in issues:
        try:
            sweep_issue(issue, now)
        except Exception as error:  # one broken request must not stop the others
            print(f"sweep: request #{issue['number']} skipped: {error}", file=sys.stderr)


def sweep_issue(issue, now):
    recs = records(issue["comments"])
    for action, detail in sweep_plan(issue, recs, now):
        if action == "free_claim":
            relay = "relay" in {l["name"] for l in issue["labels"]}
            apply_plan(issue["number"], empty_plan(
                remove=["claimed"], add=[] if relay else ["open"], unassign=[detail] if detail else [],
                reply=record_comment(f"No news from @{detail or 'the donor'} for {CLAIM_HOURS} hours: open again.",
                                     {"event": "release", "donor": detail, "at": int(now)})))
        elif action == "expire":
            apply_plan(issue["number"], empty_plan(
                remove=["open", "relay"], add=["expired"], close=True,
                reply=f"No donor in {EXPIRE_DAYS} days. Reopen the issue to list it again."))
        elif action == "check_pr":
            follow_pr(issue, recs, detail, now)


def follow_pr(issue, recs, url, now):
    owner_repo, number = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url).groups()
    pr = gh_json("api", f"repos/{owner_repo}/pulls/{number}", check=False)
    # A deleted or unreachable pull request answers {"message": "Not Found"}: nothing to follow.
    if not pr or pr.get("state") != "closed":
        return
    donor = [r for r in recs if r.get("event") == "delivered"][-1]["donor"]
    if pr.get("merged_at"):
        apply_plan(issue["number"], empty_plan(
            remove=["delivered"], add=["merged"], close=True,
            reply=record_comment("Merged by the requester. Thank you!",
                                 {"event": "merged", "donor": donor, "at": int(now)})))
    else:
        apply_plan(issue["number"], empty_plan(
            remove=["delivered"], add=["rejected"], close=True,
            reply=record_comment("The requester closed the pull request without merging.",
                                 {"event": "pr_closed", "donor": donor, "at": int(now)})))


# Delivery without GitHub tools: the donor's cloud session can only push its branch. Its newest
# commit on claude/ats-<n> ends with Antipode-* trailers, and the harvest (every 10 minutes) turns
# them into what the donor used to post: the pull request (opened by the Antipode bot account) and
# the /delivered, /relay, /question, /release or /flag record.
TRAILER_RE = re.compile(r"^Antipode-([A-Za-z]+):[ \t]*(.*?)[ \t]*$")
STATUSES = ("delivered", "relay", "question", "release", "flag")
OUTCOMES = ("delivered", "relay", "question")
HARVEST_HOURS = 2 * CLAIM_HOURS  # a late delivery is still accepted while nobody took the request since
NO_PUSH_MINUTES = 45  # the session pushes its branch first: none by then means the push was refused


def parse_status(message):
    """Read the trailers of a commit message. None when it carries no valid Antipode-Status."""
    lines = (message or "").replace("\r\n", "\n").strip().split("\n")
    fields, body = {}, []
    for line in lines[1:]:
        match = TRAILER_RE.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2)
        else:
            body.append(line)
    status = fields.get("status", "").lower()
    if status not in STATUSES:
        return None
    tokens = fields.get("tokens", "")
    return {"status": status,
            "tokens": min(int(tokens), MAX_TOKENS) if tokens.isdigit() else 0,
            "title": safe(fields.get("title") or lines[0], 120).strip(),
            "note": safe(fields.get("question") or fields.get("reason") or "", 300).strip(),
            "body": "\n".join(body).strip()}


def harvest_command(status, issue, donor, target, pr_url=None):
    """The slash command the donor would have posted, for handle_command."""
    branch = f"{donor}/{target.split('/')[-1]}:claude/ats-{issue}"
    kind, tokens, note = status["status"], status["tokens"], status["note"]
    if kind == "delivered":
        return f"/delivered {pr_url} tokens={tokens}"
    if kind == "relay":
        return f"/relay branch={branch} tokens={tokens}"
    if kind == "question":
        return f"/question {note or 'see the note on the branch'} branch={branch}"
    return f"/{kind} {note or 'no reason given'}"


def harvest_candidate(issue, recs, now):
    """(donor, frozen record, claim time) when a donor may still report on this request, else None."""
    claims = [i for i, r in enumerate(recs) if r.get("event") == "claim"]
    if not claims:
        return None
    last = recs[claims[-1]]
    if now - (last.get("at") or 0) > HARVEST_HOURS * 3600:
        return None
    labels = {label["name"] for label in issue["labels"]}
    if not labels & {"claimed", "open", "relay"}:
        return None
    after = recs[claims[-1] + 1:]
    if any(r.get("event") in OUTCOMES or r.get("via") == "harvest" for r in after):
        return None
    listed = [r for r in recs if r.get("listed")]
    if not listed:
        return None
    return last.get("donor"), listed[-1], last.get("at") or 0


def pr_body(status, issue_url, donor, issue):
    note = status["body"][:6000] or "No description."
    return (f"{note}\n\n---\n"
            f"Antipode request: {issue_url}\n"
            f"Tokens given by @{donor}. Opened by the Antipode bot from `{donor}:claude/ats-{issue}`.")


def with_note(reply, note):
    """Insert the session's note (relay hand-off, question details) before the record."""
    if not note:
        return reply
    match = RECORD_RE.search(reply)
    cut = match.start() if match else len(reply)
    return reply[:cut].rstrip() + "\n\n" + safe(note, 4000) + "\n\n" + reply[cut:]


def tag_record(reply, **fields):
    match = RECORD_RE.search(reply)
    if not match:
        return record_comment(reply, {"event": "reply", **fields})
    rec = json.loads(match.group(1))
    rec.update(fields)
    return reply[:match.start()] + f"<!-- ats:record {json.dumps(rec, separators=(',', ':'))} -->"


def bot_gh(*args, input_text=None):
    """gh as the Antipode bot account. It has no write access to any repository: it comments and
    opens pull requests; labels and assignees stay with the workflow's own token."""
    token = os.environ.get("ANTIPODE_BOT_TOKEN")
    if not token:
        raise RuntimeError("ANTIPODE_BOT_TOKEN is not set")
    result = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True,
                            env={**os.environ, "GH_TOKEN": token})
    if result.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def open_pull_request(target, base, donor, issue, title, body):
    head = f"{donor}:claude/ats-{issue}"
    existing = gh_json("api", f"repos/{target}/pulls?head={head}&state=open", check=False) or []
    if existing:
        return existing[0]["html_url"]
    pr = bot_gh("api", "-X", "POST", f"repos/{target}/pulls", "-f", f"title={title}", "-f", f"head={head}",
                "-f", f"base={base}", "-f", f"body={body}")
    return pr["html_url"]


def harvest_issue(issue, now):
    recs = records(issue["comments"])
    found = harvest_candidate(issue, recs, now)
    if not found:
        return
    donor, frozen, claimed_at = found
    target, base, number = frozen.get("repo") or "", frozen.get("base") or "", issue["number"]
    fork = f"{donor}/{target.split('/')[-1]}"
    info = gh_json("api", f"repos/{fork}", check=False) or {}
    if ((info.get("parent") or {}).get("full_name") or "").lower() != target.lower():
        return
    head = gh_json("api", f"repos/{fork}/commits/claude/ats-{number}", check=False) or {}
    labels = {label["name"] for label in issue["labels"]}
    if not head.get("sha"):
        if "claimed" in labels and now - claimed_at > NO_PUSH_MINUTES * 60:
            # #8: the donor's Claude GitHub app didn't cover the fork, and the request stayed locked.
            status = {"status": "release", "tokens": 0, "title": "", "body": "",
                      "note": f"no branch pushed to {fork} after {NO_PUSH_MINUTES} minutes "
                              "(the donor's Claude GitHub app may not cover the fork)"}
        else:
            return
    else:
        status = parse_status((head.get("commit") or {}).get("message"))
    if not status:
        return
    if status["status"] != "delivered" and "claimed" not in labels:
        return  # the lock ran out: only a delivery is still accepted, as /delivered allows
    full = load_issue(number)
    pr_url = None
    if status["status"] == "delivered":
        issue_url = f"https://github.com/{repo()}/issues/{number}"
        pr_url = open_pull_request(target, base, donor, number, status["title"],
                                   pr_body(status, issue_url, donor, number))
    text = harvest_command(status, number, donor, target, pr_url)
    ctx = command_context(full, donor, text)
    plan = handle_command(text, donor, {l["name"] for l in full["labels"]},
                          {a["login"] for a in full["assignees"]}, target, now, ctx)
    if not plan or not plan["reply"]:
        return
    if status["status"] in ("relay", "question", "flag"):
        plan["reply"] = with_note(plan["reply"], status["body"])
    plan["reply"] = tag_record(plan["reply"], via="harvest", head=head.get("sha", ""))
    apply_plan(number, plan)


def cmd_harvest(args):
    now = time.time()
    issues = gh_json("issue", "list", "-R", repo(), "--state", "open", "--limit", "500",
                     "--json", "number,labels,comments")
    for issue in issues:
        try:
            harvest_issue(issue, now)
        except Exception as error:  # one broken request must not stop the others
            print(f"harvest: request #{issue['number']} skipped: {error}", file=sys.stderr)


def cmd_stats(args):
    issues = gh_json("issue", "list", "-R", repo(), "--state", "all", "--limit", "1000",
                     "--json", "number,author,comments")
    stats = build_stats(issues, time.time())
    with open(args.out, "w") as f:
        json.dump(stats, f, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("command"); p.add_argument("--issue", type=int, required=True)
    p = sub.add_parser("scan"); p.add_argument("--issue", type=int, required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("intake"); p.add_argument("--issue", type=int, required=True)
    p.add_argument("--action", required=True); p.add_argument("--out", default="report.json")
    sub.add_parser("sweep")
    sub.add_parser("harvest")
    p = sub.add_parser("stats"); p.add_argument("--out", default="stats.json")
    args = parser.parse_args()
    {"command": cmd_command, "scan": cmd_scan, "intake": cmd_intake,
     "sweep": cmd_sweep, "harvest": cmd_harvest, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
