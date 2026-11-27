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
| A Claude Code cloud session | The donor's agent doing the work | Its clones of the donor's fork and this repository. It pushes one branch (`claude/ats-<n>`) with `git` and reads public pages. No GitHub tools, no connectors |
| GitHub Actions in this repository | `scripts/ats.py` (no AI) | Issues and labels of this repository. Comments and pull requests go through the [`antipode-bot`](https://github.com/antipode-bot) account, which has no write access to any repository |

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
- The session has no GitHub tools. The routine that starts it disallows them (`disallowed_tools`), so
  an injected prompt can't read the donor's private repositories, open a pull request elsewhere, merge
  or delete anything. Through `git` it can only push to the repositories it was started with; a clone
  of the donor's other private repositories is refused.
- The session reports by pushing its branch with `Antipode-*` lines in its last commit message. The
  bot reads them from the donor's fork only, and opens the pull request itself.

**The owner changes the repository after review.**
- The reviewed commit is frozen. The agent checks out that exact commit and stops if it can't.
- Before a session starts, `preflight.py` compares the donor's fork with the reviewed commit and refuses
  if agent configuration changed (`.claude/`, `CLAUDE.md`, `AGENTS.md`, hooks, `.mcp.json`).
- Any edit to the request, even with a new title, sends it back to the checks and review.

**The donor's other data leaks through connectors.**
- Cloud routines inherit every connector of a claude.ai account by default. The plugin creates the
  routine with `clear_mcp_connections`, then reads it back and releases the request if any connector or
  setting is missing.

**Fake reviewers, griefing, forged state.**
- Posting a request needs a GitHub account older than 30 days; reviewing, taking and flagging need one
  older than 90 days. Nobody can review or take
  their own request, and a donor holds one request at a time.
- The bot's state lives in hidden records that count only when written by the bot and placed at the end
  of its own comment. Text from users that the bot repeats is escaped, so it can't create a record.
- Every slash command is answered, even when GitHub drops queued runs: each run catches up on unanswered
  comments.
- A delivery is accepted only for a pull request from the donor's fork and branch `claude/ats-<n>`.
  Self-reported token counts are capped.
- The bot's token can comment and open pull requests, nothing more: the `antipode-bot` account is not a
  collaborator of any repository. Labels and assignees are changed with the workflow's own token.

**Values from strangers reach a shell.**
- Repository, branch, commit and model are validated against strict patterns before use. The skills run
  scripts that call `gh` with argument lists, never with pasted strings.

## Known limits

- The reviewer subagent is limited to read-only tools, and told to read only the downloaded folder; the
  tools themselves don't enforce that path. Its public reason is checked by the main agent before posting.
- The session still pushes with the donor's Claude GitHub app. Setting that app to "Only select
  repositories" in [GitHub settings → Applications](https://github.com/settings/installations) is no
  longer needed for safety, but it narrows what `git` could reach if Anthropic's proxy ever let more
  through.
- A pull request opened for a donor is written by their agent, from their fork. Review it like any
  contribution from a stranger before merging.
- Antipode is new (public since November 2026). It was reviewed before launch by independent AI agents
  working without context (security, giving, asking); their findings were fixed and are covered by the
  tests in [`tests/`](tests) (`python3 -m unittest discover tests`). It has had no human third-party audit yet.
