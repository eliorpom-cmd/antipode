# Security

Antipode moves strangers' requests to volunteers' coding agents. Everything below assumes that a request,
the repository it points to, and any comment on it can be written by an attacker.

## Report a problem

Use GitHub's private vulnerability reporting on this repository (Security tab → Report a vulnerability).
Please don't open a public issue for anything exploitable.

## What runs where

| Where | What runs | What it can reach |
|---|---|---|
| Your computer | The plugin's skills and Python scripts (standard library only), with your `gh` login | Your GitHub account through `gh`, the plugin's cache folder, your Claude Code status line if you allow it |
| A Claude Code cloud session | The donor's agent doing the work | The donor's fork and this repository, with Claude Code's built-in GitHub tools. No connectors |
| GitHub Actions in this repository | `scripts/ats.py` (no AI) | Issues and labels of this repository only |

Code from a target repository never runs on your computer. It runs only inside the donor's cloud session.

## Threats and what stops them

**A malicious request or repository tries to take over the donor's agent (prompt injection).**
- Automatic checks list files that run on install, build or by agents (`package.json` scripts, hooks,
  `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, binaries).
- Two donors' agents read the request and the code before it's listed, one of them a trusted reviewer
  (the maintainer, or a donor with a merged pull request). One security flag hides it.
- Reviews download the code as an archive and read it with a subagent that has no terminal. Links,
  devices and paths outside the folder are dropped at extraction, and nothing extracted is executable.
- The donor's session reads the request only from the bot's frozen copy (comment ID passed in the
  prompt), never from the editable issue body or other people's comments.
- [AGENTS.md](AGENTS.md) lists what the agent must never do with its GitHub tools (merge, delete, touch
  other repositories, secrets, Actions).

**The owner changes the repository after review.**
- The reviewed commit is frozen. The agent checks out that exact commit and stops if it can't.
- Before a session starts, `preflight.py` compares the donor's fork with the reviewed commit and refuses
  if agent configuration changed (`.claude/`, `CLAUDE.md`, `AGENTS.md`, hooks, `.mcp.json`).
- Any edit to the request, even with a new title, sends it back to the checks and review.

**The donor's other data leaks through connectors.**
- Cloud routines inherit every connector of a claude.ai account by default. The plugin replaces them with
  a single placeholder that can't connect, then reads the routine back and deletes it if anything else is
  attached.

**Fake reviewers, griefing, forged state.**
- Posting a request needs a GitHub account older than 30 days; reviewing, taking and flagging need one
  older than 90 days. Nobody can review or take
  their own request, and a donor holds one request at a time.
- The bot's state lives in hidden records that count only when written by the bot and placed at the end
  of its own comment. Text from users that the bot repeats is escaped, so it can't create a record.
- Every slash command is answered, even when GitHub drops queued runs: each run catches up on unanswered
  comments.
- `/delivered` is accepted only for a pull request from the donor's fork and branch `claude/ats-<n>`.
  Self-reported token counts are capped.

**Values from strangers reach a shell.**
- Repository, branch, commit and model are validated against strict patterns before use. The skills run
  scripts that call `gh` with argument lists, never with pasted strings.

## Known limits

- The reviewer subagent is limited to read-only tools, and told to read only the downloaded folder; the
  tools themselves don't enforce that path. Its public reason is checked by the main agent before posting.
- The donor's cloud session keeps Claude Code's built-in GitHub tools for the donor's account. AGENTS.md
  restricts them; GitHub can't.
- A pull request opened by a donor's agent comes from the donor's GitHub account. Review it like any
  contribution from a stranger before merging.
- Antipode is new (public since November 2026). It was reviewed before launch by independent AI agents
  working without context (security, giving, asking); their findings were fixed and are covered by the
  tests in [`tests/`](tests) (`python3 -m unittest discover tests`). It has had no human third-party audit yet.
