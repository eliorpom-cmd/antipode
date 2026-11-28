# Setup (first run only)

`ROOT` below is the plugin folder: two levels above the skill's base directory.
The requests live in the public repository `eliorpom-cmd/antipode` (its issues). Write that name in
full in every command (`-R eliorpom-cmd/antipode`), never through a shell variable.
Everything uses the user's own `gh` login and their own subscription. Never ask for, read or
store Claude credentials.

**Every comment you post** on `eliorpom-cmd/antipode` (`/claim`, `/review`, `/release`) ends with this
line, on its own line: `<!-- antipode-plugin 0.5.0 -->`. The bot reads it to know the plugin is up to date; it is
invisible on GitHub. Use `--body-file` (or a body with a real line break) so the line stays separate.

1. `gh auth status` must succeed. If `gh` is missing, give the install link
   (https://cli.github.com) and stop. If it is not signed in, tell the user to run
   `gh auth login` in a terminal and stop.
2. `gh api repos/eliorpom-cmd/antipode --silent` must succeed. If it fails, show the error in one
   line (GitHub unreachable, or a token without access to public repositories) and stop.
3. Python: try `python3 --version`, then `python --version`, then `py -3 --version` (Windows). The
   first that prints Python 3.9 or later is `PY`; use it for every script. None: give
   https://www.python.org/downloads/ and stop.
4. Give mode only, when `~/.cache/antipode/usage.json` does not exist. The weekly limit is only
   visible to a status line. Explain this in one sentence and ask permission to install a small
   status line script. With a yes:
   - Copy `ROOT/scripts/usage_statusline.sh` to `~/.config/antipode/statusline.sh` (a stable path
     that survives plugin updates) and make it executable. On Windows without `sh`, say the weekly
     limit can't be read automatically and ask the user to type what `/usage` shows instead.
   - Read `~/.claude/settings.json`. If it is missing, start from `{}`. If it isn't valid JSON, stop
     and tell the user; never overwrite it. Save a copy as `~/.claude/settings.json.antipode-backup`.
   - If `statusLine.command` already points to `antipode/statusline.sh`, change nothing. If there is
     another status line, save its command to `~/.config/antipode/next_statusline` so it keeps working.
   - Set `"statusLine": {"type": "command", "command": "~/.config/antipode/statusline.sh"}` and write
     the file back with the rest untouched.
   - To undo later: restore the backup, or put the saved command back.
   After install, one message in any session fills the cache.
