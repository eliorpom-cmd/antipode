#!/usr/bin/env python3
"""Warn a requester whose CI would run a stranger's pull request with the repository's secrets.

Donors' pull requests come from forks. A workflow triggered by `pull_request` runs them without
secrets. A workflow triggered by `pull_request_target` runs with the base repository's secrets and
always runs, whatever the fork approval settings say. If it also checks out the pull request's code
and runs it, anyone who opens a pull request can read those secrets.

Usage: check_ci.py [repo_dir]   (default: current directory)
Prints JSON: {"risky": [{"file", "lines"}], "target_only": [file, ...]}. Exit code 0 always.
"""
import json
import os
import re
import sys

TRIGGER = re.compile(r"\bpull_request_target\b")
# Ways a workflow fetches the pull request's own code instead of the base branch.
PR_CODE = re.compile(
    r"github\.event\.pull_request\.head\.(sha|ref|repo)"
    r"|github\.head_ref"
    r"|refs/pull/"
    r"|github\.event\.pull_request\.merge_commit_sha"
    r"|gh\s+pr\s+checkout"
    r"|git\s+fetch[^\n]*pull/"
)


def strip_comment(line):
    # Drop YAML comments so a commented-out trigger or checkout is not reported.
    return re.sub(r"(^|\s)#.*$", "", line)


def scan(repo_dir):
    folder = os.path.join(repo_dir, ".github", "workflows")
    risky, target_only = [], []
    if not os.path.isdir(folder):
        return {"risky": risky, "target_only": target_only}
    for name in sorted(os.listdir(folder)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [strip_comment(line) for line in f]
        except OSError:
            continue
        if not any(TRIGGER.search(line) for line in lines):
            continue
        hits = [i + 1 for i, line in enumerate(lines) if PR_CODE.search(line)]
        rel = os.path.join(".github", "workflows", name)
        if hits:
            risky.append({"file": rel, "lines": hits})
        else:
            target_only.append(rel)
    return {"risky": risky, "target_only": target_only}


if __name__ == "__main__":
    print(json.dumps(scan(sys.argv[1] if len(sys.argv) > 1 else "."), indent=2))
