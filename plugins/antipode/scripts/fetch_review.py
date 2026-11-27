#!/usr/bin/env python3
"""Find requests waiting for review and download their code safely, without running anything.

  fetch_review.py queue --user <login>   up to 2 requests this user can review now (JSON list)
  fetch_review.py fetch --issue <n>      download the reviewed commit, print the folder (JSON)
  fetch_review.py clean --issue <n>      remove that download

Repository and commit come only from the bot's latest scan record, never from the issue body.
Archives are extracted without links, devices, absolute paths or "..", with size limits.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile

CENTRAL = "eliorpom-cmd/antipode"
BOT = ("antipode-bot", "github-actions[bot]", "github-actions")
RECORD_RE = re.compile(r"<!-- ats:record (\{[^\n]*\}) -->\s*\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.+$)[A-Za-z0-9._-]{1,100}$")
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_FILES = 50_000
ROOT = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "antipode", "reviews")


def gh(*args, binary=False):
    result = subprocess.run(["gh", *args], capture_output=True, text=not binary)
    return result.stdout if result.returncode == 0 else None


def gh_json(*args):
    out = gh(*args)
    try:
        return json.loads(out) if out else None
    except ValueError:
        return None


def records(comments):
    out = []
    for c in comments or []:
        if (c.get("author") or {}).get("login") not in BOT:
            continue
        m = RECORD_RE.search(c.get("body") or "")
        if m:
            try:
                rec = json.loads(m.group(1))
                if isinstance(rec, dict):
                    out.append(rec)
            except ValueError:
                pass
    return out


def body_hash(body):
    return hashlib.sha256((body or "").replace("\r\n", "\n").strip().encode()).hexdigest()[:16]


def queue(user):
    issues = gh_json("issue", "list", "-R", CENTRAL, "--label", "review", "--state", "open", "--limit", "50",
                     "--json", "number,title,body,author,labels,comments,createdAt") or []
    picked = []
    for issue in sorted(issues, key=lambda i: i["createdAt"]):
        labels = {l["name"] for l in issue["labels"]}
        if "flagged" in labels or issue["author"]["login"].lower() == user.lower():
            continue
        recs = records(issue["comments"])
        scans = [i for i, r in enumerate(recs) if r.get("event") == "scan_ok"]
        if not scans:
            continue
        scan = recs[scans[-1]]
        if scan.get("body_hash") and scan["body_hash"] != body_hash(issue["body"]):
            continue  # edited since the checks: wait for the new scan
        mine = [r for r in recs[scans[-1] + 1:] if r.get("event") == "review" and r.get("by", "").lower() == user.lower()]
        if mine:
            continue
        if not (REPO_RE.match(scan.get("repo") or "") and SHA_RE.match(scan.get("sha") or "")):
            continue
        picked.append({"number": issue["number"], "title": issue["title"], "repo": scan["repo"],
                       "sha": scan["sha"], "risky": scan.get("risky", []), "request": issue["body"]})
        if len(picked) == 2:
            break
    return picked


def folder(number):
    return os.path.join(ROOT, str(int(number)))


def fetch(number):
    issue = gh_json("issue", "view", str(number), "-R", CENTRAL, "--json", "comments")
    scans = [r for r in records((issue or {}).get("comments")) if r.get("event") == "scan_ok"]
    if not scans:
        return {"ok": False, "reason": "no automatic checks recorded"}
    repo, sha = scans[-1].get("repo") or "", scans[-1].get("sha") or ""
    if not (REPO_RE.match(repo) and SHA_RE.match(sha)):
        return {"ok": False, "reason": "invalid repository or commit in the scan record"}
    data = gh("api", f"repos/{repo}/tarball/{sha}", binary=True)
    if not data:
        return {"ok": False, "reason": "download failed"}
    if len(data) > MAX_ARCHIVE:
        return {"ok": False, "reason": "archive larger than 200 MB"}
    dest = folder(number)
    clean(number)
    os.makedirs(dest)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            name = member.name
            if name.startswith("/") or ".." in name.split("/") or not (member.isfile() or member.isdir()):
                continue  # links, devices and escapes are dropped, never written
            count += 1
            if count > MAX_FILES:
                return {"ok": False, "reason": "more than 50,000 files"}
            target = os.path.realpath(os.path.join(dest, name))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                continue
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o644)  # nothing extracted is executable
    return {"ok": True, "folder": dest, "repo": repo, "sha": sha, "files": count}


def clean(number):
    dest = folder(number)
    # Only ever removes our own download folder for that request.
    if os.path.isdir(dest) and os.path.realpath(dest).startswith(os.path.realpath(ROOT) + os.sep):
        shutil.rmtree(dest)
    return {"ok": True}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("queue").add_argument("--user", required=True)
    sub.add_parser("fetch").add_argument("--issue", type=int, required=True)
    sub.add_parser("clean").add_argument("--issue", type=int, required=True)
    args = parser.parse_args()
    if args.cmd == "queue":
        print(json.dumps(queue(args.user), indent=2, ensure_ascii=False))
        return 0
    result = fetch(args.issue) if args.cmd == "fetch" else clean(args.issue)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
