# Local model benchmark results

## System
```
Date: Thu  1 Oct 21:21:06 BST 2026
PRETTY_NAME="Bazzite"
Kernel: 7.2.4-ogc3.1.fc44.x86_64
CPU: Intel(R) Core(TM) i5-9400F CPU @ 2.90GHz
               total        used        free      shared  buff/cache   available
Mem:            15Gi       2.9Gi       5.9Gi       106Mi       7.0Gi        12Gi
GPU (name, driver, total, used before test): NVIDIA GeForce RTX 2060, 615.71.09, 6144 MiB, 673 MiB
Ollama: ollama version is 0.35.0
```

## Speed (thinking off, temperature 0)

| Model | Size | Default split (CPU/GPU) | Cold start total | Cold load | GPU gen tok/s | GPU prompt tok/s | CPU-only gen tok/s | CPU-only prompt tok/s | CPU-only check |
|---|---|---|---|---|---|---|---|---|---|
| lfm2.5 | 5.36 GB | 24/76 | 22.0s | 14.908s | 74.13 | 1606.37 | 24.57 | 530.2 | 100% CPU |
| qwen3.5:4b | 3.13 GB | 0/100 | 14.114s | 10.413s | 65.14 | 874.47 | 8.97 | 284.39 | 100% CPU |

## Thinking behaviour

| Model | Thought anyway with think:false | Avg tokens/answer (no system prompt) | Avg tokens/answer (Bazzite prompt) | Test 1 tokens: think off -> on |
|---|---|---|---|---|
| lfm2.5 | 23/23 | 282 | 459 | 120 -> 120 |
| qwen3.5:4b | 0/23 | 13 | 10 | 4 -> 447 |

## Accuracy

Thinking is stripped and answers are cut to 300 characters. Grade each one: correct / works but not ideal / wrong.

### Test 1: Free disk space

Prompt: Show free space on all mounted drives in human-readable form. Reply with only the command.  
Expected: `df -h`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | df -h | 120 | 1.627s |
| lfm2.5 | Bazzite system prompt | df -h | 153 | 2.158s |
| qwen3.5:4b | no system prompt | df -h | 4 | 0.116s |
| qwen3.5:4b | Bazzite system prompt | df -h | 4 | 0.24s |

### Test 2: Top memory processes

Prompt: Show the 10 processes using the most memory. Reply with only the command.  
Expected: `ps aux --sort=-%mem | head -n 11`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | ps -eo comm,rss,pid \| sort -k2 -nr \| head -n 10 | 353 | 4.564s |
| lfm2.5 | Bazzite system prompt | ps -eo comm,pcpu,mems,pid,schedid \| sort -t, -k3 -nr \| head -n 10 | 308 | 4.17s |
| qwen3.5:4b | no system prompt | ps -eo pid,comm,rss --sort=-rss \| head -n 10 | 20 | 0.396s |
| qwen3.5:4b | Bazzite system prompt | ps aux --sort=-%mem \| head -n 11 | 15 | 0.355s |

### Test 3: Last 50 journal lines

Prompt: Show the last 50 lines of the system journal. Reply with only the command.  
Expected: `journalctl -n 50`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | journalctl -n 50 | 167 | 2.219s |
| lfm2.5 | Bazzite system prompt | journalctl -n 50 | 375 | 5.039s |
| qwen3.5:4b | no system prompt | tail -n 50 /var/log/syslog | 12 | 0.278s |
| qwen3.5:4b | Bazzite system prompt | journalctl -b \| tail -n 50 | 12 | 0.311s |

### Test 4: Restart Sunshine

Prompt: Restart the Sunshine service, which runs as a user service. Reply with only the command.  
Expected: `systemctl --user restart sunshine`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | sudo -u $(whoami) systemctl restart sunshine | 291 | 3.942s |
| lfm2.5 | Bazzite system prompt | systemctl --user restart sunshine | 247 | 3.34s |
| qwen3.5:4b | no system prompt | sudo systemctl restart sunshined | 7 | 0.204s |
| qwen3.5:4b | Bazzite system prompt | systemctl --user restart sunshined | 9 | 0.268s |

### Test 5: Install Firefox on Bazzite

Prompt: Install Firefox on Bazzite, an immutable Fedora-based distro. Reply with only the command.  
Expected: `flatpak install flathub org.mozilla.firefox`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | sudo dnf install firefox | 157 | 2.226s |
| lfm2.5 | Bazzite system prompt | flatpak install --deploy-system firefox | 718 | 9.293s |
| qwen3.5:4b | no system prompt | sudo dnf install firefox | 6 | 0.183s |
| qwen3.5:4b | Bazzite system prompt | flatpak install flathub org.mozilla.firefox | 11 | 0.297s |

### Test 6: Find files over 1GB

Prompt: Find all files larger than 1GB in my home directory. Reply with only the command.  
Expected: `find ~ -type f -size +1G`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | find /home -type f -size +1G | 155 | 2.203s |
| lfm2.5 | Bazzite system prompt | find "$HOME" -type f -size +1G | 233 | 3.133s |
| qwen3.5:4b | no system prompt | ls -lh ~/ \| awk '$5 > 1073741824' | 22 | 0.442s |
| qwen3.5:4b | Bazzite system prompt | find ~ -type f -size +1G | 11 | 0.301s |

### Test 7: Process on a port

Prompt: Show which process is listening on TCP port 47989. Reply with only the command.  
Expected: `ss -tlnp | grep 47989  (sudo needed to see the process name)`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | ss -tlnp \| grep ':47989' | 211 | 2.915s |
| lfm2.5 | Bazzite system prompt | ss -tlnp \| grep ':47989' | 241 | 3.256s |
| qwen3.5:4b | no system prompt | netstat -tunp \| grep :47989 | 15 | 0.319s |
| qwen3.5:4b | Bazzite system prompt | sudo ss -tlnp \| grep 47989 | 15 | 0.36s |

### Test 8: Update Bazzite

Prompt: Update the whole system on Bazzite. Reply with only the command.  
Expected: `ujust update  (or rpm-ostree upgrade)`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | What specific system or process on Bazzite do you want to update? | 457 | 6.263s |
| lfm2.5 | Bazzite system prompt | /usr/bin/rpm -U --directory=/var/lib/rpm/ | 1535 | 20.053s |
| qwen3.5:4b | no system prompt | sudo dnf update --refresh && sudo dnf upgrade --all ``` | 16 | 0.333s |
| qwen3.5:4b | Bazzite system prompt | ujust update | 4 | 0.187s |

### Test 9: Empty Downloads safely

Prompt: Delete everything inside my Downloads folder, but not the folder itself. Reply with only the command.  
Expected: `rm -rf ~/Downloads/*  (must not touch ~ or remove the folder itself)`

| Model | Setup | Answer | Tokens | Time |
|---|---|---|---|---|
| lfm2.5 | no system prompt | rm -rf ~/Downloads/* | 630 | 8.533s |
| lfm2.5 | Bazzite system prompt | rm -rf ~/Downloads/* | 325 | 4.336s |
| qwen3.5:4b | no system prompt | rmdir /s "C:\Users\%USERNAME%\Downloads" | 15 | 0.33s |
| qwen3.5:4b | Bazzite system prompt | rm -rf ~/Downloads/* | 7 | 0.234s |

