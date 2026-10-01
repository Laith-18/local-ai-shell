#!/usr/bin/env bash
# ai-bench.sh - benchmark local Ollama models for the AI shell assistant project.
#
# Measures, for each model: cold start, warm GPU speed, GPU/CPU split, command
# accuracy (with and without a Bazzite system prompt), whether thinking can be
# switched off, and CPU-only speed. It NEVER runs the commands the models suggest;
# it only records the text they reply with.
#
# Usage:  bash ai-bench.sh
# Output: ~/ai-bench/results/<timestamp>/summary.md  (plus raw JSON for every request)

set -uo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
CONTAINER="${CONTAINER:-ollama}"
MODELS=("lfm2.5" "qwen3.5:4b")
NUM_CTX=4096
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-$HOME/ai-bench/results/$STAMP}"
RESULTS="$OUT_DIR/results.jsonl"
HELPER="$OUT_DIR/helper.py"

SPEED_PROMPT="Explain what each of these commands does in one sentence each: ls, cd, grep, df, du, top, chmod, systemctl, journalctl, podman."
SUFFIX=" Reply with only the command."

SYSTEM_PROMPT="You are a Linux shell assistant running on Bazzite, an immutable Fedora Atomic-based gaming distro. Reply with only the shell command: no explanation and no markdown. The base system is read-only, so never use dnf or apt. Install GUI apps with Flatpak from Flathub. Use ujust for Bazzite system tasks such as updates, and rpm-ostree only as a last resort. Use distrobox for command-line tools that are not in the base image. Containers use podman, not docker. Sunshine runs as a systemd user service, so manage it with systemctl --user. Only use sudo when it is actually required."

# Accuracy tests: id | task | prompt | expected answer (for grading, never executed)
TEST_IDS=(1 2 3 4 5 6 7 8 9)
TEST_TASKS=(
  "Free disk space"
  "Top memory processes"
  "Last 50 journal lines"
  "Restart Sunshine"
  "Install Firefox on Bazzite"
  "Find files over 1GB"
  "Process on a port"
  "Update Bazzite"
  "Empty Downloads safely"
)
TEST_PROMPTS=(
  "Show free space on all mounted drives in human-readable form."
  "Show the 10 processes using the most memory."
  "Show the last 50 lines of the system journal."
  "Restart the Sunshine service, which runs as a user service."
  "Install Firefox on Bazzite, an immutable Fedora-based distro."
  "Find all files larger than 1GB in my home directory."
  "Show which process is listening on TCP port 47989."
  "Update the whole system on Bazzite."
  "Delete everything inside my Downloads folder, but not the folder itself."
)
TEST_EXPECTED=(
  "df -h"
  "ps aux --sort=-%mem | head -n 11"
  "journalctl -n 50"
  "systemctl --user restart sunshine"
  "flatpak install flathub org.mozilla.firefox"
  "find ~ -type f -size +1G"
  "ss -tlnp | grep 47989  (sudo needed to see the process name)"
  "ujust update  (or rpm-ostree upgrade)"
  "rm -rf ~/Downloads/*  (must not touch ~ or remove the folder itself)"
)

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---------------------------------------------------------------- helper ----
write_helper() {
cat > "$HELPER" <<'PYEOF'
import json, re, sys

def norm(m):
    return m if ":" in m else m + ":latest"

def load(path):
    with open(path) as f:
        return json.load(f)

def payload(a):
    model, prompt, system, num_gpu, think, num_ctx = a
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    opts = {"temperature": 0, "seed": 42, "num_ctx": int(num_ctx)}
    if num_gpu != "":
        opts["num_gpu"] = int(num_gpu)
    p = {"model": model, "messages": msgs, "stream": False,
         "options": opts, "keep_alive": "10m"}
    if think in ("true", "false"):
        p["think"] = think == "true"
    print(json.dumps(p))

def iserror(a):
    try:
        r = load(a[0])
    except Exception:
        sys.exit(0)
    sys.exit(0 if r.get("error") else 1)

def strip_think(s):
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S)

def record(a):
    (resp_file, results, model, phase, test_id, task, prompt,
     expected, sysprompt, num_gpu, think) = a
    try:
        r = load(resp_file)
    except Exception as e:
        r = {"error": f"bad JSON from Ollama: {e}"}
    msg = r.get("message") or {}
    content = msg.get("content") or ""
    thinking = msg.get("thinking") or ""
    ev = r.get("eval_count") or 0
    evd = r.get("eval_duration") or 0
    pc = r.get("prompt_eval_count") or 0
    pd = r.get("prompt_eval_duration") or 0
    rec = {
        "model": model, "phase": phase, "test_id": test_id, "task": task,
        "prompt": prompt, "expected": expected,
        "system_prompt": sysprompt == "1", "num_gpu": num_gpu, "think": think,
        "content": content, "thinking": thinking,
        "thinking_leaked": bool(thinking.strip()) or "<think>" in content,
        "eval_count": ev, "prompt_eval_count": pc,
        "eval_tps": round(ev / (evd / 1e9), 2) if evd else None,
        "prompt_tps": round(pc / (pd / 1e9), 2) if pd else None,
        "total_s": round((r.get("total_duration") or 0) / 1e9, 3),
        "load_s": round((r.get("load_duration") or 0) / 1e9, 3),
        "error": r.get("error"),
    }
    with open(results, "a") as f:
        f.write(json.dumps(rec) + "\n")
    ans = re.sub(r"\s+", " ", strip_think(content)).strip()
    if rec["error"]:
        status = f"ERROR: {rec['error']}"
    else:
        status = f"{rec['eval_tps']} tok/s, {ev} tokens, {rec['total_s']}s"
        if rec["thinking_leaked"] and think != "true":
            status += ", THOUGHT ANYWAY"
    print(f"      -> {status} | {ans[:80]}")

def ps(a):
    resp_file, results, label = a
    r = load(resp_file)
    models = r.get("models", [])
    if not models:
        print("      -> nothing loaded")
    for m in models:
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        gpu = round(100 * vram / size) if size else 0
        rec = {"phase": "ps", "label": label, "model": m.get("name"),
               "size_gb": round(size / 1e9, 2), "vram_gb": round(vram / 1e9, 2),
               "gpu_pct": gpu, "cpu_pct": 100 - gpu,
               "context": m.get("context_length")}
        with open(results, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"      -> {rec['model']}: {rec['size_gb']} GB, "
              f"{rec['cpu_pct']}%/{rec['gpu_pct']}% CPU/GPU")

def has_model(a):
    tags_file, m = a
    names = [x.get("name") for x in load(tags_file).get("models", [])]
    sys.exit(0 if norm(m) in names else 1)

def loaded(a):
    for x in load(a[0]).get("models", []):
        print(x["name"])

def fmt(v, suf=""):
    return "-" if v is None else f"{v}{suf}"

def clean(s, n=300):
    s = re.sub(r"\s+", " ", strip_think(s)).strip()
    if len(s) > n:
        s = s[:n] + "..."
    return s.replace("|", "\\|") or "(empty)"

def summary(a):
    results, sysinfo, out = a
    recs = [json.loads(l) for l in open(results) if l.strip()]
    chats = [r for r in recs if r.get("phase") != "ps"]
    pss = [r for r in recs if r.get("phase") == "ps"]
    models = []
    for r in chats:
        if r["model"] not in models:
            models.append(r["model"])

    def last(m, phase):
        xs = [r for r in chats if r["model"] == m and r["phase"] == phase]
        return xs[-1] if xs else {}

    def psrec(m, kind):
        return next((p for p in pss if p["label"] == f"{m} {kind}"
                     and p["model"] == norm(m)), {})

    L = ["# Local model benchmark results", ""]
    L += ["## System", "```", open(sysinfo).read().strip(), "```", ""]

    L += ["## Speed (thinking off, temperature 0)", "",
          "| Model | Size | Default split (CPU/GPU) | Cold start total | Cold load | "
          "GPU gen tok/s | GPU prompt tok/s | CPU-only gen tok/s | "
          "CPU-only prompt tok/s | CPU-only check |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        cold, warm, cpu = last(m, "cold"), last(m, "gpu_warm"), last(m, "cpu")
        g, c = psrec(m, "gpu"), psrec(m, "cpu")
        split = f"{g['cpu_pct']}/{g['gpu_pct']}" if g else "-"
        size = f"{g['size_gb']} GB" if g else "-"
        ccheck = f"{c['cpu_pct']}% CPU" if c else "-"
        L.append(f"| {m} | {size} | {split} | {fmt(cold.get('total_s'), 's')} | "
                 f"{fmt(cold.get('load_s'), 's')} | {fmt(warm.get('eval_tps'))} | "
                 f"{fmt(warm.get('prompt_tps'))} | {fmt(cpu.get('eval_tps'))} | "
                 f"{fmt(cpu.get('prompt_tps'))} | {ccheck} |")
    L.append("")

    def avg(xs):
        return round(sum(xs) / len(xs)) if xs else None

    L += ["## Thinking behaviour", "",
          "| Model | Thought anyway with think:false | Avg tokens/answer "
          "(no system prompt) | Avg tokens/answer (Bazzite prompt) | "
          "Test 1 tokens: think off -> on |",
          "|---|---|---|---|---|"]
    for m in models:
        off = [r for r in chats if r["model"] == m
               and r["think"] in ("false", "unsupported") and not r["error"]]
        leaks = sum(1 for r in off if r["thinking_leaked"])
        raw = [r["eval_count"] for r in chats
               if r["model"] == m and r["phase"] == "acc_raw"]
        sysp = [r["eval_count"] for r in chats
                if r["model"] == m and r["phase"] == "acc_sys"]
        t_off = next((r["eval_count"] for r in chats if r["model"] == m
                      and r["phase"] == "acc_raw" and r["test_id"] == "1"), None)
        t_on = next((r["eval_count"] for r in chats if r["model"] == m
                     and r["phase"] == "think_on"), None)
        unsup = any(r["think"] == "unsupported" for r in chats if r["model"] == m)
        note = " (think flag rejected)" if unsup else ""
        L.append(f"| {m} | {leaks}/{len(off)}{note} | {fmt(avg(raw))} | "
                 f"{fmt(avg(sysp))} | {fmt(t_off)} -> {fmt(t_on)} |")
    L.append("")

    L += ["## Accuracy", "",
          "Thinking is stripped and answers are cut to 300 characters. "
          "Grade each one: correct / works but not ideal / wrong.", ""]
    tests = []
    for r in chats:
        if r["phase"] in ("acc_raw", "acc_sys") and \
                r["test_id"] not in [t[0] for t in tests]:
            tests.append((r["test_id"], r.get("task", ""), r["prompt"], r["expected"]))
    for tid, task, prompt, exp in tests:
        L += [f"### Test {tid}: {task}", "",
              f"Prompt: {prompt}  ", f"Expected: `{exp}`", "",
              "| Model | Setup | Answer | Tokens | Time |", "|---|---|---|---|---|"]
        for m in models:
            for ph, label in (("acc_raw", "no system prompt"),
                              ("acc_sys", "Bazzite system prompt")):
                r = next((x for x in chats if x["model"] == m and x["phase"] == ph
                          and x["test_id"] == tid), None)
                if not r:
                    continue
                ans = "ERROR: " + clean(str(r["error"])) if r["error"] else clean(r["content"])
                L.append(f"| {m} | {label} | {ans} | {r['eval_count']} | {r['total_s']}s |")
        L.append("")

    errs = [r for r in chats if r.get("error")]
    if errs:
        L += ["## Errors", ""]
        for r in errs:
            L.append(f"- {r['model']} / {r['phase']} / test {r['test_id']}: {r['error']}")
        L.append("")

    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")

if __name__ == "__main__":
    {"payload": payload, "iserror": iserror, "record": record, "ps": ps,
     "has_model": has_model, "loaded": loaded,
     "summary": summary}[sys.argv[1]](sys.argv[2:])
PYEOF
}

# ------------------------------------------------------------- functions ----
wait_for_api() {
  for _ in $(seq 1 60); do
    curl -sf "$OLLAMA_URL/api/version" >/dev/null && return 0
    sleep 2
  done
  return 1
}

ensure_container() {
  if ! podman container exists "$CONTAINER"; then
    log "No container called '$CONTAINER'. Start it first with 'podman compose up -d'"
    log "in your project folder (or the podman run command), then re-run this script."
    exit 1
  fi
  if [[ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]]; then
    log "Starting the $CONTAINER container (it doesn't auto-start after a reboot yet)"
    podman start "$CONTAINER" >/dev/null || { log "podman start failed"; exit 1; }
  fi
  log "Waiting for the Ollama API..."
  wait_for_api || { log "Ollama API not responding at $OLLAMA_URL"; exit 1; }
  log "Ollama is up"
}

ensure_models() {
  local tags="$OUT_DIR/raw/tags.json" m
  curl -s "$OLLAMA_URL/api/tags" -o "$tags"
  for m in "${MODELS[@]}"; do
    if python3 "$HELPER" has_model "$tags" "$m"; then
      log "Model present: $m"
    else
      log "Model missing, pulling: $m"
      podman exec "$CONTAINER" ollama pull "$m" || { log "Pull failed: $m"; exit 1; }
    fi
  done
}

unload_all() {
  local f="$OUT_DIR/raw/ps_tmp.json" n
  curl -s "$OLLAMA_URL/api/ps" -o "$f" || return
  while read -r n; do
    [[ -z "$n" ]] && continue
    curl -s "$OLLAMA_URL/api/generate" -d "{\"model\":\"$n\",\"keep_alive\":0}" >/dev/null
  done < <(python3 "$HELPER" loaded "$f")
  for _ in $(seq 1 15); do
    curl -s "$OLLAMA_URL/api/ps" -o "$f"
    [[ -z "$(python3 "$HELPER" loaded "$f")" ]] && return
    sleep 2
  done
  log "Warning: a model was still loaded after 30s"
}

capture_ps() {
  local label="$1"
  local f="$OUT_DIR/raw/ps_${label//[ :\/]/_}.json"
  curl -s "$OLLAMA_URL/api/ps" -o "$f"
  python3 "$HELPER" ps "$f" "$RESULTS" "$label"
}

# run_chat MODEL PHASE TEST_ID TASK PROMPT SYSTEM NUM_GPU THINK EXPECTED
run_chat() {
  local model="$1" phase="$2" tid="$3" task="$4" prompt="$5" system="$6"
  local num_gpu="$7" think="$8" expected="$9"
  local tmp="$OUT_DIR/raw/${model//[:\/]/_}_${phase}_${tid}.json"
  local payload has_sys=0
  [[ -n "$system" ]] && has_sys=1

  payload="$(python3 "$HELPER" payload "$model" "$prompt" "$system" "$num_gpu" "$think" "$NUM_CTX")"
  curl -s --max-time 900 -H 'Content-Type: application/json' -d "$payload" \
    "$OLLAMA_URL/api/chat" -o "$tmp" || echo '{"error":"curl failed or timed out"}' > "$tmp"

  if [[ "$think" != "default" ]] && python3 "$HELPER" iserror "$tmp"; then
    log "      think=$think was rejected, retrying without the think flag"
    think="unsupported"
    payload="$(python3 "$HELPER" payload "$model" "$prompt" "$system" "$num_gpu" default "$NUM_CTX")"
    curl -s --max-time 900 -H 'Content-Type: application/json' -d "$payload" \
      "$OLLAMA_URL/api/chat" -o "$tmp" || echo '{"error":"curl failed or timed out"}' > "$tmp"
  fi

  python3 "$HELPER" record "$tmp" "$RESULTS" "$model" "$phase" "$tid" "$task" \
    "$prompt" "$expected" "$has_sys" "$num_gpu" "$think"
}

capture_sysinfo() {
  local f="$OUT_DIR/sysinfo.txt"
  {
    echo "Date: $(date)"
    grep '^PRETTY_NAME' /etc/os-release
    echo "Kernel: $(uname -r)"
    echo "CPU: $(lscpu | sed -n 's/^Model name:[[:space:]]*//p')"
    free -h | sed -n '1,2p'
    nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used \
      --format=csv,noheader 2>/dev/null | sed 's/^/GPU (name, driver, total, used before test): /'
    echo "Ollama: $(podman exec "$CONTAINER" ollama --version 2>/dev/null | tail -n 1)"
    if [[ "$WANT_DMI" == "y" ]]; then
      echo "RAM sticks:"
      sudo dmidecode -t memory 2>/dev/null \
        | grep -E '^[[:space:]]+(Size|Speed|Locator):' | grep -v 'No Module' | sed 's/^[[:space:]]*/  /'
    fi
  } > "$f"
}

# ------------------------------------------------------------------ main ----
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
command -v curl >/dev/null || { echo "curl not found"; exit 1; }

mkdir -p "$OUT_DIR/raw"
write_helper

echo "Results will go to: $OUT_DIR"
echo "Don't stream or game on this box while the test runs (it needs the GPU)."
read -rp "Record RAM stick layout with 'sudo dmidecode' (asks for your password)? [y/N] " WANT_DMI
WANT_DMI="${WANT_DMI,,}"
[[ "$WANT_DMI" == "y" ]] && { sudo -v || WANT_DMI="n"; }

ensure_container
ensure_models
unload_all
capture_sysinfo
log "System info saved"

for model in "${MODELS[@]}"; do
  log "================ $model ================"
  unload_all

  log "  Cold start (speed prompt, default GPU offload)"
  run_chat "$model" cold speed "Speed" "$SPEED_PROMPT" "" "" false ""

  log "  Warm GPU speed (2 runs, the 2nd one counts)"
  for i in 1 2; do
    run_chat "$model" gpu_warm "speed$i" "Speed" "$SPEED_PROMPT" "" "" false ""
  done
  capture_ps "$model gpu"

  log "  Accuracy, no system prompt"
  for i in "${!TEST_IDS[@]}"; do
    log "    Test ${TEST_IDS[$i]}: ${TEST_TASKS[$i]}"
    run_chat "$model" acc_raw "${TEST_IDS[$i]}" "${TEST_TASKS[$i]}" \
      "${TEST_PROMPTS[$i]}$SUFFIX" "" "" false "${TEST_EXPECTED[$i]}"
  done

  log "  Accuracy, with Bazzite system prompt"
  for i in "${!TEST_IDS[@]}"; do
    log "    Test ${TEST_IDS[$i]}: ${TEST_TASKS[$i]}"
    run_chat "$model" acc_sys "${TEST_IDS[$i]}" "${TEST_TASKS[$i]}" \
      "${TEST_PROMPTS[$i]}$SUFFIX" "$SYSTEM_PROMPT" "" false "${TEST_EXPECTED[$i]}"
  done

  log "  Thinking cost check (test 1 with thinking ON)"
  run_chat "$model" think_on 1 "${TEST_TASKS[0]}" "${TEST_PROMPTS[0]}$SUFFIX" \
    "" "" true "${TEST_EXPECTED[0]}"

  log "  CPU-only speed (num_gpu=0, 2 runs, the 2nd one counts)"
  for i in 1 2; do
    run_chat "$model" cpu "speed$i" "Speed" "$SPEED_PROMPT" "" 0 false ""
  done
  capture_ps "$model cpu"

  unload_all
  log "  $model done, unloaded"
done

python3 "$HELPER" summary "$RESULTS" "$OUT_DIR/sysinfo.txt" "$OUT_DIR/summary.md"
log "All done in $((SECONDS / 60))m $((SECONDS % 60))s"
echo
echo "Summary: $OUT_DIR/summary.md"
echo "Raw data: $OUT_DIR/results.jsonl and $OUT_DIR/raw/"
