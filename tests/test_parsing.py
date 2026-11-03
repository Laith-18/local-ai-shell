"""Tests for turning raw model replies into commands, and for small helpers."""

import pytest


@pytest.mark.parametrize("reply,expected", [
    ("$ df -h", "df -h"),
    ("df -h", "df -h"),
    ("```bash\ndf -h\n```", "df -h"),
    ("`ls -la`", "ls -la"),
    ("<think>the user wants files</think>\n$ ls", "ls"),
    ("df -h\nThis shows your disk space.", "df -h"),  # chatter after the command is dropped
    ("\n\n$ uptime", "uptime"),
    ("", ""),
])
def test_clean_command(ai, reply, expected):
    assert ai.clean_command(reply) == expected


def test_clean_command_keeps_heredoc_body_and_drops_trailing_chatter(ai):
    reply = "$ cat > ~/x.txt << 'EOF'\nhello\nworld\nEOF\nI wrote the file for you."
    assert ai.clean_command(reply) == "cat > ~/x.txt << 'EOF'\nhello\nworld\nEOF"


def test_command_head_of_heredoc_is_first_line(ai):
    cmd = "cat > f << EOF\nrm -rf /\nEOF"
    assert ai.command_head(cmd) == "cat > f << EOF"


def test_command_head_of_plain_command_is_unchanged(ai):
    assert ai.command_head("ls -la") == "ls -la"


@pytest.mark.parametrize("buffer,finished,expected", [
    ("$ ls", False, "command"),
    ("`df -h`", False, "command"),
    ("df -h", True, "command"),
    ("ls", False, None),  # too early to tell
    ("The answer", False, None),
    ("Paris is the capital of France.", True, "answer"),
    ("hello there friend", True, "answer"),
    ("", True, "answer"),
    ("", False, None),
])
def test_decide(ai, buffer, finished, expected):
    assert ai.decide(buffer, finished) == expected


@pytest.mark.parametrize("request_text,rule", [
    ("remember that clear means clear the screen", "Clear means clear the screen"),
    ("Remember: always use dnf5", "Always use dnf5"),
    ("from now on use dnf5", "From now on use dnf5"),
    ("when i say gpu, i mean the RTX 2060", "When i say gpu, i mean the RTX 2060"),
    ("what is ram", None),
    ("remember", None),
])
def test_teaching(ai, request_text, rule):
    assert ai.teaching(request_text) == rule


@pytest.mark.parametrize("returncode,expected", [(0, 0), (1, 1), (130, 130), (-2, 130), (-9, 137)])
def test_exit_code_uses_shell_convention(ai, returncode, expected):
    assert ai.exit_code(returncode) == expected


def test_first_program_skips_env_vars_and_sudo(ai):
    assert ai.first_program("LANG=C sudo -n dnf list | head") == "dnf"


def test_strip_ansi(ai):
    assert ai.strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"
