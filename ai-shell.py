#!/usr/bin/env python3
"""
ai-shell: a terminal assistant for Bazzite, backed by a local Ollama model.

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
Standard library only, so it runs on the Bazzite host with no pip installs.
Set AI_SHELL_ANIMATE=0 to turn off the welcome animation.
"""

import argparse
import getpass
import json
import os
import random
import re
import shlex
import shutil
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
HISTORY_MESSAGES = 6         # remembered messages for follow-up questions

# ------------------------------------------------------------------ prompts --
ROUTER_PROMPT = """You are ai-shell, a friendly assistant living in a terminal on Bazzite, an immutable Fedora Atomic-based gaming distro.

Decide what the user needs:
- If answering needs information from this computer, or they want something done on it, reply with ONE line: "$ " followed by a single shell command. Nothing else.
- Never guess facts about this computer, the current date or the time: always use a command (e.g. $ date).
- If it is a general question that isn't about this machine, answer in plain English in 1-4 short sentences. No markdown.

About you: you are the {model} model, running entirely on this computer's own GPU through Ollama. You need no internet connection and nothing leaves the machine. You usually write around 60-70 tokens per second; the first reply after a break takes a few seconds longer while the model loads. You are far smaller and less capable than big cloud chatbots like Claude or ChatGPT, but you are private, free and work offline. Each reply shows its timing underneath, so for speed questions point to that rather than running a test.

Command rules:
- Do exactly what was asked; don't chain extra commands or add unneeded flags.
- Never use commands that wait for keyboard input, such as cat with no file.
- To write a file, use a heredoc: the first line is $ cat > /path/to/file << 'EOF' then the file's
  content on the following lines, then EOF alone on the last line. This is the only time you may
  reply with more than one line.
- The base system is read-only: never use dnf, yum or apt.
- Install GUI apps with Flatpak from Flathub, e.g. $ flatpak install flathub org.mozilla.firefox
  If unsure of an app's exact Flathub ID, use the plain name, e.g. $ flatpak install flathub discord
- Update the whole system with: $ ujust update
- Use distrobox for command-line tools that aren't in the base image. Containers use podman, not docker.
- Sunshine runs as a systemd user service named exactly "sunshine": manage it with systemctl --user <action> sunshine
- Logs come from journalctl; there is no /var/log/syslog.
- Only use sudo when actually required.

Things the user has taught you. Always follow these; they override everything above:
{memories}

Context: {context}"""

SUMMARY_PROMPT = """You are ai-shell on Bazzite Linux. You ran a command for the user; now explain the result.
Answer their original question directly in plain English, in 1-3 short sentences, using specific numbers and names from the output. No markdown, and don't restate the command.
If the output already answers the question, just say it naturally in one short sentence.
If the command failed, say what went wrong and suggest a fix in one sentence.
If the output is empty, say so plainly; never claim something worked unless the output shows it.
Never suggest dnf, yum or apt: Bazzite is immutable. Updates are done with: ujust update
Bazzite quirks: 'composefs' mounted at / is always 100% full because it's the read-only system image, so ignore it. /etc, /var and /var/home are normally the same partition, so count it once."""

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
]

# Shown as extra detail on the permission prompt. strong=True means type 'yes'.
PERMISSION_RULES = [
    (r"\b(sudo|pkexec|run0)\b", "runs as root", False),
    (r"\b(rm|shred|unlink)\b", "deletes files", True),
    (r"\b(dnf|yum|apt|apt-get)\b", "uses a package manager that doesn't work on Bazzite", False),
    (r"\brpm-ostree\b", "changes the base OS image", True),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "shuts down or restarts the machine", True),
    (r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z)?sh\b", "runs a script from the internet", True),
    (r"\b(kill|killall|pkill)\b", "stops running programs", True),
    (r"\b(chmod|chown)\b", "changes file permissions", True),
    (r"\bflatpak\b.*\b(install|uninstall|remove|update)\b", "installs, removes or updates apps", False),
    (r"\bsystemctl\b.*\b(start|stop|restart|enable|disable|mask)\b", "starts, stops or changes a service", False),
    (r"\bujust\b", "runs a Bazzite system task", False),
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
    "rpm-ostree": {"status"},
    "distrobox": {"list", "ls"},
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
                   "--sync", "--relinquish-var", "--setup-keys"},
    "dmesg": {"-c", "-C", "-D", "-E", "--clear", "--read-clear"},
    "date": {"-s", "--set"},
}
IP_WRITE_WORDS = {"set", "add", "del", "delete", "flush", "change", "replace", "append"}

# Full-screen programs: run attached to the terminal, nothing to summarise.
INTERACTIVE = {"clear", "reset", "top", "htop", "btop", "nvtop", "less", "more", "man", "nano", "vim", "vi",
               "nvim", "ssh", "ncdu", "watch", "tmux", "screen"}

SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;&\n]")
SAFE_REDIRECTS = re.compile(r"\d?>\s*/dev/null|&>\s*/dev/null|\d?>&\d")


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
    if name in READ_ONLY_SUBCOMMANDS:
        sub = next((a for a in args if not a.startswith("-")), None)
        return sub in READ_ONLY_SUBCOMMANDS[name]
    if name not in READ_ONLY_COMMANDS:
        return False

    for flag in FORBIDDEN_FLAGS.get(name, ()):
        if any(a == flag or a.startswith(flag + "=") for a in args):
            return False
    if name == "sed" and any(a.startswith("-i") or a == "--in-place" for a in args):
        return False
    if name == "ping" and not any(a.startswith("-c") for a in args):
        return False  # without -c, ping never stops
    if name == "hostname" and any(not a.startswith("-") for a in args):
        return False  # 'hostname newname' renames the machine
    if name == "awk" and "system(" in segment:
        return False
    return True


def is_read_only(cmd):
    if "$(" in cmd or "`" in cmd:
        return False  # command substitution could hide anything
    stripped = SAFE_REDIRECTS.sub("", cmd)
    if ">" in stripped:
        return False  # writes to a file
    segments = [s.strip() for s in SEGMENT_SPLIT.split(stripped) if s.strip()]
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
        tokens = tokens_for(SEGMENT_SPLIT.split(cmd)[0])
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


def chat_stream(model, messages, keep_alive, num_predict):
    """Yield (text_piece, final_stats_or_None) as the model generates."""
    payload = {
        "model": model, "messages": messages, "stream": True, "think": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": num_predict, "repeat_penalty": 1.1},
    }
    try:
        resp = _post("/api/chat", payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "think" not in body.lower():
            raise OllamaError(body.strip() or str(e)) from e
        payload.pop("think")  # model doesn't support the think flag
        resp = _post("/api/chat", payload)
    with resp:
        for raw in resp:
            if not raw.strip():
                continue
            data = json.loads(raw)
            if data.get("error"):
                raise OllamaError(data["error"])
            piece = (data.get("message") or {}).get("content") or ""
            if data.get("done"):
                yield piece, data
                return
            yield piece, None


def chat(model, messages, keep_alive, num_predict):
    text, final = "", {}
    for piece, done in chat_stream(model, messages, keep_alive, num_predict):
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
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if TTY:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        start, i = time.monotonic(), 0
        while not self._stop.wait(0.1):
            elapsed = time.monotonic() - start
            text = self.text if elapsed < 3 else "loading the model (slower after a break)"
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


def footer(seconds, load_seconds, final=None):
    parts = [f"{seconds:.1f}s"]
    final = final or {}
    if final.get("eval_duration") and (final.get("eval_count") or 0) >= 5:
        parts.append(f"{final['eval_count'] / (final['eval_duration'] / 1e9):.0f} tok/s")
    if load_seconds > 1.5:
        parts.append(f"{load_seconds:.1f}s of that was loading the model")
    print(paint("    (" + " · ".join(parts) + ")", DIM))


def offline_message(err):
    reason = getattr(err, "reason", err)
    print(f"\n  {paint('Can’t reach the AI', RED, BOLD)} at {OLLAMA_URL} ({reason}).")
    print(paint("  Start it with: systemctl --user start ollama   (or: podman start ollama)", DIM))


# ---------------------------------------------------------------- banner -----
LOGO = [
    "▄▀█ █   ▄▄   █▀ █ █ █▀▀ █   █  ",
    "█▀█ █        ▄█ █▀█ ██▄ █▄▄ █▄▄",
]
GRADIENT = [51, 45, 39, 33, 63, 99, 135, 171, 207, 201]
EXAMPLES = ["how much storage do i have", "is sunshine running", "what's using my GPU",
            "install discord", "how hot is my GPU", "what is a flatpak",
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
        stats["model"] = (f"{model} · {paint('offline', RED)} · run: systemctl --user start ollama", None)

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
    print(f"  {paint('Commands', DIM)} /help · /learned · /forget · clear · exit")


HELP = """
  Just type what you want in plain English.

  · Questions about this machine run a read-only command straight away and
    explain the result. Anything that changes the system asks first and says why.
  · General questions get a plain answer.
  · Follow-ups work: "now sort that by size", "restart it".

  Teach me things and I'll remember them for good:
    remember that "clear" means clear the screen
    from now on, show sizes in GB
    when I say "games", I mean the folder /var/mnt/gamedrive
  If you fix a command with e, I'll offer to remember the fix.

  clear       clear the screen
  /learned    list everything I've learned     /unlearn <n>   forget one
  /forget     forget this conversation          /raw           show or hide raw output
  /stats      show the system stats again       exit           quit and free the GPU
"""


# --------------------------------------------------------------- session -----
class Session:
    def __init__(self, model, keep_alive):
        self.model = model
        self.keep_alive = keep_alive
        self.history = []
        self.show_output = True
        self.used_model = False

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
    return shutil.which(word) is not None or word in READ_ONLY_SUBCOMMANDS or word == "ujust"


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


def command_head(cmd):
    """The part of a command that actually executes (a heredoc's body is just text)."""
    return cmd.split("\n", 1)[0] if HEREDOC_RE.search(cmd.split("\n", 1)[0]) else cmd


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


def run_command(cmd, show_output, cap_lines, allow_input=True):
    """Run with live output; returns (exit_code, captured_output, was_interactive)."""
    if allow_input and shutil.which("script") and first_program(cmd) not in INTERACTIVE:
        code, output = run_in_terminal(cmd)
        return code, output, False
    if first_program(cmd) in INTERACTIVE:
        print()
        try:
            code = subprocess.run(cmd, shell=True, executable="/bin/bash").returncode
        except KeyboardInterrupt:
            code = 130
        return code, "", True

    proc = subprocess.Popen(cmd, shell=True, executable="/bin/bash",
                            stdin=None if allow_input else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks, shown, hidden, line_start = [], 0, 0, True
    if show_output:
        print()
    try:
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
    except KeyboardInterrupt:
        pass  # Ctrl+C reaches the child too; just wait for it to exit
    code = proc.wait()
    if show_output and not line_start:
        print()
    if hidden:
        print(paint(f"    … {hidden} more lines (the answer below covers all of it)", DIM))
    return code, strip_ansi(b"".join(chunks).decode(errors="replace")), False


def summarise(session, request, cmd, code, output):
    if len(output) > SUMMARY_INPUT_CHARS:
        half = SUMMARY_INPUT_CHARS // 2
        output = output[:half] + "\n...[output trimmed]...\n" + output[-half:]
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": f"Question: {request}\nCommand: {cmd}\nExit code: {code}\n"
                                    f"Output:\n{output.strip() or '(no output)'}"},
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


def handle(session, request):
    rule = teaching(request)
    if rule:
        add_memory(rule)
        log({"model": session.model, "request": request, "decision": "learned"})
        return
    start = time.monotonic()
    messages = [{"role": "system", "content": ROUTER_PROMPT.format(context=context_line(), model=session.model,
                                                       memories=memories_for_prompt())},
                *session.history, {"role": "user", "content": request}]
    spinner = Spinner("thinking").start()
    buffer, mode, final, out = "", None, {}, Streamer()
    try:
        for piece, done in chat_stream(session.model, messages, session.keep_alive, MAX_REPLY_TOKENS):
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

    if mode == "answer":
        out.end()
        if not out.started:
            print(paint("\n  (no reply — try rephrasing)", DIM))
        footer(time.monotonic() - start, load_seconds, final)
        session.remember(request, buffer.strip())
        log({"model": session.model, "request": request, "decision": "answered"})
        return

    cmd = clean_command(buffer)
    entry = {"model": session.model, "request": request, "command": cmd}
    limit = MAX_SCRIPT_CHARS if "\n" in cmd else MAX_COMMAND_CHARS
    if not cmd or final.get("done_reason") == "length" or len(cmd) > limit:
        print(f"\n  {paint('The model got stuck', RED, BOLD)} and produced a runaway reply, "
              f"so I didn't run anything. Try rephrasing.")
        footer(time.monotonic() - start, load_seconds)
        log({**entry, "command": cmd[:200], "decision": "runaway"})
        return

    while True:
        verdict, reasons, strong = assess(cmd)
        show_command(cmd, "   read-only, running it" if verdict == "read_only" else "")

        if verdict == "blocked":
            print(f"  {paint('Blocked:', RED, BOLD)} this command {reasons[0]}. I won't run it.")
            session.remember(request, f"$ {cmd}\n(blocked: {reasons[0]})")
            log({**entry, "command": cmd, "decision": "blocked", "reasons": reasons})
            return
        if verdict == "read_only":
            break

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
            break
        print(paint("  Cancelled, nothing was run.", DIM))
        session.remember(request, f"$ {cmd}\n(user cancelled)")
        log({**entry, "command": cmd, "decision": "cancelled"})
        return

    show = session.show_output or verdict != "read_only"
    code, output, interactive = run_command(cmd, show, cap_lines=verdict == "read_only",
                                            allow_input=verdict != "read_only")
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
        summary, summary_stats = summarise(session, request, cmd, code, output)
    if entry.get("edited") and code == 0:
        offer_to_learn(request, cmd)
    footer(time.monotonic() - start, load_seconds, summary_stats)
    session.remember(request, f"$ {cmd}\n{summary}".strip())
    log({**entry, "command": cmd, "decision": "ran", "auto": verdict == "read_only",
         "exit_code": code, "seconds": round(time.monotonic() - start, 2)})


# ------------------------------------------------------------------ main -----
def interactive(session, show_banner):
    if show_banner:
        banner(session.model)
    if readline:
        try:
            readline.read_history_file(READLINE_FILE)
        except OSError:
            pass
    prompt = (paint("\nai› ", MAGENTA, BOLD)) if TTY else "\nai> "
    try:
        while True:
            try:
                request = input(prompt).strip()
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
            else:
                handle(session, request)
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
    parser = argparse.ArgumentParser(description="Plain-English terminal assistant for Bazzite.")
    parser.add_argument("request", nargs="*", help="ask once and exit")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default {DEFAULT_MODEL})")
    parser.add_argument("--no-banner", action="store_true", help="skip the welcome screen")
    args = parser.parse_args()

    if args.request:
        handle(Session(args.model, keep_alive="2m"), " ".join(args.request))
    else:
        interactive(Session(args.model, keep_alive="10m"), not args.no_banner)


if __name__ == "__main__":
    main()
