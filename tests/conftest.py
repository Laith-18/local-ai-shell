"""Load ai-shell.py as a module (its hyphenated name can't be imported normally).

ai-shell picks its distro rules from /etc/os-release when it's imported, so the tests pin
the distro: Fedora by default (CI runs on Ubuntu), and a separate copy for the Debian tests.
"""

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("AI_SHELL_DISTRO", "fedora")


def load_ai_shell(distro=None):
    previous = os.environ.get("AI_SHELL_DISTRO")
    if distro:
        os.environ["AI_SHELL_DISTRO"] = distro
    try:
        spec = importlib.util.spec_from_file_location(f"ai_shell_{distro or 'default'}", ROOT / "ai-shell.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("AI_SHELL_DISTRO", None)
        else:
            os.environ["AI_SHELL_DISTRO"] = previous


@pytest.fixture(scope="session")
def ai():
    return load_ai_shell()


@pytest.fixture(scope="session")
def ai_debian():
    return load_ai_shell("debian")
