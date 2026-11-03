"""Regression tests for Ctrl+C while a command is running.

Bug: pressing Ctrl+C during a read-only command raised KeyboardInterrupt inside
proc.wait() and crashed the whole session with a traceback.

The test runs run_command() in a child Python process in its own process group,
then sends SIGINT to the whole group, which is exactly what the terminal does
when you press Ctrl+C.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import ROOT

CHILD = textwrap.dedent("""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("ai_shell", sys.argv[1])
    ai = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ai)
    print("READY", flush=True)
    code, output, _ = ai.run_command(sys.argv[2], show_output=False, cap_lines=True,
                                     allow_input=False)
    print("EXIT", code, flush=True)
    print("STILL ALIVE", flush=True)
""")


def ctrl_c_during(cmd, after=0.7):
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT / "ai-shell.py"), cmd],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    assert proc.stdout.readline().strip() == "READY"
    time.sleep(after)
    os.killpg(proc.pid, signal.SIGINT)
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail("ai-shell hung after Ctrl+C")
    return proc.returncode, out, err


@pytest.mark.parametrize("cmd", [
    pytest.param("for i in $(seq 100); do echo line $i; sleep 0.1; done", id="while-output-streams"),
    pytest.param("exec >/dev/null; sleep 5", id="while-waiting-for-exit"),
])
def test_ctrl_c_stops_the_command_but_not_ai_shell(cmd):
    returncode, out, err = ctrl_c_during(cmd)
    assert "Traceback" not in err, err
    assert returncode == 0
    assert "EXIT 130" in out
    assert "STILL ALIVE" in out


def test_ctrl_c_handler_is_restored_afterwards(ai):
    before = signal.getsignal(signal.SIGINT)
    ai.run_command("true", show_output=False, cap_lines=True, allow_input=False)
    assert signal.getsignal(signal.SIGINT) is before


def test_normal_command_output_is_captured(ai):
    code, output, interactive = ai.run_command("echo hello; echo oops >&2; exit 3",
                                               show_output=False, cap_lines=True,
                                               allow_input=False)
    assert (code, interactive) == (3, False)
    assert "hello" in output and "oops" in output
