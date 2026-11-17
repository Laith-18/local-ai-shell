#!/usr/bin/env python3
"""
Turn ai-shell's own history into draft eval tasks.

Every request ai-shell handles is logged to ~/.local/share/ai-shell/history.jsonl.
The ones that went wrong are the best test cases, because they're real questions
the model actually failed. This picks them out and writes TOML task stubs that
you finish by hand (check the accept regex, fill in a reference answer) and then
paste into eval/tasks.toml.

Picked: commands that failed (non-zero exit), usage errors that needed a retry,
commands you corrected with 'e', commands you cancelled, runaway replies, and
programs that weren't installed.

Usage:
  python3 eval/from_history.py                      # print drafts
  python3 eval/from_history.py -o eval/drafts.toml  # write them to a file
  python3 eval/from_history.py --since 2026-11-01

Standard library only. Never runs anything.
"""

import argparse
import json
import os
import re
import shlex
import sys
import tomllib
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-shell"
HISTORY = DATA_DIR / "history.jsonl"
TASKS_FILE = Path(__file__).resolve().parent / "tasks.toml"

# Exit codes that aren't failures for the command that produced them.
NOT_FAILURES = {("dnf", 100), ("dnf5", 100), ("grep", 1), ("pgrep", 1), ("diff", 1), ("rpm", 1)}


def program(cmd):
    try:
        words = shlex.split(cmd.split("\n", 1)[0])
    except ValueError:
        words = cmd.split()
    while words and (words[0] == "sudo" or "=" in words[0]):
        words.pop(0)
    return os.path.basename(words[0]) if words else ""


def why_interesting(entry):
    decision, code = entry.get("decision"), entry.get("exit_code")
    if entry.get("usage_error"):
        return "usage error (needed a retry)"
    if entry.get("edited") and decision == "ran":
        return "you corrected the command"
    if decision == "runaway":
        return "runaway reply"
    if decision == "missing":
        return "used a program that isn't installed"
    if decision == "cancelled":
        return "you cancelled it"
    if decision == "ran" and code not in (0, 130, None) and (program(entry.get("command", "")), code) not in NOT_FAILURES:
        return f"exit code {code}"
    return None


def slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "task"
    name, n = f"hist-{base}", 2
    while name in taken:
        name, n = f"hist-{base}-{n}", n + 1
    taken.add(name)
    return name


def toml_str(s):
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def load_history(path, since=None):
    entries = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if since and datetime.fromisoformat(entry.get("time", "1970-01-01T00:00:00+00:00")).date() < since:
            continue
        entries.append(entry)
    return entries


def drafts(entries, existing_prompts=(), existing_ids=()):
    """Group interesting entries by request; a corrected command that worked is the best example."""
    by_request = {}
    for entry in entries:
        reason = why_interesting(entry)
        request = (entry.get("request") or "").strip()
        if not request or request.lower() in existing_prompts:
            continue
        item = by_request.setdefault(request, {"reasons": [], "bad": [], "good": None})
        if reason:
            item["reasons"].append(reason)
            if entry.get("command") and not entry.get("edited"):
                item["bad"].append(entry["command"])
        if entry.get("decision") == "ran" and entry.get("exit_code") == 0 and (entry.get("edited") or entry.get("retry_of")):
            item["good"] = entry["command"]
    taken = set(existing_ids)
    out = []
    for request, item in by_request.items():
        if not item["reasons"]:
            continue
        lines = ["[[task]]", f"id = {toml_str(slug(request, taken))}", 'category = "from-history"',
                 'kind = "command"  # TODO: or "answer" if it should have answered in English',
                 f"prompt = {toml_str(request)}",
                 f"# Why it's here: {', '.join(dict.fromkeys(item['reasons']))}"]
        for bad in dict.fromkeys(item["bad"]):
            lines.append(f"# Model said: {bad.splitlines()[0]}")
        good = item["good"]
        if good:
            accept = toml_str("^" + re.escape(program(good)) + r"\b")
            lines.append(f"accept = [{accept}]  # TODO: tighten")
            lines.append(f"example = {toml_str(good)}")
        else:
            lines.append("accept = ['TODO']  # TODO: regex for a correct command")
            lines.append("example = \"TODO\"  # TODO: a correct command")
        bad_programs = {program(b) for b in item["bad"]} - ({program(good)} if good else set())
        if bad_programs:
            lines.append("reject = [" + ", ".join(toml_str('^' + re.escape(p) + r'\b') for p in sorted(bad_programs) if p)
                         + "]  # TODO: check")
        out.append("\n".join(lines))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Draft eval tasks from ai-shell's history.")
    parser.add_argument("--history", type=Path, default=HISTORY)
    parser.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    parser.add_argument("-o", "--out", type=Path, help="write drafts here instead of printing them")
    args = parser.parse_args(argv)

    if not args.history.exists():
        print(f"No history yet at {args.history}", file=sys.stderr)
        return 1
    with open(TASKS_FILE, "rb") as f:
        tasks = tomllib.load(f)["task"]
    found = drafts(load_history(args.history, args.since),
                   existing_prompts={t["prompt"].lower() for t in tasks},
                   existing_ids={t["id"] for t in tasks})
    header = (f"# {len(found)} draft tasks from {args.history} ({datetime.now():%Y-%m-%d}).\n"
              "# Finish the TODOs, then paste the ones worth keeping into eval/tasks.toml.\n")
    text = header + "\n\n".join(found) + "\n"
    if args.out:
        args.out.write_text(text)
        print(f"Wrote {len(found)} drafts to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
