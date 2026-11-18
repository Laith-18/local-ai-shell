# Changelog

## v0.5.0 (18 Nov 2026): fixes from the first 3-repeat thinking run

The thinking run scored 94.3%, and its failures pointed at a few concrete bugs ([report](eval/reports/2026-11-18/)).

**Safety**
- Overwriting an existing, non-empty file with `>` now needs a typed `yes`, with a warning that says how many lines it would replace. The thinking model answered "add a line to my .bashrc" with `cat > ~/.bashrc` on every repeat.
- `sudo` is dropped from package queries (`dnf search/list/info/…`, `rpm -q`, `flatpak list/info/search`). They don't need root, and as read-only commands they would auto-run and then hang on a password prompt.

**Fewer wrong answers**
- Prompt: `>>` adds to a file and `>` replaces it; never tell the user to run a command, reply with it instead; quote numbers from pasted output with their labels.
- The CPU fact now spells out the topology ("6 cores, 6 threads, no hyper-threading"); with only "6 threads" the model invented hyper-threading.
- Known-good command for emptying a folder but keeping it: `find ~/Downloads -mindepth 1 -delete`.
- A path that only differs by case (`~/downloads`) is fixed and the command run again, without asking the model.
- `command -v X` is recognised as read-only.

**Eval**
- `answer_reject` for answers from the facts with a wrong detail; stricter checks on pasted `nvidia-smi` output; `find … -exec rm` accepted for emptying a folder; `command -v` accepted for "is X installed".
- The day's four real runs are committed in `eval/reports/2026-11-18/`, with as-run and regraded scores.

**Other**
- README: what's new, 3-repeat findings, a corrected claim (the 4B with thinking also produced destructive commands, so the safety layer is what holds, not the model), new failure modes.
- CI badge and workflow point at `main` (the `fedora` branch no longer exists).
- ~415 tests.

## v0.4.0 (18 Nov 2026): think by default

- **Thinking is on by default** (`/think on`), with a 1,000-token budget (`AI_SHELL_THINK_BUDGET`): past it, the request is dropped and asked again without thinking, so a reply never stalls or comes back empty. `/think auto` and `/think off` remain.
- `sudo` is dropped from `systemctl --user` / `journalctl --user`, which can never work with it.
- An answer that contains exactly one `$ command` line offers to run it ("Run that command?").
- Prompt: Sunshine is a user service and Ollama a system one; "it" means the one thing just discussed; known-good commands for uptime, `pgrep`, kernel logs, and Sunshine and Ollama status, logs and restarts.
- Eval mirrors the real pipeline: `--think off|auto|on`, `--think-budget`, the same fallback and command repairs, and commands offered from answers. Reports think fallbacks, offers and fixes.
- Dangerous tasks get a `danger` pattern, so a harmless look (`lsblk -f` before a format) isn't counted as unsafe; several graders that failed valid commands were loosened.

## v0.3.0 (17 Nov 2026): ground the model, check its commands

- **Verified machine facts** in every prompt (OS, CPU, RAM, GPU and driver, motherboard, internet, installed tools); `/facts` shows them.
- Shared Fedora facts in every prompt (e.g. `dnf update` is an alias of `upgrade`) and known-good commands for GPU stats, motherboard, BIOS, IP, updates, failed services and boot errors.
- Programs that aren't installed are never run.
- Usage errors get one retry with the program's real `--help` (for `nvidia-smi --query-gpu`, the list of valid fields).
- Header-only output from a trailing `| head -1` is re-read in full.
- `/think` modes, an 8K context window and longer conversation memory.
- Multi-line paste arrives as one question (bracketed paste, a 50 ms paste window, and `"""` blocks).
- Eval: nine tasks from real failures, fixed machine facts, `answer_ok`, and `eval/from_history.py` to draft tasks from the history log.
- Follow-up fix (Copilot): usage-error detection for the Rust coreutils `ls` on Fedora 45 ("unexpected argument").

## Earlier

- Automated eval harness (78 tasks), CI with ruff and pytest on Python 3.11–3.14, a hardened read-only allowlist with table-driven safety tests, quote-aware pipeline splitting, and the port from Bazzite to Fedora. See the git history.
