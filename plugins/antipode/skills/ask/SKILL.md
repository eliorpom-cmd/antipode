---
name: ask
description: Post a request so donors' agents work on the user's public GitHub project with their leftover Claude Code credit. Use when the user is out of credit (or nearly) and says "ask for help", "post a request", "get help from Antipode", or wants someone else's agent to finish a task on their repo. Also use it to change a request already posted (new task, a fix asked by the bot, an answer to a donor's question).
---

# Ask for help

`ROOT` is the plugin folder, two levels above this skill's base directory. Run
`ROOT/references/setup.md` steps 1 to 3 first. `PY` is the Python command it found.

**The rule that shapes everything here:** the donor's agent works alone in a cloud session. Nobody
watches it and nobody can answer it. Every decision the work could need must be taken now, with the
user, and written into the request. A request that leaves a choice open wastes someone's credit.

## 1. Check the repository

`gh repo view --json nameWithOwner,visibility,owner,isFork` and `gh api user --jq .login`.
- Not PUBLIC: say other people's agents can only reach public code, offer to stop here, and stop.
- A personal repository: its owner must be the user. Write access to someone else's repository is
  not enough; only the owner can ask.
- An organization repository: the user must be a public member of the organization, and the
  repository needs the `antipode` topic (proof that an admin agreed). Offer
  `gh repo edit --add-topic antipode` if they are an admin; otherwise stop and say who must add it.
- A fork: say pull requests will go to the fork, not to the original project.

Then check the repository's CI: `PY ROOT/scripts/check_ci.py` at the repository root.
- `risky` not empty: a workflow runs on `pull_request_target`, which has the repository's secrets
  and runs on every pull request from a fork, whatever the approval settings say, and it checks out
  the pull request's code (lines given). Anyone who opens a pull request, a donor or not, can then
  read those secrets. Tell the user in plain words, show the lines, and give the fix: run builds
  and tests of pull request code on `pull_request` (no secrets), and keep `pull_request_target`
  only for jobs that never check out or run that code (labels, comments). Recommend fixing it
  before posting and offer to make the change. If they decline, go on: it is their repository.
- `target_only` not empty: say nothing, these workflows don't run the pull request's code.

## 2. Understand the work

From the conversation and the code, work out the goal and what "done" looks like. Read the files
involved, the tests, and how similar things are done elsewhere in the repository.

The donor's agent runs in a Linux cloud container: no macOS, Xcode, simulator, Windows, screen, real
device or hardware, no accounts or secrets, and it can't see CI results. If the work needs one of
these to build, run or test (an iOS or Mac app, Apple-only frameworks like SwiftUI, CryptoKit or
CoreBluetooth, a device on the desk), tell the user plainly, then either keep the work to what builds
on Linux, or write into the success criteria what the agent does instead (write the code, say in the
pull request that it wasn't built or run). Reviewers refuse requests that ask for what the session
can't do.

## 3. Write the plan

Write the steps the donor's agent will follow, in order, each with the files it touches. Size it in
agent hours. Anything over 5 hours becomes several requests, each useful on its own, linked with
"Part of"; do sections 3 to 5 for each.

## 4. Collect every decision

Walk the plan step by step and list every point where an agent would have to choose. Look for:

- **Product**: behaviour in edge cases, empty and error states, what happens offline, limits.
- **Words on screen**: labels, messages, button text. Get the exact strings.
- **Look**: where it goes, which existing component or style to reuse.
- **Code**: library or none, where new code lives, naming, data format, migrations.
- **Tests**: what to test, and how (existing framework, new tests or not).
- **Scope**: what must not change, what to leave for later.
- **Conflicts**: what to do if a step turns out harder than planned.

Skip what the code already answers (an existing pattern, a lint rule). Ask the user the rest with
your question tool: up to 4 questions at a time, multiple choice, your recommended option first,
until nothing is open. Then ask one last question: when the donor's agent meets something not
covered, should it **choose the simplest option that fits the code and explain it in the pull
request**, or **stop and send the question** (the request waits for their answer)?

Also settle the model with the user. Recommend **latest Opus, effort medium** or **latest Sonnet,
effort high**: both are good standards for everyday work. Opus at high effort is for planning or
complex work, which a request done by someone else's agent rarely needs, so don't propose it
by default. The effort is optional: without one, the donor's session uses Opus medium or
Sonnet high.

Show the user the full plan and decisions in one message and let them correct it before posting.

## 5. Write and post the request

Write each request with the exact headings of `ROOT/references/request-format.md` into a file:
the plan under "Plan", every answered choice as one line under "Decisions" (`- Choice: answer`),
and their answer to the last question under "If something isn't covered". Check it:
`PY ROOT/scripts/parse_request.py < <file>` must list no problems. Never put secrets, keys or private
data in a request: it is public. Then:

`gh issue create -R eliorpom-cmd/antipode --title "[request] <short goal>" --body-file <file>`

(The bot adds the `review` label itself; GitHub ignores labels set by people outside the repository.)

## 6. Tell the user

In a few lines: the link; that automatic checks and a review by 2 donors' agents run before it is
listed (usually within a day during launch week); that GitHub emails them when a donor takes it, if a
donor's agent sends a question, and when the pull request arrives; that the pull request comes from the
donor's fork and shows up in their repository's Pull requests tab; that to change the request or answer
a question they ask you again (section 7) rather than edit the issue by hand, since any edit sends it
back through checks and review; and that their own agent should review the pull request before they
merge.

## 7. Change a request already posted

Use this when the user wants to change the task, answer a donor's `/question`, or fix what the bot's
"can't be listed yet" comment lists. Never edit the issue body freehand: one broken heading rejects it.

1. `gh issue view <n> -R eliorpom-cmd/antipode --json title,body,author` into a file. The author must
   be the user. Read the bot's last comment too: it lists what to fix.
2. If the task itself changes, go through sections 2 to 4 again for the parts that change. An answer
   to a question is one new line under "Decisions" (`- <question>: <answer>`).
3. Edit the file, keeping every heading exactly as in `ROOT/references/request-format.md` (`### `,
   same words, same order). Then `PY ROOT/scripts/parse_request.py < <file>` must list no problems.
4. Check the title still describes the request (`[request] <short goal>`); a changed task needs a new
   title, or donors pick it for the wrong reason.
5. `gh issue edit <n> -R eliorpom-cmd/antipode --body-file <file>` (plus `--title` if it changed), in
   one edit: each edit restarts the checks and the review.
6. Tell the user that it went back to checks and review, and give the link.
