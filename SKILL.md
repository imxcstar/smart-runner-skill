---
name: smart-runner
description: A watchdog wrapper for long-running tasks. Monitors stdout/stderr. Triggers AI immediately on IO wait (2s) or stall (30s silence). Reports periodically (5m) via heartbeat.
---

# Smart Runner

Runs a command in a PTY, monitoring its output stream. Input injection via named pipe (no session_id dependency).

- **IO_WAIT**: Output pauses >2s without ending in newline (e.g., `[y/N]:` prompt) → Triggers AI immediately
- **STALL**: No output for >30s → Triggers AI immediately
- **Heartbeat**: Every 5m → Triggers AI for periodic status check

## Usage

### 1. Start the Runner

```bash
python scripts/runner.py \
  --cmd "sh build.sh" \
  --name "Watchdog: Build Project" \
  --payload "[SmartRunner: Build Project]
Task: Running build.sh
Goal: Compile and test the project"
```

**Parameters:**
- `--cmd`: The command to execute (required)
- `--name`: Cron job name for identification (required)
- `--payload`: Task context for AI (required)

### 2. Runner Directory Structure

After starting, `.runner/` contains:
```
.runner/
├── status.json    # Current state (MONITORING/WAITING_FOR_AI/AI_DONE)
├── output.log     # Full stdout/stderr capture
├── runner.pid     # Runner process PID
└── input.pipe     # Named pipe for input injection
```

### 3. AI Handover Protocol

When woken by Cron:

1. **Check Status**: Read `.runner/status.json`
2. **Analyze**: Read `.runner/output.log` (last ~30 lines)
3. **Intervene** (via exec):
   - **Text Input**: `echo "y" > .runner/input.pipe`
   - **Special Keys**: `printf "
" > .runner/input.pipe` (Enter), `printf "\x03" > .runner/input.pipe` (Ctrl+C)
4. **Resume** (CRITICAL): Write `{"state": "AI_DONE"}` to `.runner/status.json`.
