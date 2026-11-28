"""Tell the maintainer that a bot run failed: one comment on the single open "Bot run failed" issue,
which @mentions the repository owner (GitHub then sends an email).

Run by the `if: failure()` step of Intake, Commands, Sweep and Harvest with the workflow's own token:
the bot's token may be what broke. Standard library only, and independent of ats.py, so a broken
ats.py still raises the alert.

  python3 scripts/alert.py --workflow Harvest --run-url <url> [--issue 12]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

LABEL = "bot-alert"  # same as ALERT_LABEL in ats.py: Commands ignores this issue
TITLE = "Bot run failed"
QUIET_MINUTES = 60  # a scheduled workflow failing every 10 minutes sends one alert an hour, not six
MARK = "<!-- ats:alert {} -->"


def alert_body(workflow, run_url, issue, owner):
    about = f" while handling request #{issue}" if issue else ""
    return (f"@{owner} **{workflow}** failed{about}.\n\nRun: {run_url}\n\n"
            "Read the run's log, fix the cause, then check the labels of the requests it touched: "
            "the bot puts back labels that its own records name, on its next pass.\n\n"
            + MARK.format(workflow))


def iso_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def recently_alerted(comments, workflow, now):
    """True when this workflow already alerted in the last QUIET_MINUTES."""
    mark = MARK.format(workflow)
    for comment in comments:
        posted = iso_time(comment.get("createdAt"))
        if mark in (comment.get("body") or "") and posted and now - posted < QUIET_MINUTES * 60:
            return True
    return False


def gh(*args, input_text=None):
    result = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--issue", default="")
    args = parser.parse_args()
    repo, owner = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REPOSITORY_OWNER"]
    issue = args.issue if args.issue.isdigit() else ""
    body = alert_body(args.workflow, args.run_url, issue, owner)

    gh("label", "create", LABEL, "-R", repo, "--color", "b60205", "--force",
       "--description", "Bot runs that failed (not a request)")
    found = json.loads(gh("issue", "list", "-R", repo, "--label", LABEL, "--state", "open",
                          "--json", "number,title,comments", "--limit", "20") or "[]")
    found = sorted((i for i in found if i["title"] == TITLE), key=lambda i: i["number"])
    if not found:
        url = gh("issue", "create", "-R", repo, "--title", TITLE, "--label", LABEL, "--body-file", "-",
                 input_text="The Antipode bot posts here when one of its runs fails. Close this issue once "
                            "everything works; the next failure opens a new one.").strip()
        number = url.rstrip("/").split("/")[-1]
    elif recently_alerted(found[0]["comments"], args.workflow, time.time()):
        print(f"{args.workflow} already alerted in the last {QUIET_MINUTES} minutes")
        return
    else:
        number = str(found[0]["number"])
    print(gh("issue", "comment", number, "-R", repo, "--body-file", "-", input_text=body).strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"alert: {error}")
