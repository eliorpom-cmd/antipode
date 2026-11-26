#!/usr/bin/env python3
"""Turn the markdown body of a request issue (GitHub issue form) into a dict.

Issue forms render each field as "### <label>" followed by the value; empty optional fields
read "_No response_". Other heading levels are accepted for known labels (see split_sections).
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
LABELS = {name: label for label, name in FIELDS.items()}
FORMAT_URL = "https://github.com/eliorpom-cmd/antipode/blob/main/plugins/antipode/references/request-format.md"
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _key(text):
    """Compare headings loosely: case, curly apostrophes, underscores and repeated spaces don't matter."""
    return " ".join(text.replace("’", "'").replace("_", " ").lower().split())


KNOWN = {_key(label): label for label in FIELDS}
# Internal field names, which an agent may copy from an older error message, and close synonyms.
ALIASES = {_key(name): label for name, label in LABELS.items() if _key(name) not in KNOWN}
ALIASES.update({_key(k): v for k, v in {
    "Fallback": "If something isn't covered", "Kind": "Type", "Repo": "Repository",
    "Base": "Base branch", "Branch": "Base branch", "Criteria": "Success criteria",
    "Terms": "Rules", "Pointers": "Where to start"}.items()})


def split_sections(body, hints=None):
    """Split on headings, ignoring any inside ``` or ~~~ code fences.

    The issue form writes "### <label>". A "#", "##" or "####" heading also counts when its text is
    a known label (or an alias, reported as a rename), so a body edited by hand or by an agent still
    parses; any other heading of another level stays part of the value. Lines that look like a label without "#" are reported in `hints`.
    """
    sections, current, fence = [], None, None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
            if current is not None:
                current[1].append(line)
            continue
        match = HEADING_RE.match(line) if fence is None else None
        if match and (len(match[1]) == 3 or _key(match[2]) in KNOWN or _key(match[2]) in ALIASES):
            label = KNOWN.get(_key(match[2]), match[2].strip())
            current = [label, []]
            sections.append(current)
            continue
        if fence is None and hints is not None and _key(line) in KNOWN and line == stripped:
            hints.setdefault("bare", []).append(KNOWN[_key(line)])
        if current is not None:
            current[1].append(line)
    return [(label, "\n".join(lines)) for label, lines in sections]


def parse(body):
    result, seen, hints = {}, set(), {}
    for label, value in split_sections(body, hints):
        name = FIELDS.get(label)
        if not name:
            if _key(label) in ALIASES:
                # Normalised name, not the raw text: problems are quoted back in the bot's comment.
                hints.setdefault("renamed", []).append((_key(label).capitalize(), ALIASES[_key(label)]))
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
    if hints:
        result["_hints"] = hints
    return result


def problems(request):
    """List what makes a parsed request unusable (empty list when valid)."""
    hints = request.get("_hints") or {}
    issues, explained = [], set()
    # A heading under another name: say which exact heading to write instead of "missing ...".
    for wrong, right in hints.get("renamed", []):
        if not request.get(FIELDS[right]):
            issues.append(f"rename the heading “### {wrong}” to “### {right}”")
            explained.add(FIELDS[right])
    # A label written as plain text: the "### " in front of it was lost.
    bare = [label for label in dict.fromkeys(hints.get("bare", [])) if not request.get(FIELDS[label])]
    if bare:
        issues.append("headings must start with “### ”: write " + ", ".join(f"“### {label}”" for label in bare)
                      + f" (format: {FORMAT_URL})")
        explained.update(FIELDS[label] for label in bare)
    issues += [f"missing “### {LABELS[name]}”" for name in REQUIRED
               if not request.get(name) and name not in explained]
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
    if terms.count("[X]") + terms.count("[x]") < 2 and "terms" not in explained:
        issues.append("rules not accepted")
    return issues


if __name__ == "__main__":
    parsed = parse(sys.stdin.read())
    print(json.dumps({"request": parsed, "problems": problems(parsed)}, indent=2))
