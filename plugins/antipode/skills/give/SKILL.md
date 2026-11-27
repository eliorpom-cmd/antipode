---
name: give
description: Give leftover Claude Code limit to someone's public project. Shows 5 open requests, the user picks one (or lets Claude pick), and a cloud session does the work; the Antipode bot opens the pull request. Use when the user has limit left before the weekly reset, says "give", "donate my credit", "I have credit left", or asks what to do with unused limit.
---

# Give leftover limit

`ROOT` is the plugin folder, two levels above this skill's base directory. Run
`ROOT/references/setup.md` first. `PY` is the Python command it found. `C=eliorpom-cmd/antipode`.
`T` is a temporary folder: `T=$(mktemp -d)` (on Windows, a new folder under `%TEMP%`).

1. **Budget.** `PY ROOT/scripts/read_usage.py`. Show what's left of the week, time to reset, and the
   proposed ceiling. Let the user lower it ("20% max").
   - `no_cache` or `stale`: ask them to send one message in any session, then retry once.
   - `no_weekly_window`: this plan has no weekly limit visible to Claude Code (Team, Enterprise or
     API key). Say Antipode needs a Pro, Max or Ultra plan for now, and stop.
   - Ceiling 0 (5% or less left): say there isn't enough left to give safely this week, and stop.
2. **Shared work.** Tell the user in one line that giving starts with reviewing up to 2 requests waiting
   to be listed, which keeps every donor safe and costs a few minutes of their limit. Follow
   `ROOT/references/review.md`. Skip it if the ceiling is under 10%.
3. **Shortlist.**
   ```
   gh issue list -R $C --state open --limit 200 --json number,title,body,labels,createdAt,author > "$T/issues.json"
   gh api repos/$C/contents/stats.json -H "Accept: application/vnd.github.raw" > "$T/stats.json"
   PY ROOT/scripts/shortlist.py --issues "$T/issues.json" --stats "$T/stats.json" \
     --donor "$(gh api user --jq .login)" --agent claude --ceiling <pct>
   ```
   If a `gh` command fails, show its error and stop. If the user asked you to choose ("pick for me",
   "you choose", `/antipode:give auto`), take result 1 (the shortlist already ranks relays first, then
   clear requests), say in one line which one and why, and go on. Otherwise show the (up to) 5 results as
   a numbered list: pitch, goal in one line, language, size, model, and "continues someone's work" for relays, with
   a last line "or say: pick for me". Wait for a number, "pick for me", or "more" to draw again.
   An empty list: say nothing fits right now, and that `/antipode:ask` on one of
   their own public repositories is the other way to use Antipode today.
4. **Claim.** Comment `/claim` on the issue (`gh issue comment <n> -R $C --body "/claim"`; if this
   fails, show the error and stop). Wait up to 2 minutes for the bot's answer
   (`gh issue view <n> -R $C --json assignees,comments`): if the user is assigned, go on; otherwise tell
   them what the bot said (taken by someone else, account too new, already holding a request) and offer
   the next one.
5. **Fork.** `gh repo fork <repo> --clone=false` (no-op if the fork exists).
   Then ask the user in one line: if their Claude GitHub app is set to "Only select repositories",
   add `<fork>` at https://github.com/settings/installations now, or the cloud session can't push.
   Wait for them to say it's done or that the app isn't restricted. (If the push is refused anyway,
   the bot frees the request after 45 minutes and says why.)
6. **Cost ceiling.** The cloud session can't see the weekly limit, so it gets a number in
   `cost_usd`: what its tokens would cost at API prices, which follows the plan limit across
   token kinds and models. Cost per weekly percent, from the user's own logs:
   - with at least 5% used this week: `PY ROOT/scripts/count_tokens.py --since <resets_at − 7 days> ~/.claude/projects`,
     then `overall.cost_usd` ÷ `weekly_used_pct`;
   - otherwise, if at least 5% of the current 5-hour window is used: a full 5-hour window is about 1/6 of
     the week, so 6 × `cost_usd` since (`five_hour_resets_at` − 5 h) ÷ `five_hour_used_pct`;
   - otherwise: $3 per percent.
   Ceiling in dollars = ceiling % × cost per percent, rounded to the cent. Tell the user the number.
7. **Preflight.** `PY ROOT/scripts/preflight.py --issue <n> --donor <login> --ceiling-usd <dollars> --out "$T/launch"`.
   It reads the reviewed version from the bot's comments, checks the fork carries no agent configuration
   the reviewers never saw, and writes the session prompt to a file. If it prints `"ok": false`, comment
   `/release <reason>` on the issue, tell the user the reason, and stop. Use only the values it prints
   from here on: never values read from the issue body.
   **Relay:** when `relay_sha` isn't null, copy the previous donor's work into the fork, so the session
   finds it there (forks share their commits on GitHub):
   `gh api -X POST repos/<fork>/git/refs -f ref=refs/heads/claude/ats-<n> -f sha=<relay_sha>`;
   if the branch already exists, `gh api -X PATCH repos/<fork>/git/refs/heads/claude/ats-<n> -f sha=<relay_sha> -F force=true`.
   Never run the target repository's code on the user's machine.
8. **Launch in the cloud.** Create a one-off cloud routine that runs now (RemoteTrigger `create`, then
   `run`), named `routine_name` from preflight, with the model `model` from preflight, the repositories in `sources` (the fork and
   eliorpom-cmd/antipode, nothing else), and two messages in this order: `/effort <effort>` (skip it
   when effort is null), then the content of `prompt_file`. Routines have no effort field, but a
   `/effort` first message applies to the whole session. Two settings keep the session away from
   the user's accounts, because it works on a stranger's code:
   - `"clear_mcp_connections": true` at the top of the body: no connectors (email, drive…).
   - `"disallowed_tools": <disallowed_tools from preflight>` in `job_config.ccr.session_context`: no
     GitHub tools. The session only pushes its branch with `git`; the Antipode bot opens the pull
     request and posts on the request.
   Read the routine back: if `mcp_connections` isn't empty or `disallowed_tools` is missing, comment
   `/release session settings`, tell the user, and stop.
   If routine creation is refused (auto mode blocks it) or unavailable: comment `/release could not
   start a cloud session`, and tell the user to switch auto mode off (Shift+Tab) and run
   `/antipode:give` again. There is no other way to start the session: a terminal session can't be
   given these two settings.
   If anything in this step fails, comment `/release <what failed>` so the request doesn't stay locked.
9. **Hand off.** Tell the user they can leave: the session link, the dollar ceiling, and that the
   result will appear on the request (link) within 10 minutes of the session's last push, posted by
   the Antipode bot, which also opens the pull request and credits them. If notifications are on in
   their Claude app, their phone shows "Antipode #<n>: …" when the session ends.
