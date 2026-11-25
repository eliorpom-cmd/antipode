#!/usr/bin/env python3
"""Turn the markdown body of a request issue (GitHub issue form) into a dict.

Issue forms render each field as "### <label>" followed by the value;
empty optional fields read "_No response_".
"""
import json
import re
import sys

# Issue form label → field name. Must match central/.github/ISSUE_TEMPLATE/request.yml.
FIELDS = {
    "Repository": "repo",
    "Base branch": "base",
    "Pitch": "pitch",
    "Goal": "goal",
    "Success criteria": "criteria",
    "Plan": "plan",
    "Decisions": "decisions",
    "If something isn't covered": "fallback",
    "Agent": "agent",
    "Model": "model",
    "Size": "size",
    "Type": "kind",
    "Where to start": "pointers",
    "Off limits": "off_limits",
    "Part of": "part_of",
    "Rules": "terms",
}
REQUIRED = ("repo", "base", "pitch", "goal", "criteria", "plan", "decisions", "fallback",
            "agent", "model", "size", "kind")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.+$)[A-Za-z0-9._-]{1,100}$")
# Values that end up in commands and API paths: plain git ref names and model names only.
BASE_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")
MODEL_RE = re.compile(r"^[A-Za-z0-9 .,:()_-]{1,80}$")
FALLBACKS = {"choose the simplest option": "choose", "stop and send me the question": "ask"}


def split_sections(body):
    """Split on "### " headings, ignoring any inside ``` or ~~~ code fences."""
    sections, current, fence = [], None, None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
        if fence is None and line.startswith("### ") and not stripped.startswith(("```", "~~~")):
            current = [line[4:].strip(), []]
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return [(label, "\n".join(lines)) for label, lines in sections]


def parse(body):
    result, seen = {}, set()
    for label, value in split_sections(body):
        name = FIELDS.get(label)
        if not name:
            continue
        if name in seen:
            result.setdefault("_duplicates", []).append(label)
            continue  # the first one is what GitHub shows on top; a later copy can't override it
        seen.add(name)
        value = value.strip()
        result[name] = None if value in ("", "_No response_") else value
    if result.get("size"):
        result["size"] = result["size"][0]  # "M (1 to 3 hours)" → "M"
    if result.get("fallback"):
        # Only the two options of the form count; anything else is reported as a problem.
        text = result["fallback"].lower()
        result["fallback"] = next((v for k, v in FALLBACKS.items() if text.startswith(k)), "invalid")
    if result.get("repo"):
        result["repo"] = result["repo"].removeprefix("https://github.com/").strip("/")
    return result


def problems(request):
    """List what makes a parsed request unusable (empty list when valid)."""
    issues = [f"missing {name}" for name in REQUIRED if not request.get(name)]
    repo = request.get("repo")
    if repo and not REPO_RE.match(repo):
        issues.append("repository must be owner/name")
    if request.get("_duplicates"):
        issues.append("each heading must appear once: " + ", ".join(request["_duplicates"]))
    if request.get("base") and not BASE_RE.match(request["base"]):
        issues.append("base branch must be a plain branch name")
    if request.get("model") and not MODEL_RE.match(request["model"]):
        issues.append("model must be a model name like “latest Opus, effort medium”")
    if request.get("fallback") == "invalid":
        issues.append("“If something isn't covered” must be one of the two options of the form")
    if request.get("size") and request["size"] not in ("S", "M", "L"):
        issues.append("size must be S, M or L")
    decisions = request.get("decisions") or ""
    if decisions and sum(1 for line in decisions.splitlines() if line.strip().startswith(("-", "*"))) < 1:
        issues.append("decisions must be a list, one choice per line")
    terms = request.get("terms") or ""
    if terms.count("[X]") + terms.count("[x]") < 2:
        issues.append("rules not accepted")
    return issues


if __name__ == "__main__":
    parsed = parse(sys.stdin.read())
    print(json.dumps({"request": parsed, "problems": problems(parsed)}, indent=2))
