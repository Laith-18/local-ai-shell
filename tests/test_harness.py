"""Tests for the checks around the model: machine facts, installed-program checks,
'| head -1' recovery, usage-error retries, thinking mode and multi-line input.

The end-to-end tests drive handle() against a fake Ollama server, so the whole
pipeline (router -> safety -> run -> fix -> summary) runs without a GPU.
"""

import http.server
import json
import threading

import pytest

# ------------------------------------------------------------------ facts ----
FACTS = {
    "os": "Fedora Linux 44 (KDE Plasma Desktop Edition), kernel 6.19.4",
    "cpu": "Intel(R) Core(TM) i5-9400F CPU @ 2.90GHz (6 cores, 6 threads, no hyper-threading)",
    "ram": "16.7 GB",
    "gpus": ["NVIDIA GeForce RTX 2060, 6.0 GB VRAM. The NVIDIA driver 620.70.06 is installed and working"],
    "board": "ASUSTeK COMPUTER INC. PRIME B365M-A",
    "online": True,
    "installed": ["htop", "fastfetch"],
    "not_installed": ["neofetch", "fzf"],
}


def test_format_facts_lists_everything(ai):
    text = ai.format_facts(FACTS)
    for needle in ("Fedora Linux 44", "i5-9400F", "16.7 GB", "620.70.06 is installed and working",
                   "PRIME B365M-A", "this computer is online", "htop, fastfetch", "neofetch, fzf"):
        assert needle in text


def test_format_facts_offline_and_no_gpu(ai):
    text = ai.format_facts({"online": False})
    assert "couldn't reach the internet" in text
    assert "GPU: none detected" in text


def test_gpu_facts_from_nvidia_smi(ai, monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ai, "run_quiet", lambda argv, timeout=3: "NVIDIA GeForce RTX 2060, 6144, 620.70.06\n")
    assert ai.gpu_facts() == ["NVIDIA GeForce RTX 2060, 6.0 GB VRAM. The NVIDIA driver 620.70.06 is "
                              "installed and working (nvidia-smi works)"]


def test_gpu_facts_falls_back_to_lspci(ai, monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda name: None if name == "nvidia-smi" else "/usr/bin/" + name)
    lspci = ("00:02.0 Host bridge: Intel Corporation 8th Gen Core\n"
             "01:00.0 VGA compatible controller: NVIDIA Corporation TU106 [GeForce RTX 2060 Rev. A] (rev a1)\n")
    monkeypatch.setattr(ai, "run_quiet", lambda argv, timeout=3: lspci)
    assert ai.gpu_facts() == ["NVIDIA Corporation TU106 [GeForce RTX 2060 Rev. A] (rev a1)"]


def test_board_reads_dmi_and_skips_placeholders(ai, monkeypatch, tmp_path):
    for name, value in {"board_vendor": "Micro-Star International Co., Ltd.", "board_name": "B450 TOMAHAWK",
                        "sys_vendor": "To Be Filled By O.E.M.", "product_name": "To Be Filled By O.E.M."}.items():
        (tmp_path / name).write_text(value + "\n")
    monkeypatch.setattr(ai, "DMI", tmp_path)
    assert ai.board() == "Micro-Star International Co., Ltd. B450 TOMAHAWK"


def test_board_includes_laptop_model(ai, monkeypatch, tmp_path):
    for name, value in {"board_vendor": "Dell Inc.", "board_name": "0Y7Y8Y",
                        "sys_vendor": "Dell Inc.", "product_name": "Inspiron 7577"}.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setattr(ai, "DMI", tmp_path)
    assert ai.board() == "Dell Inc. 0Y7Y8Y (system: Dell Inc. Inspiron 7577)"


def test_router_prompt_has_facts_and_shared_rules(ai):
    prompt = ai.ROUTER_PROMPT.format(context="c", model="m", memories="-", facts=ai.format_facts(FACTS),
                                     fedora_rules=ai.FEDORA_RULES)
    assert "620.70.06" in prompt
    assert '"update" is just another name for "upgrade"' in prompt
    assert "/sys/class/dmi/id/board_name" in prompt
    assert "{" not in prompt.replace("{ ", "")  # no unfilled placeholders


def test_summary_prompt_has_facts(ai):
    prompt = ai.SUMMARY_PROMPT.format(facts=ai.format_facts(FACTS), fedora_rules=ai.FEDORA_RULES)
    assert "620.70.06" in prompt and "only column headings" in prompt


# --------------------------------------------------------- missing programs --
@pytest.mark.parametrize("cmd,missing", [
    ("ls -la | grep x", []),
    ("bless -t ~/Pictures", ["bless"]),
    ("sudo nvidia-configurator-xyz --auto", ["nvidia-configurator-xyz"]),
    ("LANG=C ls", []),
    ("for i in 1 2; do echo $i; done", []),
    ("cat > ~/x.sh << 'EOF'\nnot-a-real-program\nEOF", []),  # heredoc body is text, not commands
    ("/definitely/not/here --x", ["/definitely/not/here"]),
    ("echo $(whoami)", []),
    ("frobnicate-a | frobnicate-a", ["frobnicate-a"]),
])
def test_missing_programs(ai, cmd, missing):
    assert ai.missing_programs(cmd) == missing


# ------------------------------------------------------ head -1 recovery -----
HEADER = "memory.total [MiB], memory.used [MiB]\n"


def test_head_that_kept_only_the_header_is_dropped(ai):
    cmd = "nvidia-smi --query-gpu=memory.total,memory.used --format=csv | head -1"
    assert ai.without_short_head(cmd, HEADER) == "nvidia-smi --query-gpu=memory.total,memory.used --format=csv"


@pytest.mark.parametrize("cmd,output", [
    ("nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1", "6144 MiB\n"),  # real data
    ("ps aux | head -n 5", "USER PID\n"),             # head -n 5 is a deliberate top 5
    ("ls -t | head -n1", "notes-2026-11.txt\n"),       # has digits, so it's data
    ("grep 'a | head -1' f", ""),                     # inside quotes, not a pipe
    ("df -h", "Filesystem Size Used\n"),               # no head at all
])
def test_head_is_kept_when_it_did_its_job(ai, cmd, output):
    assert ai.without_short_head(cmd, output) is None


@pytest.mark.parametrize("cmd", ["x | head -1", "x | head -n 1", "x | head -n1", "x |head -2"])
def test_short_head_forms(ai, cmd):
    assert ai.without_short_head(cmd, "a b c\n") == "x"


# ------------------------------------------------------------ usage errors ---
@pytest.mark.parametrize("code,output,expected", [
    (2, 'Field "compute.capability" is not a valid field to query.', True),
    (2, "ERROR: Option -a is not recognized. Please run 'nvidia-smi -h' for help.", True),
    (2, "ls: unrecognized option '--bogus'\nTry 'ls --help' for more information.", True),
    (2, "error: unexpected argument '--bogus' found\n\nUsage: ls [OPTIONS] [FILES]...", True),
    (1, "Usage: ip [ OPTIONS ] OBJECT { COMMAND | help }", True),
    (0, "is not a valid field", False),
    (1, "cat: /nope: No such file or directory", False),
    (130, "invalid option", False),
])
def test_is_usage_error(ai, code, output, expected):
    assert ai.is_usage_error(code, output) is expected


def test_help_text_for_nvidia_query_lists_fields(ai, monkeypatch):
    help_query = ('List of valid properties to query for the switch "--query-gpu":\n\n'
                  '"name" or "gpu_name"\nOfficial product name of the GPU.\n\n'
                  '"memory.total"\nTotal installed GPU memory.\n\n"compute_cap"\nCUDA Compute capability.\n')
    monkeypatch.setattr(ai, "run_quiet", lambda argv, timeout=3: help_query)
    text = ai.help_text("nvidia-smi --query-gpu=memory.total,compute.capability")
    assert text == "Valid --query-gpu fields: name, memory.total, compute_cap"


def test_help_text_only_for_read_only_programs(ai):
    assert ai.help_text("rm --bogus x") == ""
    assert "ls" in ai.help_text("ls --bogus").lower()


# ---------------------------------------------------------------- thinking ---
@pytest.mark.parametrize("mode,request_text,expected", [
    ("auto", "why is my wifi so slow", True),
    ("auto", "can you find out the reason for the high tokens per second", True),
    ("auto", "what's the difference between dnf update and dnf upgrade", True),
    ("auto", "show my ip address", False),
    ("auto", "install htop", False),
    ("on", "show my ip address", True),
    ("off", "why is my wifi so slow", False),
])
def test_should_think(ai, mode, request_text, expected):
    assert ai.should_think(mode, request_text) is expected


def test_set_think(ai, capsys):
    session = ai.Session("m", "1m")
    assert session.think == "on"  # the default since the eval: 81.6% -> 89.7% with thinking
    ai.set_think(session, "off")
    assert session.think == "off"
    ai.set_think(session, "banana")
    assert session.think == "off"


# ------------------------------------------------------------ multi-line -----
def feed(monkeypatch, ai, lines):
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(ai, "readline", None)


def test_triple_quoted_block_is_one_request(ai, monkeypatch):
    feed(monkeypatch, ai, ['"""what does this mean', "| GPU 0 | 50C |", "| 4308MiB / 6144MiB |", '"""'])
    assert ai.read_request("ai> ") == "what does this mean\n| GPU 0 | 50C |\n| 4308MiB / 6144MiB |"


def test_triple_quotes_on_one_line(ai, monkeypatch):
    feed(monkeypatch, ai, ['"""hello there"""'])
    assert ai.read_request("ai> ") == "hello there"


def test_normal_line_is_unchanged(ai, monkeypatch):
    feed(monkeypatch, ai, ["  show my ip  ", "this must not be read"])
    assert ai.read_request("ai> ") == "show my ip"


# ------------------------------------------------- end to end, fake Ollama ---
class FakeOllama(http.server.BaseHTTPRequestHandler):
    """Answers like the model would, choosing a reply by which prompt it was sent."""
    replies = {}
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        system = body.get("messages", [{}])[0].get("content", "")
        if system.startswith("You are ai-shell, a friendly"):
            text = FakeOllama.replies["router"]
        elif "failed with a usage error" in system:
            text = FakeOllama.replies["fix"]
        elif "In one short sentence" in system:
            text = "Changes something."
        else:
            text = FakeOllama.replies.get("summary", "Summary.")
        self.send_response(200)
        self.end_headers()
        if body.get("think"):
            for _ in range(FakeOllama.replies.get("think_chunks", 3)):
                try:
                    self.wfile.write((json.dumps({"message": {"thinking": "hmm "}, "done": False}) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    return  # the client gave up thinking, as it should past the budget
        for chunk in ({"message": {"content": text}, "done": False},
                      {"message": {"content": ""}, "done": True, "eval_count": 3, "eval_duration": 10**8}):
            self.wfile.write((json.dumps(chunk) + "\n").encode())


@pytest.fixture
def pipeline(ai, monkeypatch, tmp_path):
    FakeOllama.requests, FakeOllama.replies = [], {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ai, "OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(ai, "LOG_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ai, "MEMORY_FILE", tmp_path / "memory.json")
    session = ai.Session("test-model", "1m", think="auto")
    session._facts = ai.format_facts(FACTS)

    def ask(request):
        ai.handle(session, request)
        log = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
        return session, log
    yield ask
    server.shutdown()


def kinds(requests):
    out = []
    for r in requests:
        system = r["messages"][0]["content"]
        out.append("router" if system.startswith("You are ai-shell, a friendly")
                   else "fix" if "usage error" in system else "summary")
    return out


def test_usage_error_is_fixed_with_help_text(pipeline, capsys):
    FakeOllama.replies = {"router": "$ ls --bogus-flag /tmp", "fix": "$ ls -d /tmp", "summary": "It's there."}
    session, log = pipeline("does /tmp exist")
    assert kinds(FakeOllama.requests) == ["router", "fix", "summary"]
    fix_request = FakeOllama.requests[1]
    error_text = fix_request["messages"][1]["content"].lower()
    assert "unrecognized option" in error_text or "unexpected argument" in error_text
    assert "Usage: ls" in fix_request["messages"][1]["content"]  # the real help text was included
    assert fix_request["think"] is True                           # reasons when fixing
    assert [e.get("usage_error") for e in log] == [True, None]
    assert log[1]["command"] == "ls -d /tmp" and log[1]["exit_code"] == 0 and log[1]["retry_of"]
    assert "tried again" in capsys.readouterr().out


def test_fix_is_only_tried_once(pipeline):
    FakeOllama.replies = {"router": "$ ls --bogus-flag /tmp", "fix": "$ ls --other-bogus /tmp"}
    _, log = pipeline("does /tmp exist")
    assert kinds(FakeOllama.requests) == ["router", "fix", "summary"]
    assert [e["exit_code"] for e in log] == [2, 2]


def test_same_command_back_from_fix_is_not_rerun(pipeline):
    FakeOllama.replies = {"router": "$ ls --bogus-flag /tmp", "fix": "$ ls --bogus-flag /tmp"}
    _, log = pipeline("does /tmp exist")
    assert kinds(FakeOllama.requests) == ["router", "fix", "summary"]
    assert len(log) == 2  # the failed run (logged before the fix) and the final record of it


def test_header_only_output_is_re_read_in_full(pipeline, capsys):
    FakeOllama.replies = {"router": "$ awk 'BEGIN{print \"memory.total [MiB], memory.used [MiB]\"; "
                                    "print \"6144 MiB, 4301 MiB\"}' | head -1"}
    pipeline("how much vram do i have")
    summary = FakeOllama.requests[-1]["messages"][1]["content"]
    assert "6144 MiB, 4301 MiB" in summary
    assert "kept only the column headings" in summary
    assert "cut that down to just the headings" in capsys.readouterr().out


def test_missing_program_is_not_run(pipeline, capsys):
    FakeOllama.replies = {"router": "$ bless-not-real -t ~/Pictures"}
    session, log = pipeline("browse my pictures")
    assert kinds(FakeOllama.requests) == ["router"]  # no summary, nothing ran
    assert log[-1]["decision"] == "missing" and log[-1]["missing"] == ["bless-not-real"]
    assert "not installed" in capsys.readouterr().out
    assert "not installed" in session.history[-1]["content"]


def test_router_gets_facts_context_and_num_ctx(pipeline, ai):
    FakeOllama.replies = {"router": "Paris is the capital of France."}
    pipeline("what is the capital of france")
    sent = FakeOllama.requests[0]
    assert "620.70.06" in sent["messages"][0]["content"]
    assert sent["options"]["num_ctx"] == ai.NUM_CTX
    assert sent["think"] is False  # a simple question doesn't trigger thinking in auto mode


def test_diagnostic_question_thinks_with_a_bigger_budget(pipeline, ai):
    FakeOllama.replies = {"router": "Because the model is small."}
    pipeline("why is it so fast")
    sent = FakeOllama.requests[0]
    assert sent["think"] is True
    assert sent["options"]["num_predict"] == ai.MAX_REPLY_TOKENS + ai.THINK_BUDGET + 50


# ---------------------------------------------------------- thinking budget ----
def test_thinking_past_the_budget_is_asked_again_without_thinking(pipeline, ai, monkeypatch, capsys):
    monkeypatch.setattr(ai, "THINK_BUDGET", 20)
    FakeOllama.replies = {"router": "Tokyo is the capital of Japan.", "think_chunks": 500}
    session, log = pipeline("why is tokyo the capital")  # 'why' -> thinks in auto mode
    assert [r["think"] for r in FakeOllama.requests] == [True, False]
    out = capsys.readouterr().out
    assert "Tokyo is the capital" in out and "thinking took too long" in out
    assert log[-1]["think"] == "fallback"


def test_thinking_within_the_budget_is_kept(pipeline, ai, monkeypatch, capsys):
    monkeypatch.setattr(ai, "THINK_BUDGET", 20)
    FakeOllama.replies = {"router": "Because it is.", "think_chunks": 5}
    pipeline("why is the sky blue")
    assert [r["think"] for r in FakeOllama.requests] == [True]
    assert "thought it through first" in capsys.readouterr().out


# ------------------------------------------------- commands that can't work ----
@pytest.mark.parametrize("cmd,fixed", [
    ("sudo systemctl --user restart sunshine", "systemctl --user restart sunshine"),
    ("sudo systemctl restart --user sunshine", "systemctl restart --user sunshine"),
    ("sudo -n journalctl --user -u sunshine -n 50", "journalctl --user -u sunshine -n 50"),
    ("sudo systemctl start ollama && sudo systemctl --user start sunshine",
     "sudo systemctl start ollama && systemctl --user start sunshine"),
])
def test_sudo_is_dropped_from_user_services(ai, cmd, fixed):
    assert ai.normalise_command(cmd)[0] == fixed
    assert ai.normalise_command(cmd)[1]


@pytest.mark.parametrize("cmd", ["sudo systemctl restart ollama", "systemctl --user status sunshine",
                                 "echo 'sudo systemctl --user x'", "sudo -u laith systemctl --user status x"])
def test_other_commands_are_left_alone(ai, cmd):
    assert ai.normalise_command(cmd) == (cmd, [])


def test_router_command_with_sudo_and_user_is_fixed_before_running(pipeline, capsys):
    FakeOllama.replies = {"router": "$ sudo systemctl --user status definitely-not-a-unit"}
    _, log = pipeline("is my unit running")
    assert log[-1]["command"] == "systemctl --user status definitely-not-a-unit"
    assert log[-1]["model_command"].startswith("sudo ")
    assert "dropped sudo" in capsys.readouterr().out


# --------------------------------------------------- command inside answers ----
@pytest.mark.parametrize("text,cmd", [
    ("To make SSH start at boot:\n\n$ sudo systemctl enable --now sshd", "sudo systemctl enable --now sshd"),
    ("Run this:\n```bash\n$ ss -tulpn\n```", "ss -tulpn"),
    ("Try one of these:\n$ htop\n$ btop", None),     # a list of options isn't one action
    ("You can check with $ nvidia-smi anytime.", None),  # inline mention, not a command line
    ("Paris is the capital of France.", None),
])
def test_embedded_command(ai, text, cmd):
    assert ai.embedded_command(text) == cmd


def test_command_inside_an_answer_is_offered_and_runs_on_yes(pipeline, ai, monkeypatch, capsys):
    FakeOllama.replies = {"router": "Here's how to list the temp folder:\n\n$ ls -d /tmp", "summary": "It exists."}
    monkeypatch.setattr(ai.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    _, log = pipeline("how do i see the temp folder")
    assert log[-1]["offered"] and log[-1]["command"] == "ls -d /tmp" and log[-1]["exit_code"] == 0
    assert kinds(FakeOllama.requests) == ["router", "summary"]


def test_command_inside_an_answer_is_not_run_on_no(pipeline, ai, monkeypatch):
    FakeOllama.replies = {"router": "Here's how:\n\n$ ls -d /tmp"}
    monkeypatch.setattr(ai.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    _, log = pipeline("how do i see the temp folder")
    assert [e["decision"] for e in log] == ["answered"]


def test_eval_backend_uses_the_same_thinking_budget(pipeline, ai):
    import importlib.util

    from conftest import ROOT
    spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    backend = ev.OllamaBackend(ai.OLLAMA_URL, think="on", think_budget=20)
    FakeOllama.replies = {"router": "$ df -h", "think_chunks": 500}
    messages = [{"role": "system", "content": "You are ai-shell, a friendly assistant"},
                {"role": "user", "content": "how much storage"}]
    reply = backend.chat("m", messages)
    assert reply.text == "$ df -h" and reply.think_fallback and not reply.thought
    assert [r["think"] for r in FakeOllama.requests] == [True, False]
    FakeOllama.requests.clear()
    FakeOllama.replies["think_chunks"] = 5
    reply = backend.chat("m", messages)
    assert reply.thought and not reply.think_fallback and len(FakeOllama.requests) == 1


# ------------------------------------------------- overwriting existing files ----
def test_overwrite_of_an_existing_file_is_detected(ai, tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export PATH=$PATH:~/bin\nalias gs='git status'\n")
    assert ai.overwritten_files(f"cat > {rc} << 'EOF'\nalias ll='ls -la'\nEOF") == [str(rc)]
    assert ai.overwritten_files(f"sort list >{rc}") == [str(rc)]
    assert ai.overwritten_files(f"echo x >> {rc}") == []           # appending is fine
    assert ai.overwritten_files(f"echo x > {tmp_path}/new.txt") == []  # new file
    assert ai.overwritten_files(f"echo '> {rc}'") == []             # inside quotes, not a redirect
    assert ai.overwritten_files("ls 2>/dev/null") == []
    assert ai.line_count(str(rc)) == 2


def test_overwriting_bashrc_needs_typed_yes(pipeline, ai, monkeypatch, tmp_path, capsys):
    rc = tmp_path / ".bashrc"
    rc.write_text("keep me\n")
    FakeOllama.replies = {"router": f"$ cat > {rc} << 'EOF'\nalias ll='ls -la'\nEOF"}
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")  # a plain y isn't enough
    _, log = pipeline("add alias ll to my bashrc")
    assert rc.read_text() == "keep me\n" and log[-1]["decision"] == "cancelled"
    out = capsys.readouterr().out
    assert "overwrites" in out and "REPLACES the whole of" in out


def test_overwrite_prompt_asks_for_yes(pipeline, ai, monkeypatch, tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("keep me\n")
    FakeOllama.replies = {"router": f"$ echo new > {rc}"}
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    pipeline("replace my bashrc")
    assert any("type 'yes'" in p for p in prompts)


# --------------------------------------------------------- path case fixes ----
def test_fix_path_case(ai, tmp_path, monkeypatch):
    (tmp_path / "Downloads" / "Music").mkdir(parents=True)
    assert ai.fix_path_case(f"ls -lh {tmp_path}/downloads") == f"ls -lh {tmp_path}/Downloads"
    assert ai.fix_path_case(f"ls {tmp_path}/downloads/music") == f"ls {tmp_path}/Downloads/Music"
    assert ai.fix_path_case(f"ls {tmp_path}/Downloads") is None      # already right
    assert ai.fix_path_case(f"ls {tmp_path}/nothing-like-it") is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ai.fix_path_case("ls -lh ~/downloads") == "ls -lh ~/Downloads"  # keeps the ~ form


def test_fix_path_case_refuses_ambiguous_matches(ai, tmp_path):
    (tmp_path / "Notes").mkdir()
    (tmp_path / "NOTES").mkdir()
    assert ai.fix_path_case(f"ls {tmp_path}/notes") is None


def test_wrong_case_path_is_fixed_and_rerun(pipeline, tmp_path):
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "song.mp3").write_text("x")
    FakeOllama.replies = {"router": f"$ ls {tmp_path}/downloads", "summary": "One song."}
    _, log = pipeline("what's in my downloads")
    assert log[0]["path_case"] and log[0]["exit_code"] != 0
    assert log[-1]["command"] == f"ls {tmp_path}/Downloads" and log[-1]["exit_code"] == 0


# ------------------------------------------------- sudo on package queries ----
@pytest.mark.parametrize("cmd,fixed", [
    ("sudo dnf search --all obs", "dnf search --all obs"),
    ("sudo rpm -qa | grep steam", "rpm -qa | grep steam"),
    ("sudo flatpak list --app", "flatpak list --app"),
    ("sudo dnf check-upgrade", "dnf check-upgrade"),
])
def test_sudo_is_dropped_from_package_queries(ai, cmd, fixed):
    out, fixes = ai.normalise_command(cmd)
    assert out == fixed and "doesn't need root" in fixes[0]


@pytest.mark.parametrize("cmd", ["sudo dnf install htop", "sudo dnf remove htop", "sudo dnf upgrade --refresh",
                                 "sudo flatpak install flathub discord", "sudo rpm -i x.rpm"])
def test_sudo_is_kept_for_package_changes(ai, cmd):
    assert ai.normalise_command(cmd) == (cmd, [])


@pytest.mark.parametrize("cmd,verdict", [("command -v steam", "read_only"), ("command -V ls", "read_only"),
                                         ("command steam", "permission"), ("command -v", "permission")])
def test_command_v_is_read_only(ai, cmd, verdict):
    assert ai.assess(cmd)[0] == verdict


# --------------------------------------------------------------- cpu facts ----
def fake_cpuinfo(cores, threads_per_core):
    blocks = []
    for core in range(cores):
        for _ in range(threads_per_core):
            blocks.append(f"processor\t: {len(blocks)}\nmodel name\t: Test CPU\nphysical id\t: 0\ncore id\t: {core}\n")
    return "\n".join(blocks)


@pytest.mark.parametrize("cores,tpc,expected", [
    (6, 1, "6 cores, 6 threads, no hyper-threading"),        # i5-9400F
    (6, 2, "6 cores, 12 threads, with hyper-threading"),      # Ryzen 5 5600X
])
def test_cpu_topology(ai, monkeypatch, cores, tpc, expected):
    info = fake_cpuinfo(cores, tpc)
    monkeypatch.setattr(ai, "read_text", lambda path: info if "cpuinfo" in str(path) else "")
    monkeypatch.setattr(ai.os, "cpu_count", lambda: cores * tpc)
    assert ai.cpu_topology() == expected
