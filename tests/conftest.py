"""Load ai-shell.py as a module (its hyphenated name can't be imported normally)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_ai_shell():
    spec = importlib.util.spec_from_file_location("ai_shell", ROOT / "ai-shell.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def ai():
    return load_ai_shell()
