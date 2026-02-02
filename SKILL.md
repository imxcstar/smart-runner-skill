---
name: smart-runner
description: A watchdog wrapper for long-running tasks. Monitors stdout/stderr. Triggers AI immediately on IO wait (2s) or stall (30s silence). Reports periodically (5m) via heartbeat.
---

# Smart Runner

Runs a command in a PTY, monitoring its output stream. Input injection via named pipe.

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

**Notes:**
- `.runner/` directory is created in the current working directory
- `--payload` should contain task context only (runner auto-appends system instructions)

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
   - `MONITORING`: Cron heartbeat only, report progress if needed
   - `WAITING_FOR_AI`: 🚨 Action required!

2. **Analyze**: Read `.runner/output.log` (last ~30 lines)
   - **IO_WAIT**: Process waiting for input
   - **STALL**: No output for 30s, check if error/hang

3. **Intervene** (via exec):

   **⚠️ IMPORTANT: Input Injection Reference**
   
   ```bash
   # TEXT INPUT
   echo "y" > .runner/input.pipe          # Send "y" + newline
   echo -n "hello" > .runner/input.pipe   # Send only "hello", no newline
   
   # SPECIAL KEYS - Use printf
   printf "
" > .runner/input.pipe       # Enter
   printf "\x03" > .runner/input.pipe     # Ctrl+C
   printf "\x1b[B" > .runner/input.pipe   # Down Arrow
   ```

4. **Resume** (CRITICAL): Write to `.runner/status.json`:
   ```json
   {"state": "AI_DONE", "updated_at": <timestamp>}
   ```
   Without this, the runner stays paused!
