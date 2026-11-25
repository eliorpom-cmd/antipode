# Request body format

Use these headings exactly, in this order (they match the issue form of the central
repository). Leave optional fields as `_No response_`.

```
### Repository

owner/name

### Base branch

main

### Pitch

One sentence on what the app does.

### Goal

What needs doing, 3 to 10 lines. Say why, not only what.

### Success criteria

- [ ] Checks the donor's agent can run or see for itself

### Plan

1. Step, with the files it touches
2. …

### Decisions

- Choice: answer
- Exact text on screen: "…"

### If something isn't covered

Choose the simplest option that fits the code, and explain it in the pull request
(or: Stop and send me the question (the request waits for my answer))

### Agent

Claude Code | Codex | Either

### Model

e.g. latest Opus, effort medium (effort optional: low, medium, high, xhigh or max; without it, Opus runs at medium and Sonnet at high)

### Size

S (under 1 hour) | M (1 to 3 hours) | L (3 to 5 hours)

### Type

code | security | docs | translation | research | writing

### Where to start

_No response_

### Off limits

_No response_

### Part of

_No response_

### Rules

- [X] I own or maintain this repository and I want pull requests from donors' agents on it.
- [X] The repository is public and the request follows the Anthropic and OpenAI usage policies.
```

## What makes a clear request (scored 1 to 5 at review)

- A stranger's agent can start without asking anything.
- Criteria are checkable: a command that passes, a screen that shows something, a file that exists.
- One outcome per request. "Add dark mode and fix the login bug" is two requests.
- Nothing left to decide: the plan says what to do in order, and Decisions answers every choice
  the work could meet. The donor's session runs alone; an open choice wastes their credit.
