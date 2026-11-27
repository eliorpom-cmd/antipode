# Antipode plugin

Four commands for Claude Code:

- `/antipode:start` checks your setup, explains both sides and suggests a first step. Run it first.
- `/antipode:give` first reviews up to 2 requests waiting to be listed, then shows 5 open requests. You
  pick one, and a cloud session does the work on your fork, within the credit you set. The Antipode bot opens the pull request.
- `/antipode:ask` reads your code, writes a plan, asks you every choice at once, and posts the request.
- `/antipode:review` only does the review part.

Reviews run in the `reviewer` subagent, which can only read (no terminal). The code is downloaded as an
archive and never installed, built or run.

Install and rules: see the [repository README](../../README.md).

## What it installs

With your permission, `give` sets a small status line script (`scripts/usage_statusline.sh`). Claude Code only
shows your weekly limit to status lines, so the script copies it to `~/.cache/antipode/usage.json`.
Your previous status line keeps working.

## Scripts

| Script | Does |
|---|---|
| `read_usage.py` | reads the cached weekly limit and proposes a ceiling (what’s left minus 5 %) |
| `shortlist.py` | draws the 5 requests: relays first, then clear requests from recent donors |
| `parse_request.py` | reads a request issue and lists what’s missing |
| `count_tokens.py` | counts the tokens a session used, from its own logs |
