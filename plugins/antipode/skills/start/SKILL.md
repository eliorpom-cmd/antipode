---
name: start
description: First-run tour of Antipode. Checks the setup, explains how giving and asking work, shows the commands, and suggests a first step on one of the user's projects. Use right after installing Antipode, or when the user asks "how does Antipode work", "what can I do with Antipode", "antipode help" or "get started".
---

# Antipode: first run

`ROOT` is the plugin folder, two levels above this skill's base directory. Keep every message short:
the user just installed this from a link and wants to see it work, not read a manual.

## 1. Check the setup

Run `ROOT/references/setup.md` steps 1 to 3 (not the status line yet). Show the result as a checklist:

```
  Antipode setup
  ✓ GitHub CLI signed in as <login>
  ✓ Antipode requests reachable (github.com/eliorpom-cmd/antipode)
  ✓ Python 3 (<version>)
```

Use ✗ and the one fix for anything missing, then stop until it is fixed.

Then one line: Antipode's updates don't install themselves. Suggest turning that on once:
`/plugin` → Marketplaces → antipode → Enable auto-update. Security fixes ship as plugin updates.

## 2. The tour, in one screen

Show exactly this block (fill `<login>`), then nothing else in that message but step 3's question:

```
  ◯  ANTIPODE  ·  your unused Claude Code limit, put to work

  Your weekly limit resets whether you used it or not.

  YOU HAVE LIMIT LEFT            YOU RAN OUT
  /antipode:give                 /antipode:ask
  1. Review 2 requests (safety)  1. Your agent plans the work
  2. Pick 1 of 5, or let Claude  2. You answer every choice once
  3. A cloud session does it,    3. A donor's agent does it and
     the bot opens the PR,          sends you a pull request
     stops at the share you set

  /antipode:review   review waiting requests only
  /antipode:start    this tour

  Safe by design
  · Donations run in a fresh cloud session: no access to your files, keys or connectors
  · That session has no GitHub tools: it can't see your other repos, only push its branch
  · Nothing from a stranger's repo ever runs on this computer
  · Public repos only; changes arrive as pull requests you can refuse
```

## 3. Suggest a first step

Ask with your question tool, recommended option first:

- **Ask for help on one of my projects**: list their public repositories
  (`gh repo list --visibility public --source --limit 30 --json nameWithOwner,description,pushedAt`),
  take the 4 most recently pushed, and offer them. If the current folder is a git repository with a
  public GitHub remote they own, offer it first. Then run the `ask` skill in that repository (clone it
  to a folder they choose if it isn't local).
- **Give my leftover limit now**: run the `give` skill.
- **Just look at the open requests**: `gh issue list -R eliorpom-cmd/antipode --label open --limit 10`
  and show titles and links.
- **Nothing for now**: say `/antipode:start` brings this back, and stop.

If they have no public repository, say that `ask` needs one (private code can't be reached by other
people's agents) and recommend giving first.

Codex support is coming soon; for now Antipode runs with Claude Code only. Say so only if asked.
