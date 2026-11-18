#!/usr/bin/env python3
"""
ai-shell: a terminal assistant for Fedora KDE Plasma, backed by a local Ollama model.

Ask in plain English:
  - General questions get a plain-English answer.
  - Questions about this machine get a command. Read-only commands run straight
    away and the result is explained in plain English. Anything that changes the
    system tells you, in English, why it needs permission and waits for you.

Usage:
  ai                                  interactive mode with the welcome screen
  ai "how much storage do i have"     one-shot
  ai --no-banner                      interactive mode, no welcome screen

In interactive mode: /help, /learned, /unlearn, /forget, /raw, /stats, clear, exit.
Standard library only, so it runs with the system Python and no pip installs.
Set AI_SHELL_ANIMATE=0 to turn off the welcome animation.
"""

import argparse
import contextlib
import getpass
import json
import os
import platform
import random
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import readline  # line editing, up-arrow history, prefilled edit prompt
except ImportError:
    readline = None

# ----------------------------------------------------------------- settings --
VERSION = "2.0.0"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("AI_SHELL_MODEL", "qwen3.5:4b")
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-shell"
LOG_FILE = DATA_DIR / "history.jsonl"
READLINE_FILE = DATA_DIR / "readline_history"

TTY = sys.stdout.isatty()
ANIMATE = TTY and os.environ.get("AI_SHELL_ANIMATE", "1") != "0"

MAX_REPLY_TOKENS = 900       # cap per reply; stops runaway loops quickly
MAX_COMMAND_CHARS = 400      # a one-line "command" longer than this is a runaway
MAX_SCRIPT_CHARS = 6000      # limit for multi-line commands that write a file
MEMORY_FILE = DATA_DIR / "memory.json"
MAX_MEMORIES = 40
OUTPUT_PREVIEW_LINES = 15    # lines of raw output shown for read-only commands
SUMMARY_INPUT_CHARS = 4000   # how much command output the model gets to read
HISTORY_MESSAGES = 10        # remembered messages for follow-up questions
NUM_CTX = int(os.environ.get("AI_SHELL_NUM_CTX", "8192"))  # context window; Ollama's default is smaller
THINK_MODES = ("off", "auto", "on")
DEFAULT_THINK = os.environ.get("AI_SHELL_THINK", "on")
# Reasoning first made qwen3.5:4b far more accurate (81.6% -> 89.7% on the eval), but a few
# replies thought for 2,000-3,900 tokens (up to a minute) and one ran out and said nothing.
# So thinking gets a budget: past it, ai-shell stops and asks again without thinking.
THINK_BUDGET = int(os.environ.get("AI_SHELL_THINK_BUDGET", "1000"))  # ~17 s at 60 tok/s
HELP_TEXT_CHARS = 2500       # how much --help output the model gets when fixing a command
PASTE_WINDOW = 0.05          # seconds: lines arriving this close together are one paste

# ------------------------------------------------------------------ prompts --
# Shared by every prompt in the pipeline, so the summary step never contradicts the router.
FEDORA_RULES = """Fedora facts:
- Software is installed with dnf (never yum, apt or rpm-ostree). RPM Fusion is enabled.
- In dnf, "update" is just another name for "upgrade". --refresh makes dnf re-download the package lists first. Update everything with: sudo dnf upgrade --refresh
- "dnf check-upgrade" exits with code 100 when updates are available; that is not an error.
- Logs come from journalctl; there is no /var/log/syslog.
- / and /home are btrfs subvolumes on the same partition, so count that space once. Ignore tmpfs, efivarfs and zram entries."""

ROUTER_PROMPT = """You are ai-shell, a friendly assistant living in a terminal on Fedora Linux with the KDE Plasma desktop.

Decide what the user needs:
- If the verified facts below answer the question, answer from them directly in plain English.
- Otherwise, if answering needs information from this computer, or they want something done on it, reply with ONE line: "$ " followed by a single shell command. Nothing else.
- Never guess facts about this computer, the current date or the time: use the verified facts or a command (e.g. $ date).
- If it is a general question that isn't about this machine, answer in plain English in 1-4 short sentences. No markdown.
- If the user pastes command output, explain what it shows; don't re-run the command unless they ask. Only quote numbers that appear in it, with the label they appear under (e.g. "Fan 42%" is the fan, not power).
- If the user says they've already tried or done something, don't suggest it again: suggest different things, or say you're out of ideas.
- Never tell the user to run a command, or ask whether you should: if a command answers the question, reply with that command and ai-shell runs it.

About you: you are the {model} model, running entirely on this computer's own GPU through Ollama, so nothing the user asks leaves the machine (the computer itself can still be online: see the facts). The first reply after a break takes a few seconds longer while the model loads. You are far smaller and less capable than big cloud chatbots like Claude or ChatGPT, but you are private and free. Each reply shows its speed underneath. That speed is normal for a local model of your size on this GPU: it is set mainly by the model's size and the GPU's memory bandwidth. Don't invent other explanations, and don't make claims about how fast other AI services are.

{facts}

Command rules:
- Do exactly what was asked; don't chain extra commands or add unneeded flags.
- Don't pipe into head or tail unless the user asks for a top N or the last N lines; ai-shell already shortens long output.
- Only use programs listed as installed, or common core tools. Never invent programs or options.
- Never use commands that wait for keyboard input, such as cat with no file.
- To ADD a line to an existing file, append with >>, e.g. $ echo 'alias ll="ls -la"' >> ~/.bashrc
  A single > REPLACES the whole file, so never use > on a file that already exists (like ~/.bashrc) unless the user asks to replace it.
- To create a new file, use a heredoc: the first line is $ cat > /path/to/file << 'EOF' then the file's
  content on the following lines, then EOF alone on the last line (use cat >> to append several lines).
  This is the only time you may reply with more than one line.
- Install software with dnf, e.g. $ sudo dnf install htop
- GUI apps can also come from Flathub, e.g. $ flatpak install flathub discord
- Never remove the NVIDIA driver (akmod-nvidia) or kernel packages unless the user asks for exactly that.
- Containers use podman, not docker.
- Sunshine runs as a systemd USER service named exactly "sunshine": always add --user (systemctl --user, journalctl --user) and never sudo.
- Ollama runs as a SYSTEM service named "ollama": no --user; sudo only to change it (start, stop, restart).
- Never combine sudo with --user: it targets root's services, not yours, and fails.
- "it", "that" and "this" mean the one thing just discussed. Act on that one thing only.
- Only use sudo when actually required.

{fedora_rules}

Known-good commands. When one fits the request, use it exactly:
- GPU load, VRAM, temperature, power: $ nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv
- What is using the GPU: $ nvidia-smi
- Motherboard model: $ cat /sys/class/dmi/id/board_vendor /sys/class/dmi/id/board_name
- BIOS version: $ cat /sys/class/dmi/id/bios_vendor /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date
- CPU details: $ lscpu
- RAM sticks, speed and slots: $ sudo dmidecode -t memory
- IP addresses: $ ip -brief address
- Are updates available: $ dnf check-upgrade
- Failed services: $ systemctl --failed
- Errors since boot: $ journalctl -b -p err --no-pager
- Temperatures: $ sensors
- Local AI models: $ ollama list   (loaded right now: $ ollama ps)
- Is the internet working right now: $ ping -c 3 1.1.1.1
- How long the computer has been on: $ uptime -p
- Is a program running: $ pgrep -a firefox
- Empty a folder but keep the folder itself: $ find ~/Downloads -mindepth 1 -delete
- Kernel messages: $ journalctl -k -n 50 --no-pager
- Sunshine status / logs: $ systemctl --user status sunshine   /   $ journalctl --user -u sunshine -n 50 --no-pager
- Start, stop or restart Sunshine: $ systemctl --user restart sunshine
- Ollama status / logs: $ systemctl status ollama   /   $ journalctl -u ollama -n 50 --no-pager
- Start, stop or restart Ollama: $ sudo systemctl restart ollama

Things the user has taught you. Always follow these; they override everything above:
{memories}

Context: {context}"""

SUMMARY_PROMPT = """You are ai-shell on Fedora Linux (KDE Plasma). You ran a command for the user; now explain the result.
Answer their original question directly in plain English, in 1-3 short sentences, using specific numbers and names from the output. No markdown, and don't restate the command.
If the output already answers the question, just say it naturally in one short sentence.
If the command failed, say what went wrong and suggest a fix in one sentence. Only suggest programs and options you are sure exist.
If the output is empty, say so plainly; never claim something worked unless the output shows it.
If the output is only column headings with no values, say the command returned no data. Never conclude from that alone that hardware, drivers or software are missing: the verified facts below are true.

{facts}

{fedora_rules}"""

FIX_PROMPT = """You are ai-shell on Fedora Linux. A command you suggested failed with a usage error (a wrong option or field name).
Using the error and the help text, reply with ONE line: "$ " followed by a corrected command that answers the user's question. Nothing else.
Use only options and field names that appear in the help text. Don't add head or tail.
If this program can't do it, use a different installed program instead.

{facts}"""

EXPLAIN_PROMPT = """In one short sentence for a non-expert, say what this Linux command will change on the computer. Start with a verb. No markdown."""

# ---------------------------------------------------------------- colours ----
BOLD, DIM = "1", "2"
RED, GREEN, YELLOW, MAGENTA, CYAN = "31", "32", "33", "35", "36"


def paint(text, *codes):
    if not TTY or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


# --------------------------------------------------------- safety rules ------
# Never run these, even if asked.
BLOCKED = [
    (r"\brm\s+.*-\w*[rR]\w*.*\s(/|/\*|~/?|\$HOME/?|\"\$HOME\"/?)(\s|;|&|$)",
     "would delete / or your whole home folder"),
    (r"\bmkfs(\.\w+)?\b", "would format a drive"),
    (r"\bdd\b.*\bof=/dev/", "would write raw data straight onto a disk"),
    (r">\s*/dev/(sd|nvme|vd|hd)", "would overwrite a disk"),
    (r":\(\)\s*\{.*\};\s*:", "is a fork bomb that would freeze the machine"),
    (r"\bchmod\s+(-R\s+)?0?777\s+/(\s|$)", "would make the whole system world-writable"),
    (r"\brm\s+.*-\w*[rR]\w*.*\s/(bin|boot|dev|etc|home|lib|lib64|opt|proc|root|sbin|srv|sys|usr|var)"
     r"/?(\s|;|&|$)", "would delete a core system folder"),
    (r"\brm\s+.*-\w*[rR]\w*.*\s/home/[^/\s;&|]+/?(\s|;|&|$)", "would delete someone's whole home folder"),
]

# Shown as extra detail on the permission prompt. strong=True means type 'yes'.
PERMISSION_RULES = [
    (r"\b(sudo|pkexec|run0)\b", "runs as root", False),
    (r"\b(rm|shred|unlink)\b", "deletes files", True),
    (r"\b(dnf|dnf5)\b.*\b(remove|erase|autoremove|swap|distro-sync|system-upgrade|downgrade)\b",
     "removes software or changes the whole system", True),
    (r"\b(dnf|dnf5)\b.*\b(install|reinstall|upgrade|update)\b", "installs or updates software", False),
    (r"\b(yum|apt|apt-get|rpm-ostree)\b", "uses a tool that isn't Fedora's package manager", False),
    (r"\brpm\b\s+-(i|U|e|F)", "installs or removes a package directly", True),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "shuts down or restarts the machine", True),
    (r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z)?sh\b", "runs a script from the internet", True),
    # The same thing without a pipe: bash <(curl …), sh -c "$(curl …)", source <(wget …).
    # The thinking 4B wrote 'bash <(curl -sL …)' on two of three repeats, which only needed 'y'.
    (r"\b(ba|z|da|k)?sh\b[^|;&]*?(<\(|\$\(|`)\s*(sudo\s+)?(curl|wget)\b", "runs a script from the internet", True),
    (r"(^|[;&|(]\s*|\s)(source|\.)\s+<\(\s*(curl|wget)\b", "runs a script from the internet", True),
    (r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(python3?|perl|ruby|node)\b", "runs a script from the internet", True),
    (r"\b(kill|killall|pkill)\b", "stops running programs", True),
    (r"\b(chmod|chown)\b", "changes file permissions", True),
    (r"\bflatpak\b.*\b(install|uninstall|remove|update)\b", "installs, removes or updates apps", False),
    (r"\bsystemctl\b.*\b(start|stop|restart|enable|disable|mask)\b", "starts, stops or changes a service", False),
    (r"\b(mv|cp)\b", "moves or copies files", False),
    (r"-delete\b", "deletes files", True),
    (r"\bsed\b.*\s-i", "edits a file in place", False),
    (r"(?<![\d&])>(?!\s*/dev/null|&)", "writes to a file", False),
    (r"\bpodman\b.*\b(rm|rmi|stop|kill|prune)\b", "removes or stops containers", True),
]

# Commands that only read. Anything not covered here needs permission.
READ_ONLY_COMMANDS = {
    "ls", "df", "du", "free", "ps", "pgrep", "pidof", "uptime", "whoami", "id", "groups",
    "hostname", "uname", "date", "cal", "cat", "head", "tail", "wc", "sort", "uniq", "grep",
    "egrep", "fgrep", "rg", "awk", "sed", "cut", "tr", "column", "nl", "stat", "file", "which",
    "whereis", "type", "echo", "printf", "pwd", "env", "printenv", "lsblk", "lscpu", "lspci",
    "lsusb", "lsmod", "findmnt", "ss", "netstat", "ping", "nvidia-smi", "sensors", "journalctl",
    "find", "tree", "realpath", "basename", "dirname", "sha256sum", "md5sum", "jq", "nproc",
    "vmstat", "iostat", "lsof", "fastfetch", "neofetch", "dmesg", "last", "w", "who", "locale",
    "getent", "fc-list", "vulkaninfo", "glxinfo", "inxi", "test", "true", "clear",
    "top", "htop", "btop", "nvtop", "less", "more", "man",
}
READ_ONLY_SUBCOMMANDS = {
    "systemctl": {None, "status", "is-active", "is-enabled", "is-failed", "list-units",
                  "list-unit-files", "list-timers", "list-sockets", "show", "cat"},
    "flatpak": {"list", "info", "search", "remotes", "remote-ls", "history", "ps"},
    "podman": {"ps", "images", "logs", "inspect", "version", "info", "top", "port"},
    "dnf": {"list", "info", "search", "repolist", "repoquery", "provides", "check-update",
            "check-upgrade"},
    "dnf5": {"list", "info", "search", "repolist", "repoquery", "provides", "check-update",
             "check-upgrade"},
    "git": {"status", "log", "diff", "show"},
    "loginctl": {None, "list-sessions", "list-users", "show-session", "show-user",
                 "session-status", "user-status"},
    "timedatectl": {None, "status", "show"},
    "hostnamectl": {None, "status"},
    "ollama": {"list", "ps", "show"},
    "resolvectl": {None, "status"},
    "bootctl": {None, "status"},
}
FORBIDDEN_FLAGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
    "journalctl": {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush",
                   "--sync", "--relinquish-var", "--setup-keys", "--update-catalog"},
    "dmesg": {"-c", "-C", "-D", "-E", "--clear", "--read-clear"},
    "date": {"-s", "--set"},
    "git": {"--output", "--ext-diff"},                 # writes a file / runs a diff program
    "rg": {"--pre"},                                 # runs a program on every file
    "tree": {"-o"},                                    # writes its output to a file
    "ss": {"-K", "--kill"},                            # kills sockets
    "awk": {"-i", "--include", "-l", "--load", "-f", "--file"},  # gawk -i inplace edits files
    "nvidia-smi": {"-pl", "--power-limit", "-r", "--gpu-reset", "-pm", "--persistence-mode",
                   "-c", "--compute-mode", "-e", "--ecc-config", "-ac", "--applications-clocks",
                   "-rac", "--reset-applications-clocks", "-lgc", "--lock-gpu-clocks", "-rgc",
                   "--reset-gpu-clocks", "-lmc", "--lock-memory-clocks", "-rmc",
                   "--reset-memory-clocks", "-am", "--accounting-mode", "-caa",
                   "--clear-accounted-apps", "-mig", "--multi-instance-gpu"},
}
# Single-letter options that must not appear anywhere in a bundle like -nro or -tK.
FORBIDDEN_SHORT_LETTERS = {"sort": "o", "ss": "K"}
# Options that take a value, so the next word isn't a file name (used for uniq).
UNIQ_VALUE_FLAGS = {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}
# GNU sed: s///e and the e command run shell commands; w/W write files.
AWK_SIDE_EFFECTS = re.compile(r"system\s*\(|getline|[|>]")
SED_SUBST_RE = re.compile(r"""[sy]([^\\\n])(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1([A-Za-z0-9]*)""")
SED_ADDRESS_RE = re.compile(r"/(?:\\.|[^/\\])*/|\\(.)(?:\\.|(?!\1).)*\1")
IP_WRITE_WORDS = {"set", "add", "del", "delete", "flush", "change", "replace", "append"}

# Full-screen programs: run attached to the terminal, nothing to summarise.
INTERACTIVE = {"clear", "reset", "top", "htop", "btop", "nvtop", "less", "more", "man", "nano", "vim", "vi",
               "nvim", "ssh", "ncdu", "watch", "tmux", "screen"}

SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;&\n]")
SAFE_REDIRECTS = re.compile(r"(?:\d?>|&>)\s*/dev/null(?![\w/.-])|\d?>&\d")


def tokens_for(segment):
    """Tokenise one pipeline segment, skipping VAR=value and sudo prefixes."""
    tokens = shlex.split(segment)
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] == "sudo":
        tokens.pop(0)
        while tokens and tokens[0].startswith("-"):
            tokens.pop(0)
    return tokens


def sed_is_read_only(args):
    """True if a sed invocation can't edit, write or execute anything."""
    scripts, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-i") or a.startswith("--in-place") or a in ("-f", "--file") \
                or a.startswith("--file="):
            return False  # edits in place, or runs a script file we can't inspect
        if a in ("-l", "--line-length"):
            i += 2  # takes a number, which isn't the script
            continue
        if a in ("-e", "--expression"):
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-e") and len(a) > 2:
            scripts.append(a[2:])
        elif not a.startswith("-") and not scripts:
            scripts.append(a)  # first plain argument is the script; the rest are files
        i += 1
    for script in scripts:
        for match in SED_SUBST_RE.finditer(script):
            if set(match.group(2)) & set("ewW"):
                return False
        rest = SED_ADDRESS_RE.sub("", SED_SUBST_RE.sub("", script))
        if set(rest) & set("ewW"):
            return False  # e = execute, w/W = write a file
    return True


def segment_is_read_only(segment):
    try:
        tokens = tokens_for(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    name, args = os.path.basename(tokens[0]), tokens[1:]

    if name == "ip":
        return not any(a in IP_WRITE_WORDS for a in args)
    if name == "command":  # 'command -v X' only looks X up; 'command X' would run X
        return len(args) >= 2 and args[0] in ("-v", "-V")
    if name == "rpm":  # only queries (rpm -qa, rpm -qf, rpm -qi ...) are read-only
        return bool(args) and (args[0].startswith("-q") or args[0] == "--query")
    if name not in READ_ONLY_COMMANDS and name not in READ_ONLY_SUBCOMMANDS:
        return False

    for flag in FORBIDDEN_FLAGS.get(name, ()):
        # "-o file", "--output=file", and the attached short form "-ofile"
        short_attached = len(flag) == 2 and flag[1] != "-"
        if any(a == flag or a.startswith(flag + "=") or (short_attached and a.startswith(flag))
               for a in args):
            return False
    letters = FORBIDDEN_SHORT_LETTERS.get(name, "")
    if letters and any(a.startswith("-") and not a.startswith("--") and set(letters) & set(a[1:])
                       for a in args):
        return False
    if name in READ_ONLY_SUBCOMMANDS:
        sub = next((a for a in args if not a.startswith("-")), None)
        return sub in READ_ONLY_SUBCOMMANDS[name]
    if name == "env":
        return all(a in ("-0", "--null") for a in args)  # 'env CMD' runs CMD
    if name == "uniq":
        files, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in UNIQ_VALUE_FLAGS:
                skip = True
            elif not a.startswith("-") or a == "-":
                files.append(a)
        if len(files) > 1:
            return False  # 'uniq IN OUT' overwrites OUT
    if name == "sed" and not sed_is_read_only(args):
        return False
    if name == "ping" and not any(a.startswith("-c") for a in args):
        return False  # without -c, ping never stops
    if name == "hostname" and any(not a.startswith("-") for a in args):
        return False  # 'hostname newname' renames the machine
    if name == "awk" and AWK_SIDE_EFFECTS.search(segment):
        return False  # awk can write files (print > f), pipe to a shell (print | "sh") or run commands
    return True


def mask_quoted(cmd):
    """Same length as cmd, with everything inside quotes (and escaped chars) replaced by '_'.

    Lets shell operators like | ; & > be found only where the shell would see them,
    so grep -E "a|b" is one command, not two. Unclosed quotes mask to the end.
    """
    out, quote, i = [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == quote:
                quote = None
                out.append(c)
            elif c == "\\" and quote == '"' and i + 1 < len(cmd):
                out += "__"
                i += 1
            else:
                out.append("_")
        elif c == "\\" and i + 1 < len(cmd):
            out += "__"
            i += 1
        else:
            if c in "'\"":
                quote = c
            out.append(c)
        i += 1
    return "".join(out)


def split_segments(cmd):
    """Split on |, ||, &&, ;, & and newlines that are outside quotes."""
    masked, segments, start = mask_quoted(cmd), [], 0
    for m in SEGMENT_SPLIT.finditer(masked):
        segments.append(cmd[start:m.start()])
        start = m.end()
    segments.append(cmd[start:])
    return [s.strip() for s in segments if s.strip()]


def is_read_only(cmd):
    if "$(" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd:
        # Command substitution, and process substitution <(…) / >(…), run whatever is inside them,
        # so 'ls <(rm -rf ~)' is not a read-only command.
        return False
    stripped = SAFE_REDIRECTS.sub("", cmd)
    if ">" in mask_quoted(stripped):
        return False  # writes to a file
    segments = split_segments(stripped)
    return bool(segments) and all(segment_is_read_only(s) for s in segments)


def assess(cmd):
    """Return (verdict, reasons, strong) where verdict is blocked/read_only/permission."""
    cmd = command_head(cmd)
    blocked = [why for pat, why in BLOCKED if re.search(pat, cmd)]
    if blocked:
        return "blocked", blocked, True
    if is_read_only(cmd):
        return "read_only", [], False
    hits = [(why, strong) for pat, why, strong in PERMISSION_RULES if re.search(pat, cmd)]
    reasons = [why for why, _ in hits] or ["isn't on the read-only list"]
    return "permission", reasons, any(strong for _, strong in hits)


def first_program(cmd):
    try:
        tokens = tokens_for((split_segments(cmd) or [""])[0])
    except ValueError:
        return ""
    return os.path.basename(tokens[0]) if tokens else ""


# ------------------------------------------------------------ ollama API -----
class OllamaError(Exception):
    pass


def _post(path, payload, timeout=300):
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(path, timeout=1.5):
    with urllib.request.urlopen(f"{OLLAMA_URL}{path}", timeout=timeout) as resp:
        return json.load(resp)


class ThinkingTooLong(Exception):
    pass


def chat_stream(model, messages, keep_alive, num_predict, think=False, on_thinking=None, on_fallback=None):
    """Yield (text_piece, final_stats_or_None) as the model generates.

    With think=True the model reasons first. Ollama sends that reasoning separately
    (message.thinking), so it never reaches the reply text; on_thinking() is called
    while it happens so the spinner can say so. If the reasoning runs past THINK_BUDGET
    tokens before any reply text, the request is dropped and asked again without
    thinking; the final stats then carry think_fallback=True.
    """
    if not think:
        yield from _chat_stream(model, messages, keep_alive, num_predict, False, None)
        return
    try:
        yield from _chat_stream(model, messages, keep_alive, num_predict, True, on_thinking)
    except ThinkingTooLong:
        if on_fallback:
            on_fallback()
        for piece, done in _chat_stream(model, messages, keep_alive, num_predict, False, None):
            yield piece, ({**done, "think_fallback": True} if done is not None else None)


def _chat_stream(model, messages, keep_alive, num_predict, think, on_thinking):
    payload = {
        "model": model, "messages": messages, "stream": True, "think": think,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": num_predict + (THINK_BUDGET + 50 if think else 0),
                    "repeat_penalty": 1.1, "num_ctx": NUM_CTX},
    }
    try:
        resp = _post("/api/chat", payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "think" not in body.lower():
            raise OllamaError(body.strip() or str(e)) from e
        payload.pop("think")  # model doesn't support the think flag
        resp = _post("/api/chat", payload)
    thought, replied = 0, False
    with resp:
        for raw in resp:
            if not raw.strip():
                continue
            data = json.loads(raw)
            if data.get("error"):
                raise OllamaError(data["error"])
            message = data.get("message") or {}
            piece = message.get("content") or ""
            if message.get("thinking"):
                thought += 1  # Ollama streams roughly one token per chunk
                if on_thinking:
                    on_thinking()
                if think and not replied and thought > THINK_BUDGET:
                    raise ThinkingTooLong()  # leaving the with block closes the connection
            replied = replied or bool(piece.strip())
            if data.get("done"):
                yield piece, data
                return
            yield piece, None


def chat(model, messages, keep_alive, num_predict, think=False):
    text, final = "", {}
    for piece, done in chat_stream(model, messages, keep_alive, num_predict, think):
        text += piece
        final = done or final
    return text, final


def unload(model):
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0}, timeout=10).close()
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- display -----
class Spinner:
    """A tiny status spinner. Redraws 10x a second, so the CPU cost is nil."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text):
        self.text = text
        self.thinking = False  # set once the model starts reasoning, so a pause isn't blamed on loading
        self.fell_back = False
        self._stop = threading.Event()
        self._thread = None

    def mark_thinking(self):
        self.thinking = True

    def mark_fallback(self):
        self.thinking = False
        self.text = "thinking took too long, answering directly"
        self.fell_back = True

    def start(self):
        if TTY:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        start, i = time.monotonic(), 0
        while not self._stop.wait(0.1):
            elapsed = time.monotonic() - start
            if self.thinking:
                text = "thinking it through"
            else:
                text = self.text if elapsed < 3 or self.fell_back else "loading the model (slower after a break)"
            sys.stdout.write(f"\r  {paint(self.FRAMES[i % len(self.FRAMES)], MAGENTA)} "
                             f"{paint(f'{text}… {elapsed:.0f}s', DIM)}\033[K")
            sys.stdout.flush()
            i += 1

    def stop(self):
        if self._thread and not self._stop.is_set():
            self._stop.set()
            self._thread.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def typewrite(text, delay=0.008):
    if not ANIMATE:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()


class Streamer:
    """Prints streamed reply text, indented, after a ✦ marker."""

    def __init__(self):
        self.started = False

    def write(self, text):
        if not text:
            return
        if not self.started:
            text = text.lstrip()
            if not text:
                return
            sys.stdout.write("\n  " + paint("✦ ", MAGENTA, BOLD))
            self.started = True
        sys.stdout.write(text.replace("\n", "\n    "))
        sys.stdout.flush()

    def end(self):
        if self.started:
            print()


def footer(seconds, load_seconds, final=None, thought=False):
    parts = [f"{seconds:.1f}s"]
    final = final or {}
    if final.get("eval_duration") and (final.get("eval_count") or 0) >= 5:
        parts.append(f"{final['eval_count'] / (final['eval_duration'] / 1e9):.0f} tok/s")
    if thought == "fallback" or final.get("think_fallback"):
        parts.append("thinking took too long, so it answered directly")
    elif thought:
        parts.append("thought it through first")
    if load_seconds > 1.5:
        parts.append(f"{load_seconds:.1f}s of that was loading the model")
    print(paint("    (" + " · ".join(parts) + ")", DIM))


def offline_message(err):
    reason = getattr(err, "reason", err)
    print(f"\n  {paint('Can’t reach the AI', RED, BOLD)} at {OLLAMA_URL} ({reason}).")
    print(paint("  Start it with: sudo systemctl start ollama", DIM))


# --------------------------------------------------------- machine facts -----
# Checked once per session and put in every prompt, so the model doesn't have to guess
# (it used to claim the NVIDIA driver was missing, or that the machine was offline).
NOTABLE_TOOLS = ["htop", "btop", "nvtop", "fastfetch", "neofetch", "tmux", "screen", "fzf", "bat", "jq",
                 "ripgrep:rg", "podman", "flatpak", "sensors", "dmidecode", "nmcli", "lspci", "lsusb",
                 "inxi", "curl", "wget", "git", "python3", "ollama", "sunshine"]
DMI = Path("/sys/class/dmi/id")


def read_text(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def os_release():
    for line in read_text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.system()


def cpu_model():
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return re.sub(r"\s+", " ", line.split(":", 1)[1]).strip()
    return platform.processor() or "unknown"


def cpu_topology():
    """'6 cores, 6 threads, no hyper-threading' from /proc/cpuinfo. Spelled out because,
    told only '6 threads', the model invented hyper-threading and 12 threads."""
    threads = os.cpu_count() or 0
    cores = set()
    physical = core = None
    for line in read_text("/proc/cpuinfo").splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "physical id":
            physical = value
        elif key == "core id":
            core = value
        elif not key and core is not None:
            cores.add((physical, core))
            physical = core = None
    if core is not None:
        cores.add((physical, core))
    if not cores or not threads:
        return f"{threads} threads" if threads else ""
    smt = "with hyper-threading" if threads > len(cores) else "no hyper-threading"
    return f"{len(cores)} cores, {threads} threads, {smt}"


def ram_total_gb():
    match = re.search(r"MemTotal:\s+(\d+)", read_text("/proc/meminfo"))
    return round(int(match.group(1)) * 1024 / 1e9, 1) if match else None


def run_quiet(argv, timeout=3):
    """Run a fixed, harmless command and return its stdout, or '' if anything goes wrong."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_facts():
    if shutil.which("nvidia-smi"):
        out = run_quiet(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                         "--format=csv,noheader,nounits"])
        gpus = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3 and parts[1].isdigit():
                gpus.append(f"{parts[0]}, {int(parts[1]) / 1024:.1f} GB VRAM. The NVIDIA driver "
                            f"{parts[2]} is installed and working (nvidia-smi works)")
        if gpus:
            return gpus
    out = run_quiet(["lspci"]) if shutil.which("lspci") else ""
    return [re.sub(r"^\S+\s+[^:]+:\s*", "", line) for line in out.splitlines()
            if re.search(r"VGA|3D controller|Display controller", line)]


def board():
    vendor, name = read_text(DMI / "board_vendor"), read_text(DMI / "board_name")
    text = " ".join(x for x in (vendor, name) if x)
    product = " ".join(x for x in (read_text(DMI / "sys_vendor"), read_text(DMI / "product_name"))
                       if x and "o.e.m" not in x.lower() and "to be filled" not in x.lower())
    if product and product != text:
        text = f"{text} (system: {product})" if text else product
    return text


def is_online(timeout=0.6):
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def installed_tools():
    have, missing = [], []
    for entry in NOTABLE_TOOLS:
        label, _, binary = entry.partition(":")
        (have if shutil.which(binary or label) else missing).append(label)
    return have, missing


def machine_facts():
    """Facts about this computer that don't change during a session."""
    have, missing = installed_tools()
    ram = ram_total_gb()
    return {
        "os": f"{os_release()}, kernel {platform.release()}",
        "cpu": f"{cpu_model()} ({cpu_topology()})",
        "ram": f"{ram} GB" if ram else "",
        "gpus": gpu_facts(),
        "board": board(),
        "online": is_online(),
        "installed": have,
        "not_installed": missing,
    }


def format_facts(facts):
    lines = ["Verified facts about this computer (checked when ai-shell started, so they are true;"
             " live values like usage, temperatures and free space still need a command):"]
    if facts.get("os"):
        lines.append(f"- OS: {facts['os']}")
    if facts.get("cpu"):
        lines.append(f"- CPU: {facts['cpu']}")
    if facts.get("ram"):
        lines.append(f"- RAM: {facts['ram']} total")
    for gpu in facts.get("gpus") or ["none detected"]:
        lines.append(f"- GPU: {gpu}")
    if facts.get("board"):
        lines.append(f"- Motherboard: {facts['board']}")
    if "online" in facts:
        lines.append("- Internet: " + ("this computer is online" if facts["online"]
                                       else "this computer couldn't reach the internet at startup"))
    if facts.get("installed"):
        lines.append("- Installed tools: " + ", ".join(facts["installed"]))
    if facts.get("not_installed"):
        lines.append("- Not installed (suggest installing them, don't run them): "
                     + ", ".join(facts["not_installed"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- banner -----
LOGO = [
    "▄▀█ █   ▄▄   █▀ █ █ █▀▀ █   █  ",
    "█▀█ █        ▄█ █▀█ ██▄ █▄▄ █▄▄",
]
GRADIENT = [51, 45, 39, 33, 63, 99, 135, 171, 207, 201]
EXAMPLES = ["how much storage do i have", "is sunshine running", "what's using my GPU",
            "install htop", "how hot is my GPU", "is my system up to date",
            "which process is using the most memory", "show my IP address"]


def gradient_line(text):
    if not TTY:
        return text
    width = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        colour = GRADIENT[int(i / width * (len(GRADIENT) - 1))]
        out.append(f"\033[1;38;5;{colour}m{ch}")
    return "".join(out) + "\033[0m"


def human_gb(n_bytes):
    return f"{n_bytes / 1e9:.0f} GB" if n_bytes >= 10e9 else f"{n_bytes / 1e9:.1f} GB"


def gather_stats(model):
    stats = {}
    try:
        version = get_json("/api/version").get("version", "?")
        loaded = next((m for m in get_json("/api/ps").get("models", [])
                       if m.get("name") in (model, f"{model}:latest")), None)
        if loaded:
            size, vram = loaded.get("size") or 0, loaded.get("size_vram") or 0
            where = "on GPU" if size and vram >= size else f"{round(100 * vram / size) if size else 0}% on GPU"
            stats["model"] = (f"{model} · {paint('ready', GREEN)}, loaded {where}", version)
        else:
            stats["model"] = (f"{model} · {paint('idle', YELLOW)}, loads on your first question", version)
    except Exception:  # noqa: BLE001
        stats["model"] = (f"{model} · {paint('offline', RED)} · run: sudo systemctl start ollama", None)

    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
            name, temp, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
            name = name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
            stats["gpu"] = f"{name} · {temp}°C · {int(used) / 1024:.1f} / {int(total) / 1024:.1f} GB VRAM in use"
        except Exception:  # noqa: BLE001
            pass

    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            mem[key] = int(value.split()[0]) * 1024
        used_ram = mem["MemTotal"] - mem["MemAvailable"]
        up = float(Path("/proc/uptime").read_text().split()[0])
        hours, minutes = int(up // 3600), int(up % 3600 // 60)
        stats["system"] = (f"load {os.getloadavg()[0]:.1f} ({os.cpu_count()} core{'s' if (os.cpu_count() or 1) != 1 else ''}) · "
                           f"RAM {used_ram / 1e9:.1f} / {mem['MemTotal'] / 1e9:.1f} GB · "
                           f"up {hours}h {minutes:02d}m")
    except Exception:  # noqa: BLE001
        pass

    drives = []
    try:
        home = shutil.disk_usage(Path.home())
        drives.append(f"home {human_gb(home.free)} free")
    except OSError:
        pass
    for base in (Path("/var/mnt"), Path("/run/media") / getpass.getuser()):
        try:
            for mount in sorted(base.iterdir()):
                if os.path.ismount(mount):
                    drives.append(f"{mount.name} {human_gb(shutil.disk_usage(mount).free)} free")
        except OSError:
            pass
    if drives:
        stats["storage"] = " · ".join(drives)

    try:
        today = datetime.now().date()
        total = today_count = ran = ran_ok = 0
        for line in LOG_FILE.read_text().splitlines():
            entry = json.loads(line)
            total += 1
            if datetime.fromisoformat(entry["time"]).astimezone().date() == today:
                today_count += 1
            if entry.get("decision") == "ran":
                ran += 1
                ran_ok += entry.get("exit_code") == 0
        if total:
            text = f"{total} questions so far · {today_count} today"
            if ran:
                text += f" · {round(100 * ran_ok / ran)}% of commands succeeded"
            stats["history"] = text
    except (OSError, ValueError, KeyError):
        pass
    return stats


def print_stats(model):
    stats = gather_stats(model)
    rows = [("Model", stats["model"][0]), ("GPU", stats.get("gpu")), ("System", stats.get("system")),
            ("Storage", stats.get("storage")), ("History", stats.get("history")),
            ("Learned", f"{len(load_memories())} things you've taught me · /learned to see them"
             if load_memories() else None)]
    for label, value in rows:
        if value:
            print(f"  {paint(label.ljust(9), DIM)} {value}")
    return stats


def banner(model):
    print()
    for line in LOGO:
        print("  " + gradient_line(line))
    print()
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    name = getpass.getuser().capitalize()
    sys.stdout.write("  ")
    typewrite(f"{greeting}, {name}. Ask me about this machine, or anything at all.")
    print()
    print_stats(model)
    tips = " · ".join(f"“{t}”" for t in random.sample(EXAMPLES, 3))
    print(f"\n  {paint('Try', DIM)}      {tips}")
    print(f"  {paint('Commands', DIM)} /help · /learned · /think · /facts · clear · exit")


HELP = """
  Just type what you want in plain English.

  · Questions about this machine run a read-only command straight away and
    explain the result. Anything that changes the system asks first and says why.
  · General questions get a plain answer.
  · Follow-ups work: "now sort that by size", "restart it".

  Teach me things and I'll remember them for good:
    remember that "clear" means clear the screen
    from now on, show sizes in GB
    when I say "games", I mean the folder /mnt/gamedrive
  If you fix a command with e, I'll offer to remember the fix.

  Paste multi-line output (e.g. an nvidia-smi table) straight in, or wrap
  it in triple quotes: start with \"\"\" and end with \"\"\".

  clear       clear the screen
  /learned    list everything I've learned     /unlearn <n>   forget one
  /forget     forget this conversation          /raw           show or hide raw output
  /think      on · auto · off (on by default: reasons first, up to a budget, then answers directly)
  /facts      show what I know about this machine
  /stats      show the system stats again       exit           quit and free the GPU
"""


# --------------------------------------------------------------- session -----
class Session:
    def __init__(self, model, keep_alive, think=DEFAULT_THINK):
        self.model = model
        self.keep_alive = keep_alive
        self.think = think if think in THINK_MODES else "auto"
        self.history = []
        self.show_output = True
        self.used_model = False
        self._facts = None

    @property
    def facts(self):
        """Machine facts as prompt text, gathered on first use."""
        if self._facts is None:
            self._facts = format_facts(machine_facts())
        return self._facts

    def remember(self, request, reply):
        self.history += [{"role": "user", "content": request},
                         {"role": "assistant", "content": reply}]
        self.history = self.history[-HISTORY_MESSAGES:]


def load_memories():
    try:
        data = json.loads(MEMORY_FILE.read_text())
        return [m for m in data if isinstance(m, str)]
    except (OSError, ValueError):
        return []


def save_memories(memories):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(memories[-MAX_MEMORIES:], indent=2))


def add_memory(text):
    text = text.strip().rstrip(".")
    memories = [m for m in load_memories() if m.lower() != text.lower()]
    memories.append(text)
    save_memories(memories)
    print(f"\n  {paint('✓ Learned:', GREEN, BOLD)} {text}")
    print(paint("    I'll follow this from now on. See everything I've learned with /learned.", DIM))


def memories_for_prompt():
    memories = load_memories()
    return "\n".join(f"- {m}" for m in memories) if memories else "(nothing yet)"


TEACH_PREFIXES = ("remember that ", "remember: ", "remember ", "from now on ", "from now on, ",
                  "when i say ", "when i ask ")


def teaching(request):
    """If the user is teaching a rule, return the rule text to store."""
    low = request.lower()
    for prefix in TEACH_PREFIXES:
        if low.startswith(prefix):
            rule = request[len(prefix):].strip() if prefix.startswith("remember") else request.strip()
            return rule[0].upper() + rule[1:] if rule else None
    return None


def offer_to_learn(request, cmd):
    if "\n" in cmd:
        return
    try:
        answer = input(f"  {paint('Remember this fix for next time?', MAGENTA)} y/N: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer in ("y", "yes"):
        add_memory(f'When I ask "{request}", run: {cmd}')


def show_memories():
    memories = load_memories()
    if not memories:
        print(paint('\n  Nothing learned yet. Teach me with: remember that "clear" means clear the screen', DIM))
        return
    print()
    for i, m in enumerate(memories, 1):
        print(f"  {paint(str(i).rjust(2), DIM)}  {m}")
    print(paint("\n  Remove one with /unlearn <number>, or everything with /unlearn all.", DIM))


def unlearn(arg):
    memories = load_memories()
    if arg == "all":
        save_memories([])
        print(paint(f"  Forgot all {len(memories)} things I'd learned.", DIM))
    elif arg.isdigit() and 1 <= int(arg) <= len(memories):
        removed = memories.pop(int(arg) - 1)
        save_memories(memories)
        print(paint(f"  Forgot: {removed}", DIM))
    else:
        print(paint("  Use /unlearn <number> (see /learned) or /unlearn all.", DIM))


def context_line():
    return (f"user={getpass.getuser()}, host={socket.gethostname()}, "
            f"cwd={os.getcwd()}, home={Path.home()}")


def log(entry):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def looks_like_command(line):
    line = line.strip()
    if not line or line[-1] in ".!?" or line[0].isupper():
        return False
    try:
        word = shlex.split(line)[0]
    except (ValueError, IndexError):
        return False
    return shutil.which(word) is not None or word in READ_ONLY_SUBCOMMANDS


def decide(buffer, finished):
    """Work out from the start of the reply whether it's a command or an answer."""
    text = buffer.lstrip()
    if not text:
        return "answer" if finished else None
    if text[0] in "$`":
        return "command"
    if "\n" in text or len(text) >= 80 or finished:
        one_line = "\n" not in text.strip()
        return "command" if one_line and looks_like_command(text) else "answer"
    return None


SELF_EXPLANATORY = {"echo", "printf", "date", "pwd", "whoami", "hostname", "uptime", "cal", "nproc"}


HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def clean_command(text):
    """Extract the command. Normally one line; a heredoc keeps its body and terminator."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```[\w-]*", "", text)
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""
    first = lines[0].strip()
    if first.startswith("$"):
        first = first[1:].strip()
    if len(first) > 1 and first[0] == "`" and first[-1] == "`":
        first = first.strip("`").strip()
    match = HEREDOC_RE.search(first)
    if not match:
        return first
    end, body = match.group(1), []
    for line in lines[1:]:
        if line.strip() == end:
            break
        body.append(line.rstrip())
    return "\n".join([first, *body, end])


SUDO_USER_RE = re.compile(r"(^|&&|\|\||;)(\s*)sudo\s+(?:-\S+\s+)*((?:systemctl|journalctl)\b[^|;&\n]*?\s--user\b)")


SUDO_QUERY_RE = re.compile(
    r"(^|&&|\|\||;|\|)(\s*)sudo\s+(?:-\S+\s+)*("
    r"(?:dnf5?)\s+(?:-\S+\s+)*(?:list|info|search|repolist|repoquery|provides|check-update|check-upgrade)\b"
    r"|rpm\s+-q|flatpak\s+(?:list|info|search|remotes|remote-ls|history)\b)")


def normalise_command(cmd):
    """Fix mistakes that can't work or would get stuck. Returns (command, list of fixes made).

    - sudo + --user: 'sudo systemctl --user restart sunshine' talks to root's user
      services, not yours, and fails. Both 4B and 9B made this mistake in the eval.
    - sudo on a package query ('sudo dnf search obs'): root isn't needed, and because
      the query is read-only it would auto-run and then sit waiting for a password.
    """
    fixes = []
    head, sep, body = cmd.partition("\n") if HEREDOC_RE.search(cmd.split("\n", 1)[0]) else (cmd, "", "")
    fixed = SUDO_USER_RE.sub(r"\1\2\3", head)
    if fixed != head:
        fixes.append("dropped sudo, because --user services belong to you, not root")
    queried = SUDO_QUERY_RE.sub(r"\1\2\3", fixed)
    if queried != fixed:
        fixes.append("dropped sudo, because looking up packages doesn't need root")
    return queried + sep + body, fixes


OVERWRITE_RE = re.compile(r"(?<![>&\d])\d?>(?![>&])\s*([^\s;&|<>]+)")


def overwritten_files(cmd):
    """Existing, non-empty files a command would replace with a single '>'.

    The thinking model answered "add a line to my .bashrc" with cat > ~/.bashrc << EOF
    every time, which replaces the whole file. That deserves a typed 'yes' and a warning.
    """
    head = command_head(cmd)
    masked = mask_quoted(head)
    hits = []
    for match in OVERWRITE_RE.finditer(masked):
        target = head[match.start(1):match.end(1)].strip("'\"")
        if target.startswith("/dev/"):
            continue
        path = Path(os.path.expandvars(os.path.expanduser(target)))
        try:
            if path.is_file() and path.stat().st_size > 0:
                hits.append(target)
        except OSError:
            continue
    return hits


def line_count(target):
    try:
        with open(os.path.expanduser(target), "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def fix_path_case(cmd):
    """If a path in the command doesn't exist only because of its case (~/downloads for
    ~/Downloads), return the command with the real path; otherwise None. No model needed."""
    try:
        words = shlex.split(command_head(cmd))
    except ValueError:
        return None
    fixed = cmd
    for word in words:
        if "/" not in word or word.startswith("-"):
            continue
        expanded = os.path.expanduser(word)
        if os.path.exists(expanded):
            continue
        real = _case_insensitive(expanded)
        if real and real != expanded:
            home = str(Path.home())
            shown = "~" + real[len(home):] if word.startswith("~") and real.startswith(home) else real
            fixed = fixed.replace(word, shown, 1)
    return fixed if fixed != cmd else None


def _case_insensitive(path):
    parts = Path(path).parts
    if not parts or not os.path.isabs(path):
        return None
    current = Path(parts[0])
    for part in parts[1:]:
        candidate = current / part
        if not candidate.exists():
            try:
                matches = [p for p in current.iterdir() if p.name.lower() == part.lower()]
            except OSError:
                return None
            if len(matches) != 1:
                return None
            candidate = matches[0]
        current = candidate
    return str(current)


EMBEDDED_RE = re.compile(r"^\s*\$\s+(\S.*)$", re.M)


def embedded_command(text):
    """A reply that's prose plus one '$ command' line: return that command, else None.

    The model sometimes answers "Run this: $ sudo systemctl enable --now sshd" instead of
    replying with just the command, and then nothing ran. Exactly one such line is needed,
    so a list of options isn't mistaken for a single action.
    """
    lines = EMBEDDED_RE.findall(re.sub(r"```[\w-]*", "", text))
    if len(lines) != 1:
        return None
    cmd = lines[0].strip().strip("`").strip()
    return cmd if cmd and len(cmd) <= MAX_COMMAND_CHARS and not HEREDOC_RE.search(cmd) else None


def command_head(cmd):
    """The part of a command that actually executes (a heredoc's body is just text)."""
    return cmd.split("\n", 1)[0] if HEREDOC_RE.search(cmd.split("\n", 1)[0]) else cmd


# ------------------------------------------------ checks around running ------
# Words that mean the model has to reason, not just recall a command.
DIAGNOSTIC_RE = re.compile(
    r"\b(why|diagnos\w*|troubleshoot\w*|investigat\w*|debug\w*|inspect|figure out|find out|work out|"
    r"root cause|reason for|what'?s wrong|what is wrong|not working|isn'?t working|broken|"
    r"difference between|compare|should i|can i run)\b", re.I)


def should_think(mode, request):
    return mode == "on" or (mode == "auto" and bool(DIAGNOSTIC_RE.search(request)))


SHELL_WORDS = {"cd", "echo", "printf", "type", "test", "[", "[[", "true", "false", "export", "set", "unset",
               "alias", "source", ".", "exit", "read", "command", "history", "pwd", "ulimit", "umask",
               "wait", "jobs", "fg", "bg", "time", "exec", "eval", "let", "declare", "local", "for",
               "while", "until", "if", "then", "else", "elif", "fi", "do", "done", "case", "esac",
               "shift", "trap", "!", "{", "}", "function", "select", "builtin", "hash", "kill"}


def missing_programs(cmd):
    """Programs a command would run that aren't installed (catches invented ones like 'bless -t')."""
    missing = []
    for segment in split_segments(command_head(cmd)):
        try:
            tokens = tokens_for(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        name = tokens[0]
        if name in SHELL_WORDS or name[0] in "({$" or "=" in name:
            continue
        if "/" in name:
            found = os.access(os.path.expanduser(name), os.X_OK)
        else:
            found = shutil.which(name) is not None
        if not found and name not in missing:
            missing.append(name)
    return missing


SHORT_HEAD_RE = re.compile(r"\|\s*head\s+(?:-n\s*|-)?([12])\s*$")


def without_short_head(cmd, output):
    """If '| head -1' (or -2) left only column headings, return the command without it.

    The model likes to add '| head -1', which on most tabular output keeps just the
    header row. It then read that as "no GPU detected". Re-reading the full output fixes it.
    """
    match = SHORT_HEAD_RE.search(mask_quoted(cmd))
    if not match:
        return None
    kept = [ln for ln in output.splitlines() if ln.strip()]
    if len(kept) > int(match.group(1)) or any(ch.isdigit() for ln in kept for ch in ln):
        return None  # real data came through, so head did its job
    return cmd[:match.start()].rstrip()


USAGE_ERROR_RE = re.compile(
    r"not a valid field|is not valid|not recognized|unrecognized option|unrecognised option|"
    r"invalid option|unknown option|illegal option|invalid argument|unexpected argument|unknown command|"
    r"option requires an argument|invalid field|^usage:", re.I | re.M)


def is_usage_error(code, output):
    return code not in (0, 130) and bool(USAGE_ERROR_RE.search(output))


def help_text(cmd):
    """Help for the failing program, so the model can pick real options. Only runs --help
    for programs on the read-only list, and never through a shell."""
    prog = first_program(cmd)
    if prog == "nvidia-smi":
        for query, helper in (("--query-gpu", "--help-query-gpu"),
                              ("--query-compute-apps", "--help-query-compute-apps")):
            if query in cmd:
                fields = re.findall(r'^"([\w.]+)"', run_quiet(["nvidia-smi", helper]), re.M)
                if fields:
                    return f"Valid {query} fields: " + ", ".join(fields)
    if prog in READ_ONLY_COMMANDS or prog in READ_ONLY_SUBCOMMANDS or prog in ("ip", "rpm"):
        try:
            done = subprocess.run([prog, "--help"], capture_output=True, text=True, timeout=3,
                                  stdin=subprocess.DEVNULL)
            return strip_ansi(done.stdout + done.stderr)[:HELP_TEXT_CHARS]
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def show_command(cmd, note=""):
    lines = cmd.split("\n")
    print(f"\n  {paint('$', DIM)} {paint(lines[0], BOLD, CYAN)}" + (paint(note, DIM) if note else ""))
    body = lines[1:]
    for line in body[:12]:
        print(paint(f"    {line}", DIM))
    if len(body) > 12:
        print(paint(f"    … {len(body) - 12} more lines", DIM))


def edit(cmd):
    if readline:
        readline.set_startup_hook(lambda: readline.insert_text(cmd))
    try:
        return input("  edit> ").strip()
    finally:
        if readline:
            readline.set_startup_hook()


def explain(session, cmd):
    spinner = Spinner("working out what this does").start()
    try:
        head = command_head(cmd)
        if head != cmd:
            head += f"   (followed by {cmd.count(chr(10)) - 1} lines of file content)"
        text, _ = chat(session.model, [{"role": "system", "content": EXPLAIN_PROMPT},
                                       {"role": "user", "content": head}],
                       session.keep_alive, 60)
        text = strip_ansi(text).strip().splitlines()[0].strip() if text.strip() else ""
    except Exception:  # noqa: BLE001
        text = ""
    finally:
        spinner.stop()
    return text or "It isn't on the read-only list, so it could change something."


def run_in_terminal(cmd):
    """Run attached to the real terminal, so progress bars, sudo and [Y/n] prompts work
    normally, while script(1) records the output for the summary."""
    import tempfile
    fd, log_path = tempfile.mkstemp(prefix="ai-shell-", suffix=".log")
    os.close(fd)
    print()
    try:
        code = subprocess.run(["script", "-q", "-e", "-f", "-c", cmd, log_path],
                              env={**os.environ, "SHELL": "/bin/bash"}).returncode
    except KeyboardInterrupt:
        code = 130
    try:
        raw = Path(log_path).read_text(errors="replace")
    finally:
        Path(log_path).unlink(missing_ok=True)
    lines = []
    for line in strip_ansi(raw).replace("\r\n", "\n").split("\n"):
        line = line.split("\r")[-1]  # keep only the final state of progress bars
        if not line.startswith(("Script started", "Script done")):
            lines.append(line)
    return code, "\n".join(lines).strip()


@contextlib.contextmanager
def child_owns_ctrl_c():
    """While a command runs, let Ctrl+C go to the command only, like a real shell does.

    The child is in the terminal's foreground process group, so it receives SIGINT
    itself and decides what to do. If ai-shell also reacted, a KeyboardInterrupt
    could land anywhere (e.g. inside proc.wait()) and crash the whole session.
    Must be entered *after* the child has started, otherwise the child would
    inherit SIG_IGN and Ctrl+C would stop working for it too.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def exit_code(returncode):
    """Normalise Popen's 'killed by signal N' (-N) to the shell convention (128+N)."""
    return 128 - returncode if returncode < 0 else returncode


def run_command(cmd, show_output, cap_lines, allow_input=True):
    """Run with live output; returns (exit_code, captured_output, was_interactive)."""
    if allow_input and shutil.which("script") and first_program(cmd) not in INTERACTIVE:
        code, output = run_in_terminal(cmd)
        return code, output, False
    if first_program(cmd) in INTERACTIVE:
        print()
        proc = subprocess.Popen(cmd, shell=True, executable="/bin/bash")
        with child_owns_ctrl_c():
            code = exit_code(proc.wait())
        return code, "", True

    proc = subprocess.Popen(cmd, shell=True, executable="/bin/bash",
                            stdin=None if allow_input else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks, shown, hidden, line_start = [], 0, 0, True
    if show_output:
        print()
    with child_owns_ctrl_c():
        while True:
            data = os.read(proc.stdout.fileno(), 4096)
            if not data:
                break
            chunks.append(data)
            if not show_output:
                continue
            for part in data.decode(errors="replace").splitlines(keepends=True):
                ends_line = part.endswith("\n")
                if cap_lines and shown >= OUTPUT_PREVIEW_LINES:
                    hidden += ends_line
                    continue
                if line_start:
                    sys.stdout.write("    ")
                sys.stdout.write(paint(part.rstrip("\n"), DIM) + ("\n" if ends_line else ""))
                line_start = ends_line
                shown += ends_line
            sys.stdout.flush()
        code = exit_code(proc.wait())
    if show_output and not line_start:
        print()
    if hidden:
        print(paint(f"    … {hidden} more lines (the answer below covers all of it)", DIM))
    return code, strip_ansi(b"".join(chunks).decode(errors="replace")), False


def summarise(session, request, cmd, code, output, note=""):
    if len(output) > SUMMARY_INPUT_CHARS:
        half = SUMMARY_INPUT_CHARS // 2
        output = output[:half] + "\n...[output trimmed]...\n" + output[-half:]
    system = SUMMARY_PROMPT.format(facts=session.facts, fedora_rules=FEDORA_RULES)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Question: {request}\nCommand: {cmd}\nExit code: {code}\n"
                                    + (f"Note: {note}\n" if note else "")
                                    + f"Output:\n{output.strip() or '(no output)'}"},
    ]
    spinner = Spinner("reading the output").start()
    out, text, final = Streamer(), "", {}
    try:
        for piece, done in chat_stream(session.model, messages, session.keep_alive, 200):
            final = done or final
            if piece and not out.started:
                spinner.stop()
            text += piece
            out.write(piece)
    except Exception as e:  # noqa: BLE001
        spinner.stop()
        print(paint(f"\n  (couldn't summarise the output: {e})", DIM))
    finally:
        spinner.stop()
        out.end()
    return text.strip(), final


def router_messages(session, request):
    system = ROUTER_PROMPT.format(context=context_line(), model=session.model, memories=memories_for_prompt(),
                                  facts=session.facts, fedora_rules=FEDORA_RULES)
    return [{"role": "system", "content": system}, *session.history, {"role": "user", "content": request}]


def fix_command(session, request, cmd, output):
    """Ask the model for a corrected command after a usage error, with the help text in hand.

    Returns the new command, or None if it couldn't come up with a different one.
    """
    helptext = help_text(cmd)
    error = output.strip()[-1500:] or "(no output)"
    messages = [
        {"role": "system", "content": FIX_PROMPT.format(facts=session.facts)},
        {"role": "user", "content": f"Question: {request}\nFailed command: {cmd}\nError:\n{error}\n\n"
                                    f"Help text:\n{helptext or '(no help text available)'}"},
    ]
    think = session.think != "off"  # fixing a mistake is exactly where reasoning pays off
    spinner = Spinner("checking the help and fixing the command").start()
    if think:
        spinner.mark_thinking()
    try:
        text, _ = chat(session.model, messages, session.keep_alive, 200, think=think)
    except Exception:  # noqa: BLE001
        return None
    finally:
        spinner.stop()
    if decide(text, True) != "command":
        return None
    fixed, _ = normalise_command(clean_command(text))
    if not fixed or fixed == cmd or "\n" in fixed or len(fixed) > MAX_COMMAND_CHARS:
        return None
    return fixed


def handle(session, request):
    rule = teaching(request)
    if rule:
        add_memory(rule)
        log({"model": session.model, "request": request, "decision": "learned"})
        return
    start = time.monotonic()
    think = should_think(session.think, request)
    spinner = Spinner("thinking").start()
    buffer, mode, final, out = "", None, {}, Streamer()
    try:
        for piece, done in chat_stream(session.model, router_messages(session, request), session.keep_alive,
                                       MAX_REPLY_TOKENS, think=think, on_thinking=spinner.mark_thinking,
                                       on_fallback=spinner.mark_fallback):
            buffer += piece
            final = done or final
            if mode is None:
                mode = decide(buffer, done is not None)
                if mode == "answer":
                    spinner.stop()
                    out.write(buffer)
            elif mode == "answer":
                out.write(piece)
    except KeyboardInterrupt:
        spinner.stop()
        print(paint("\n  Stopped.", DIM))
        return
    except urllib.error.URLError as e:
        spinner.stop()
        offline_message(e)
        return
    except (OllamaError, OSError, ValueError) as e:
        spinner.stop()
        print(f"\n  {paint('The model returned an error:', RED)} {e}")
        return
    finally:
        spinner.stop()

    session.used_model = True
    load_seconds = (final.get("load_duration") or 0) / 1e9
    mode = mode or decide(buffer, True)
    if final.get("think_fallback"):
        think = "fallback"

    if mode == "answer":
        out.end()
        if not out.started:
            print(paint("\n  (no reply — try rephrasing)", DIM))
        footer(time.monotonic() - start, load_seconds, final, thought=think)
        session.remember(request, buffer.strip())
        log({"model": session.model, "request": request, "decision": "answered", "think": think})
        offered = embedded_command(buffer)
        if offered and sys.stdin.isatty():
            offer_embedded(session, request, offered, think)
        return

    cmd = clean_command(buffer)
    entry = {"model": session.model, "request": request, "command": cmd, "think": think}
    limit = MAX_SCRIPT_CHARS if "\n" in cmd else MAX_COMMAND_CHARS
    if not cmd or final.get("done_reason") == "length" or len(cmd) > limit:
        print(f"\n  {paint('The model got stuck', RED, BOLD)} and produced a runaway reply, "
              f"so I didn't run anything. Try rephrasing.")
        footer(time.monotonic() - start, load_seconds)
        log({**entry, "command": cmd[:200], "decision": "runaway"})
        return
    cmd, fixes = normalise_command(cmd)
    if fixes:
        entry.update(command=cmd, fixed=fixes, model_command=entry["command"])
        print(paint(f"\n  (fixed the model's command: {'; '.join(fixes)})", DIM))
    execute(session, request, cmd, entry, start, load_seconds, thought=think)


def offer_embedded(session, request, cmd, think):
    """The answer included one command: offer to run it, through the usual safety checks."""
    cmd, fixes = normalise_command(cmd)
    try:
        answer = input(f"\n  {paint('Run that command?', MAGENTA)} {paint(cmd, CYAN)}  y/N: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer not in ("y", "yes"):
        return
    for fix in fixes:
        print(paint(f"  (fixed: {fix})", DIM))
    entry = {"model": session.model, "request": request, "command": cmd, "think": think, "offered": True}
    # Read-only commands run straight away (you just said yes); anything else still asks.
    execute(session, request, cmd, entry, time.monotonic(), 0.0)


def approve(session, request, cmd, entry):
    """Safety check, installed-program check and permission prompt.

    Returns (cmd, verdict) to run (cmd may have been edited), or None if nothing should run.
    """
    while True:
        verdict, reasons, strong = assess(cmd)
        show_command(cmd, "   read-only, running it" if verdict == "read_only" else "")

        if verdict == "blocked":
            print(f"  {paint('Blocked:', RED, BOLD)} this command {reasons[0]}. I won't run it.")
            session.remember(request, f"$ {cmd}\n(blocked: {reasons[0]})")
            log({**entry, "command": cmd, "decision": "blocked", "reasons": reasons})
            return None
        missing = missing_programs(cmd)
        if missing:
            names = ", ".join(missing)
            print(f"\n  {paint('Not run:', YELLOW, BOLD)} {names} {'is' if len(missing) == 1 else 'are'} "
                  f"not installed on this machine.")
            print(paint(f"  If it's a real package, try: sudo dnf install {missing[0]}  "
                        f"(or ask me for another way)", DIM))
            session.remember(request, f"$ {cmd}\n(not run: {names} not installed)")
            log({**entry, "command": cmd, "decision": "missing", "missing": missing})
            return None
        if verdict == "read_only":
            return cmd, verdict
        replaced = overwritten_files(cmd)
        if replaced:
            strong = True
            reasons = [f"REPLACES the whole of {t} ({line_count(t)} lines now)" for t in replaced] + reasons
            print(f"\n  {paint('Careful:', RED, BOLD)} this overwrites {', '.join(replaced)} instead of adding to it."
                  + paint("  (to add a line, use >> instead of >)", DIM))

        print(f"\n  {paint('Needs your permission:', YELLOW, BOLD)} {explain(session, cmd)}")
        print(paint(f"  ({' · '.join(reasons)})", DIM))
        how = "type 'yes' to run it" if strong else "y to run it"
        try:
            answer = input(f"  {how}, e to edit, Enter to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer == "e":
            new = edit(cmd)
            if new:
                entry["edited"] = True
                cmd = new
            continue
        if (answer == "yes") if strong else (answer in ("y", "yes")):
            return cmd, verdict
        print(paint("  Cancelled, nothing was run.", DIM))
        session.remember(request, f"$ {cmd}\n(user cancelled)")
        log({**entry, "command": cmd, "decision": "cancelled"})
        return None


def execute(session, request, cmd, entry, start, load_seconds, thought=False, attempt=0):
    approved = approve(session, request, cmd, entry)
    if approved is None:
        footer(time.monotonic() - start, load_seconds, thought=thought)
        return
    cmd, verdict = approved

    show = session.show_output or verdict != "read_only"
    code, output, interactive = run_command(cmd, show, cap_lines=verdict == "read_only",
                                            allow_input=verdict != "read_only")

    # A wrong option or field name: give the model the real help text and one more go.
    if not interactive and attempt == 0 and is_usage_error(code, output):
        log({**entry, "command": cmd, "decision": "ran", "auto": verdict == "read_only",
             "exit_code": code, "usage_error": True})
        fixed = fix_command(session, request, cmd, output)
        if fixed:
            print(paint("\n  ↻ That was a usage error, so I checked the help text and tried again:", DIM))
            execute(session, request, fixed, {**entry, "command": fixed, "retry_of": cmd,
                                               "think": session.think != "off"},
                    start, load_seconds, thought=True, attempt=1)
            return

    # A path that's only wrong by case (~/downloads): fix it and run once more, no model needed.
    if not interactive and attempt == 0 and code != 0 and "No such file or directory" in output:
        fixed = fix_path_case(cmd)
        if fixed:
            log({**entry, "command": cmd, "decision": "ran", "auto": verdict == "read_only",
                 "exit_code": code, "path_case": True})
            print(paint(f"\n  ↻ That path doesn't exist, but one with different capitals does, so trying: {fixed}", DIM))
            execute(session, request, fixed, {**entry, "command": fixed, "retry_of": cmd},
                    start, load_seconds, thought=thought, attempt=1)
            return

    note = ""
    if verdict == "read_only" and code == 0 and not interactive:
        fuller = without_short_head(cmd, output)
        if fuller and assess(fuller)[0] == "read_only":
            _, full_output, _ = run_command(fuller, False, cap_lines=True, allow_input=False)
            if full_output.strip():
                output = full_output
                note = "the command's head kept only the column headings, so this is the full output instead"
                print(paint("    (head cut that down to just the headings, so I read the full output)", DIM))

    summary, summary_stats = "", {}
    meaningful = [ln for ln in output.splitlines() if ln.strip()]
    if interactive:
        if code not in (0, 130):
            print(paint(f"  (exit code {code})", DIM))
    elif code == 130:
        print(paint("\n  Stopped (Ctrl+C), so there's nothing to report.", DIM))
    elif code == 0 and not meaningful:
        print(f"\n  {paint('✓', GREEN, BOLD)} Done.")
    elif code == 0 and first_program(cmd) in SELF_EXPLANATORY and len(meaningful) <= 3:
        if not show:
            print("\n" + "\n".join(f"    {ln}" for ln in meaningful))
    else:
        summary, summary_stats = summarise(session, request, cmd, code, output, note)
    if entry.get("edited") and code == 0:
        offer_to_learn(request, cmd)
    footer(time.monotonic() - start, load_seconds, summary_stats, thought=thought)
    session.remember(request, f"$ {cmd}\n{summary}".strip())
    log({**entry, "command": cmd, "decision": "ran", "auto": verdict == "read_only",
         "exit_code": code, "seconds": round(time.monotonic() - start, 2)})


# ------------------------------------------------------------------ main -----
def stdin_has_more(timeout=PASTE_WINDOW):
    try:
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


def read_request(prompt):
    """Read one request, keeping a multi-line paste together as one request.

    - Bracketed paste (most terminals): readline hands the whole paste back at once.
    - Terminals without it send each line as if Enter was pressed; lines that are
      already waiting a few milliseconds later are part of the same paste.
    - Or wrap it by hand: start with \"\"\" and end with \"\"\".
    """
    before = readline.get_current_history_length() if readline else 0
    first = input(prompt)
    if first.lstrip().startswith('"""'):
        lines = [first.lstrip()[3:]]
        while not lines[-1].rstrip().endswith('"""'):
            lines.append(input(paint("  … ", DIM) if TTY else "... "))
        lines[-1] = lines[-1].rstrip()[:-3]
    else:
        lines = [first]
        while sys.stdin.isatty() and stdin_has_more():
            try:
                lines.append(input())
            except EOFError:
                break
    text = "\n".join(lines).strip()
    if "\n" in text and readline:
        # One history entry for the whole paste, not one per line.
        try:
            while readline.get_current_history_length() > before:
                readline.remove_history_item(readline.get_current_history_length() - 1)
            readline.add_history(" ".join(text.split())[:500])
        except (ValueError, IndexError):
            pass
    return text


def set_think(session, arg):
    if arg in THINK_MODES:
        session.think = arg
    elif arg:
        print(paint("  Use /think off, /think auto or /think on.", DIM))
        return
    detail = {"off": "never reason first (fastest, least accurate)",
              "auto": "reason first for 'why…', troubleshooting and comparison questions, and when fixing a command",
              "on": f"always reason first, for up to {THINK_BUDGET} tokens, then answer directly (most accurate)"
              }[session.think]
    print(paint(f"  Thinking is {session.think}: {detail}.", DIM))


def interactive(session, show_banner):
    if show_banner:
        banner(session.model)
    if readline:
        try:
            readline.read_history_file(READLINE_FILE)
        except OSError:
            pass
        with contextlib.suppress(Exception):
            # Python turns this off by default; on, a pasted block arrives as one line.
            readline.parse_and_bind("set enable-bracketed-paste on")
    prompt = (paint("\nai› ", MAGENTA, BOLD)) if TTY else "\nai> "
    try:
        while True:
            try:
                request = read_request(prompt)
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break
            if not request:
                continue
            command = request.lower()
            if command in ("exit", "quit", "/exit", "/quit"):
                break
            if command == "/help":
                print(HELP)
            elif command == "/raw":
                session.show_output = not session.show_output
                print(paint(f"  Raw command output is now {'shown' if session.show_output else 'hidden'}.", DIM))
            elif command in ("clear", "cls", "/clear"):
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
            elif command == "/forget":
                session.history.clear()
                print(paint("  Forgot the conversation so far (things I've learned are kept).", DIM))
            elif command in ("/learned", "/memory", "what have you learned", "what have you learnt"):
                show_memories()
            elif command.startswith("/unlearn"):
                unlearn(command[len("/unlearn"):].strip())
            elif command.startswith("/remember "):
                add_memory(request[len("/remember "):])
            elif command == "/stats":
                print()
                print_stats(session.model)
            elif command.startswith("/think"):
                set_think(session, command[len("/think"):].strip())
            elif command == "/facts":
                print("\n" + "\n".join(f"  {line}" for line in session.facts.splitlines()))
            else:
                try:
                    handle(session, request)
                except KeyboardInterrupt:
                    # Ctrl+C mid-request (e.g. while explaining or summarising)
                    # cancels that request, not the whole session.
                    print(paint("\n  Stopped.", DIM))
    finally:
        if readline:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                readline.set_history_length(500)
                readline.write_history_file(READLINE_FILE)
            except OSError:
                pass
        if session.used_model and unload(session.model):
            print(paint("  Model unloaded, so the GPU is free for games. Bye!", DIM))


def main():
    parser = argparse.ArgumentParser(description="Plain-English terminal assistant for Fedora.")
    parser.add_argument("request", nargs="*", help="ask once and exit")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default {DEFAULT_MODEL})")
    parser.add_argument("--think", choices=THINK_MODES, default=DEFAULT_THINK if DEFAULT_THINK in THINK_MODES
                        else "auto", help="reason before answering: off, auto (default) or on")
    parser.add_argument("--no-banner", action="store_true", help="skip the welcome screen")
    parser.add_argument("--version", action="version", version=f"ai-shell {VERSION}")
    args = parser.parse_args()

    if args.request:
        try:
            handle(Session(args.model, keep_alive="2m", think=args.think), " ".join(args.request))
        except KeyboardInterrupt:
            print(paint("\n  Stopped.", DIM))
            sys.exit(130)
    else:
        interactive(Session(args.model, keep_alive="10m", think=args.think), not args.no_banner)


if __name__ == "__main__":
    main()
