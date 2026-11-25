#!/usr/bin/env python3
"""Check a claimed request and prepare the donor's cloud session, from bot-written data only.

Usage: preflight.py --issue <n> --donor <login> --ceiling-usd <dollars> [--out <dir>]

Everything a stranger could change (the issue body, comments, the target repository) is read
here, validated, and never pasted into a shell by the agent. Prints one JSON object:
  {"ok": true, "target", "base", "sha", "fork", "model", "effort", "relay_branch",
   "frozen_comment", "relay_comment", "prompt_file", "fallback_command"}
or {"ok": false, "reason": "..."} (exit 1). On failure the skill releases the request.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

CENTRAL = "eliorpom-cmd/antipode"
BOT = ("github-actions[bot]", "github-actions")
RECORD_RE = re.compile(r"<!-- ats:record (\{[^\n]*\}) -->\s*\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.+$)[A-Za-z0-9._-]{1,100}$")
BASE_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")
RELAY_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}):(claude/ats-\d+)$")
# Files that change what an agent or a session does before it reads AGENTS.md.
AGENT_CONFIG = re.compile(
    r"(^|/)(\.claude/|\.mcp\.json$|\.codex/|\.cursor/|\.gemini/|\.devcontainer/|\.envrc$|"
    r"CLAUDE(\.local)?\.md$|AGENTS\.md$|GEMINI\.md$|\.husky/|\.githooks/|\.vscode/)")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def gh_json(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def bot_records(comments):
    """(record, comment url) for each bot comment that ends with a record, oldest first."""
    out = []
    for c in comments:
        if (c.get("author") or {}).get("login") not in BOT:
            continue
        m = RECORD_RE.search(c.get("body") or "")
        if m:
            try:
                rec = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append((rec, c.get("url", "")))
    return out


def model_alias(text):
    """"latest Opus, effort medium" → ("opus", "medium"). Unknown model → (None, None)."""
    low = (text or "").lower()
    alias = next((a for a in ("opus", "sonnet", "haiku") if a in low), None)
    effort = next((e for e in EFFORTS if re.search(rf"\beffort\s*[:=]?\s*{e}\b", low)), None)
    if alias and not effort:
        effort = {"opus": "medium", "sonnet": "high"}.get(alias)
    return alias, effort


def changed_config(repo, base, head):
    """Agent-config files that differ between two commits of `repo` (None if GitHub can't compare)."""
    diff = gh_json("api", f"repos/{repo}/compare/{base}...{head}")
    if not diff or "files" not in diff:
        return None
    return [f["filename"] for f in diff["files"] if AGENT_CONFIG.search(f.get("filename", ""))]


def fail(reason):
    print(json.dumps({"ok": False, "reason": reason}))
    sys.exit(1)


def fill_prompt(values):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "references", "donor-prompt.md")) as f:
        text = f.read()
    template = text.split("```", 2)[1].strip("\n")
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--donor", required=True)
    parser.add_argument("--ceiling-usd", type=float, required=True, help="cost ceiling for the session")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    issue = gh_json("issue", "view", str(args.issue), "-R", CENTRAL,
                    "--json", "number,labels,assignees,author,comments")
    if not issue:
        fail("request not found")
    labels = {l["name"] for l in issue["labels"]}
    if "flagged" in labels or not labels & {"claimed"}:
        fail("request is not claimed, or was flagged")
    if args.donor.lower() not in {a["login"].lower() for a in issue["assignees"]}:
        fail("request is assigned to someone else")
    recs = bot_records(issue["comments"])
    listings = [(r, url) for r, url in recs if r.get("listed")]
    if not listings:
        fail("no reviewed version of this request")
    frozen, frozen_url = listings[-1]
    target, base, sha = frozen.get("repo") or "", frozen.get("base") or "", frozen.get("sha") or ""
    if not (REPO_RE.match(target) and BASE_RE.match(base) and SHA_RE.match(sha)):
        fail("the reviewed version has an invalid repository, branch or commit")
    alias, effort = model_alias(frozen.get("model"))
    if not alias:
        fail(f"unknown model in the request: {frozen.get('model')!r}")

    # Relay: the branch comes from the bot's record and must live on a fork of the target.
    relay_branch, relay_url = None, None
    if "relay" in labels:
        relays = [(r, url) for r, url in recs if r.get("event") in ("relay", "question") and r.get("branch")]
        if relays:
            rec, relay_url = relays[-1]
            m = RELAY_RE.match(rec["branch"])
            if not m:
                fail("invalid relay branch")
            info = gh_json("api", f"repos/{m.group(1)}/{m.group(2)}") or {}
            if ((info.get("parent") or {}).get("full_name") or "").lower() != target.lower():
                fail("the relay branch isn't on a fork of the target repository")
            touched = changed_config(target, sha, f"{m.group(1)}:{m.group(3)}")
            if touched is None:
                fail("can't compare the relay branch with the reviewed commit")
            if touched:
                fail("the relay branch changes agent configuration: " + ", ".join(touched[:5]))
            relay_branch = rec["branch"]

    # The donor's fork is what the cloud session opens: its default branch must not carry agent
    # configuration that the reviewers never saw.
    name = target.split("/")[1]
    fork = f"{args.donor}/{name}"
    info = gh_json("api", f"repos/{fork}")
    if not info:
        fail(f"fork {fork} not found: run `gh repo fork {target} --clone=false` first")
    if ((info.get("parent") or {}).get("full_name") or "").lower() != target.lower():
        fail(f"{fork} exists but isn't a fork of {target}")
    touched = changed_config(fork, sha, info.get("default_branch") or "main")
    if touched is None:
        fail(f"can't compare your fork with the reviewed commit: run `gh repo sync {fork}` and retry")
    if touched:
        fail("the project changed agent configuration since the review: " + ", ".join(touched[:5])
             + ". Release it with `/release config changed`; editing the request sends it back to review.")

    out = args.out or tempfile.mkdtemp(prefix="antipode-launch-")
    os.makedirs(out, exist_ok=True)
    prompt = fill_prompt({
        "issue": args.issue, "central_repo": CENTRAL, "fork": fork, "target_repo": target, "base": base,
        "sha": sha, "model": alias, "frozen_comment": frozen_url, "ceiling_usd": f"${args.ceiling_usd:.2f}",
        "relay": f"the relay branch {relay_branch} (bot comment {relay_url})" if relay_branch else "the frozen sha",
    })
    prompt_file = os.path.join(out, "prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)
    effort_flag = f" --effort {effort}" if effort else ""
    fallback = (f"cd {shlex.quote(out)} && claude --cloud --model {alias}{effort_flag} "
                f"\"$(cat {shlex.quote(prompt_file)})\"")
    print(json.dumps({"ok": True, "issue": args.issue, "target": target, "base": base, "sha": sha,
                      "fork": fork, "model": alias, "effort": effort, "relay_branch": relay_branch,
                      "frozen_comment": frozen_url, "relay_comment": relay_url,
                      "prompt_file": prompt_file, "fallback_command": fallback}, indent=2))


if __name__ == "__main__":
    main()
