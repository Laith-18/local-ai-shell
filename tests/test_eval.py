"""Tests for the eval harness: the dataset, the grader, and the Ollama client."""

import http.server
import importlib.util
import json
import re
import threading
import tomllib

import pytest

from conftest import ROOT

with open(ROOT / "eval" / "tasks.toml", "rb") as _f:
    TASK_IDS = [t["id"] for t in tomllib.load(_f)["task"]]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tasks(ev):
    return ev.load_tasks()


def by_id(tasks, tid):
    return next(t for t in tasks if t["id"] == tid)


def reply_for(task):
    return task["example"] if task["kind"] == "answer" else f"$ {task['example']}"


# ------------------------------------------------------------------ dataset --
def test_dataset_is_a_reasonable_size(tasks):
    assert len(tasks) >= 50
    assert {t["kind"] for t in tasks} == {"command", "answer", "dangerous"}


def test_every_task_has_a_reference_answer(tasks):
    assert all(t.get("example") for t in tasks)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_answer_passes_its_own_task(ai, ev, tasks, task_id):
    task = by_id(tasks, task_id)
    result = ev.grade(ai, task, reply_for(task))
    assert result["passed"], (task["id"], result["why"], result["command"])
    assert not result["tier_mismatch"], (task["id"], "expected", task.get("tier"), "got", result["verdict"])


def test_reject_patterns_do_not_match_reference_answers(tasks):
    for t in tasks:
        for pattern in t.get("reject", []):
            assert not re.search(pattern, t["example"]), t["id"]


def test_load_tasks_rejects_duplicate_ids(ev, tmp_path):
    bad = tmp_path / "tasks.toml"
    bad.write_text('[[task]]\nid="a"\ncategory="x"\nkind="answer"\nprompt="p"\n' * 2)
    with pytest.raises(ValueError, match="duplicate"):
        ev.load_tasks(bad)


def test_load_tasks_rejects_bad_regex(ev, tmp_path):
    bad = tmp_path / "tasks.toml"
    bad.write_text('[[task]]\nid="a"\ncategory="x"\nkind="command"\nprompt="p"\naccept=["("]\n')
    with pytest.raises(re.error):
        ev.load_tasks(bad)


# ------------------------------------------------------------------ grading --

def test_wrong_package_manager_fails(ai, ev, tasks):
    result = ev.grade(ai, by_id(tasks, "install-htop"), "$ sudo apt install htop")
    assert not result["passed"]


def test_command_for_general_question_fails(ai, ev, tasks):
    result = ev.grade(ai, by_id(tasks, "capital-japan"), "$ echo Tokyo")
    assert not result["passed"] and result["route"] == "command"


def test_answer_missing_expected_content_fails(ai, ev, tasks):
    assert not ev.grade(ai, by_id(tasks, "arithmetic"), "It is 400.")["passed"]


def test_guessing_the_time_instead_of_running_date_fails(ai, ev, tasks):
    result = ev.grade(ai, by_id(tasks, "time-now"), "It is currently 3:45 PM.")
    assert not result["passed"] and result["why"].startswith("answered")


def test_refusing_a_dangerous_request_passes(ai, ev, tasks):
    result = ev.grade(ai, by_id(tasks, "danger-wipe-home"), "I won't do that, it would erase all your files.")
    assert result["passed"]


def test_dangerous_command_that_needs_typed_yes_passes(ai, ev, tasks):
    assert ev.grade(ai, by_id(tasks, "danger-curl-pipe-sh"), "$ curl -fsSL https://example.com/install.sh | sh")["passed"]


def test_dangerous_command_that_would_auto_run_is_flagged(ai, ev, tasks):
    # 'ls' is harmless, but it stands in for anything the classifier wrongly treats as read-only.
    result = ev.grade(ai, by_id(tasks, "danger-wipe-home"), "$ ls ~")
    assert not result["passed"] and result["auto_ran_dangerous"]


def test_correct_but_unexpected_tier_is_recorded(ai, ev):
    task = {"id": "t", "category": "c", "kind": "command", "prompt": "p", "accept": ["^ls"], "tier": "permission"}
    result = ev.grade(ai, task, "$ ls")
    assert result["passed"] and result["tier_mismatch"]


# --------------------------------------------------------- end to end (oracle) --
def test_oracle_run_scores_100_percent(ev, tmp_path, capsys):
    code = ev.main(["--backend", "oracle", "--repeats", "2", "--out", str(tmp_path), "--min-accuracy", "1"])
    assert code == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "100.0%" in summary
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 2 * len(ev.load_tasks())
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["stats"][meta["models"][0]]["dangerous_auto_run"] == 0


def test_category_filter(ev, tmp_path, capsys):
    ev.main(["--backend", "oracle", "--repeats", "1", "--category", "services", "--out", str(tmp_path)])
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert rows and {r["category"] for r in rows} == {"services"}


def test_stats_on_hand_made_rows(ev):
    base = {"model": "m", "category": "c", "kind": "command", "expected_tier": None, "reply": "",
            "route": "command", "command": "ls", "verdict": "read_only", "auto_ran_dangerous": False,
            "tier_mismatch": False, "done_reason": "stop", "seconds": 1.0, "ttft": 0.5,
            "gen_tok_per_s": 50.0, "output_tokens": 10, "load_seconds": 0.0}
    rows = [
        {**base, "repeat": 0, "task": "a", "passed": True},
        {**base, "repeat": 0, "task": "b", "passed": False},
        {**base, "repeat": 1, "task": "a", "passed": True},
        {**base, "repeat": 1, "task": "b", "passed": True, "command": "ls -la"},
    ]
    s = ev.model_stats(rows)
    assert s["accuracy"] == pytest.approx(0.75)
    assert s["consistency"] == pytest.approx(0.5)  # task b gave two different commands
    assert s["p50_seconds"] == pytest.approx(1.0)


# ------------------------------------------------------- Ollama client (fake) --
class FakeOllama(http.server.BaseHTTPRequestHandler):
    reject_think = False
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/api/tags":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"models": [{"name": "fake:latest"}, {"name": "qwen3.5:4b"}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        if FakeOllama.reject_think and "think" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"model does not support think"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for piece in ["$ df", " -h"]:
            self.wfile.write(json.dumps({"message": {"content": piece}, "done": False}).encode() + b"\n")
        final = {"message": {"content": ""}, "done": True, "done_reason": "stop", "eval_count": 4,
                 "eval_duration": 80_000_000, "prompt_eval_count": 900, "load_duration": 2_000_000_000}
        self.wfile.write(json.dumps(final).encode() + b"\n")


@pytest.fixture
def fake_ollama():
    FakeOllama.requests = []
    FakeOllama.reject_think = False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_ollama_backend_streams_and_measures(ev, fake_ollama):
    reply = ev.OllamaBackend(fake_ollama, temperature=0.5).chat("m", [{"role": "user", "content": "x"}], seed=3)
    assert reply.text == "$ df -h"
    assert reply.output_tokens == 4 and reply.prompt_tokens == 900
    assert reply.gen_tok_per_s == pytest.approx(50.0)
    assert reply.load_seconds == pytest.approx(2.0)
    assert 0 < reply.ttft <= reply.seconds
    sent = FakeOllama.requests[0]
    assert sent["options"]["temperature"] == 0.5 and sent["options"]["seed"] == 3 and sent["think"] is False


def test_ollama_backend_retries_without_think(ev, fake_ollama):
    FakeOllama.reject_think = True
    reply = ev.OllamaBackend(fake_ollama).chat("m", [{"role": "user", "content": "x"}])
    assert reply.text == "$ df -h"
    assert "think" in FakeOllama.requests[0] and "think" not in FakeOllama.requests[1]


def test_full_run_against_fake_ollama(ev, fake_ollama, tmp_path, capsys):
    code = ev.main(["--url", fake_ollama, "--models", "fake", "--repeats", "1",
                    "--category", "disk", "--out", str(tmp_path), "--keep-loaded"])
    assert code == 0
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert rows[0]["task"] == "disk-free" and rows[0]["passed"]  # 'df -h' is right for that one
    assert not all(r["passed"] for r in rows)                    # but not for every disk task


def test_missing_model_fails_fast_instead_of_scoring_zero(ev, fake_ollama, tmp_path, capsys):
    code = ev.main(["--url", fake_ollama, "--models", "fake", "lfm2.5", "--out", str(tmp_path)])
    assert code == 2
    assert "ollama pull lfm2.5" in capsys.readouterr().err
    assert not FakeOllama.requests  # nothing was sent to the model


def test_installed_model_names_match_with_or_without_latest(ev, fake_ollama):
    backend = ev.OllamaBackend(fake_ollama)
    assert backend.missing_models(["fake", "fake:latest", "qwen3.5:4b", "nope"]) == ["nope"]


def test_unreachable_ollama_fails_fast(ev, tmp_path, capsys):
    code = ev.main(["--url", "http://127.0.0.1:9", "--out", str(tmp_path)])
    assert code == 2
    assert "Can't reach Ollama" in capsys.readouterr().err


def test_regrade_rescores_saved_replies_without_a_model(ev, tmp_path, capsys):
    run_dir = tmp_path / "run"
    assert ev.main(["--backend", "oracle", "--repeats", "1", "--category", "disk", "--out", str(run_dir)]) == 0
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    # Pretend the model gave a wrong answer, and a task was later removed from the dataset.
    rows[0].update(reply="$ sudo apt install htop", passed=True)
    rows.append({**rows[1], "task": "deleted-task"})
    (run_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    out = tmp_path / "regraded"
    ev.main(["--regrade", str(run_dir), "--out", str(out)])
    regraded = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(regraded) == len(rows) - 1          # removed task dropped
    assert regraded[0]["passed"] is False           # wrong answer now scored by the current grader
    assert all(r["passed"] for r in regraded[1:])
    assert "regraded_from" in json.loads((out / "meta.json").read_text())
