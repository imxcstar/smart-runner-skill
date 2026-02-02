#!/usr/bin/env python3
"""
SmartRunner - Self-contained PTY monitor with input gate.

Features:
- Monitors stdout/stderr for IO_WAIT (2s) and STALL (30s)
- Triggers AI via Cron on anomalies or periodic heartbeat (5m)
- Input injection via named pipe
"""

import os
import sys
import time
import json
import pty
import select
import subprocess
import argparse
import signal
import threading
import stat

class SmartRunner:
    def __init__(self, cmd, cron_name, cron_payload, working_dir):
        self.cmd = cmd
        self.cron_name = cron_name
        self.cron_payload = cron_payload
        self.working_dir = working_dir
        
        self.runner_dir = os.path.join(self.working_dir, ".runner")
        self.status_file = os.path.join(self.runner_dir, "status.json")
        self.log_file = os.path.join(self.runner_dir, "output.log")
        self.pid_file = os.path.join(self.runner_dir, "runner.pid")
        self.input_pipe = os.path.join(self.runner_dir, "input.pipe")
        
        self.cron_id = None
        self.process = None
        self.master_fd = None
        self.running = True
        
        # State
        self.last_activity_time = time.time()
        self.last_log_chunk = ""
        self.state = "MONITORING"
        
        # Constants
        self.STALL_TIMEOUT = 30.0
        self.IO_WAIT_TIMEOUT = 2.0

    def setup_dirs(self):
        """Initialize runner directory and named pipe."""
        if os.path.exists(self.runner_dir):
            import shutil
            shutil.rmtree(self.runner_dir)
        
        os.makedirs(self.runner_dir)
        
        # Create named pipe for input injection
        os.mkfifo(self.input_pipe)
        os.chmod(self.input_pipe, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
        
        # Save PID
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))
            
        self.update_status("MONITORING", info="Starting up")

    def update_status(self, state, reason=None, info=None):
        """Update status file for AI to read."""
        self.state = state
        data = {
            "state": state,
            "updated_at": time.time(),
            "runner_pid": os.getpid(),
            "child_pid": self.process.pid if self.process else None,
            "cmd": self.cmd,
            "input_pipe": self.input_pipe
        }
        if reason:
            data["reason"] = reason
        if info:
            data["info"] = info
        
        with open(self.status_file, 'w') as f:
            json.dump(data, f, indent=2)

    def log_output(self, text):
        """Append output to log file."""
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(text)

    def run_openclaw_cmd(self, args):
        """Run an openclaw CLI command and return output."""
        try:
            openclaw_path = "/home/xcssa/.local/share/fnm/node-versions/v24.13.0/installation/bin/openclaw"
            cmd = [openclaw_path] + args
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip()
        except Exception as e:
            print(f"[Runner] Error running openclaw cmd: {e}")
            return None

    def setup_cron(self):
        """Setup cron job for AI triggering."""
        print(f"[Runner] Setting up cron job: {self.cron_name}")
        
        # Cleanup existing job by name
        try:
            list_output = self.run_openclaw_cmd(["cron", "list", "--json"])
            if list_output:
                jobs_data = json.loads(list_output)
                jobs = jobs_data if isinstance(jobs_data, list) else jobs_data.get("jobs", [])
                for job in jobs:
                    if job.get("name") == self.cron_name:
                        job_id = job.get("id") or job.get("jobId")
                        print(f"[Runner] Removing old cron job: {job_id}")
                        self.run_openclaw_cmd(["cron", "rm", job_id])
        except Exception as e:
            print(f"[Runner] Error during cron cleanup: {e}")

        # Build payload with process info and instructions
        process_info = f"""
---
[Process Info]
- Child PID: {self.process.pid}
- Runner PID: {os.getpid()}
- Working Dir: {self.working_dir}
- Runner Dir: {self.runner_dir}
- Input Pipe: {self.input_pipe}"""

        system_instructions = f"""
---
[SmartRunner Instructions]
1. CHECK STATUS: Read {self.status_file}
   - "MONITORING": Heartbeat only. Report progress if needed.
   - "WAITING_FOR_AI": 🚨 ACTION REQUIRED!

2. ANALYZE: Read {self.log_file} (last ~30 lines)

3. INTERVENE (If needed):
   - To send input: exec 'echo "your input" > {self.input_pipe}'
   - To send special keys: exec 'printf "\\n" > {self.input_pipe}' (newline)
   - To send Ctrl+C: exec 'printf "\\x03" > {self.input_pipe}'
   - To kill process: exec 'kill {self.process.pid}'

4. RESUME (CRITICAL):
   Write {{"state": "AI_DONE"}} to {self.status_file} after handling.
   Without this, the runner stays paused!
"""
        full_payload = self.cron_payload + process_info + system_instructions
        
        add_cmd = [
            "cron", "add",
            "--name", self.cron_name,
            "--every", "5m",
            "--session", "main",
            "--system-event", full_payload,
            "--json"
        ]
        
        output = self.run_openclaw_cmd(add_cmd)
        if output:
            try:
                job_data = json.loads(output)
                self.cron_id = job_data.get("id") or job_data.get("jobId")
                print(f"[Runner] Cron job created: {self.cron_id}")
            except:
                print(f"[Runner] Failed to parse cron output: {output}")
        
        if not self.cron_id:
            print("[Runner] WARNING: Failed to setup cron job!")

    def trigger_ai(self, reason):
        """Trigger AI intervention and wait for response."""
        if self.state == "WAITING_FOR_AI":
            return
            
        print(f"[Runner] Triggering AI. Reason: {reason}")
        self.update_status("WAITING_FOR_AI", reason=reason)
        
        if self.cron_id:
            self.run_openclaw_cmd(["cron", "run", "--force", self.cron_id])
        else:
            print("[Runner] Cannot trigger AI: No Cron ID")

        # Wait for AI response while draining PTY output
        print("[Runner] Waiting for AI...")
        while self.running:
            # Keep draining output
            if self.master_fd is not None:
                try:
                    r, _, _ = select.select([self.master_fd], [], [], 0.5)
                    if self.master_fd in r:
                        data = os.read(self.master_fd, 10240)
                        if data:
                            text = data.decode('utf-8', errors='replace')
                            sys.stdout.write(text)
                            sys.stdout.flush()
                            self.log_output(text)
                except OSError:
                    pass
            else:
                time.sleep(0.5)
            
            # Check if AI responded
            try:
                with open(self.status_file, 'r') as f:
                    data = json.load(f)
                    if data.get("state") == "AI_DONE":
                        print("[Runner] AI finished. Resuming...")
                        self.update_status("MONITORING", info="Resumed after AI intervention")
                        self.last_activity_time = time.time()
                        self.last_log_chunk = ""
                        break
            except:
                pass

    def input_gate_thread(self):
        """
        Monitor named pipe for input injection.
        Runs in a separate thread, forwarding pipe data to child PTY.
        """
        print(f"[Runner] Input gate listening: {self.input_pipe}")
        while self.running:
            try:
                # Open pipe in read mode (blocks until writer connects)
                with open(self.input_pipe, 'r') as pipe:
                    while self.running:
                        data = pipe.read(1024)
                        if not data:
                            break  # Writer closed, reopen pipe
                        if self.master_fd is not None:
                            os.write(self.master_fd, data.encode('utf-8'))
                            print(f"[Runner] Injected input: {repr(data)}")
            except Exception as e:
                if self.running:
                    print(f"[Runner] Input gate error: {e}")
                    time.sleep(0.5)

    def run(self):
        """Main entry point."""
        self.setup_dirs()
        
        # Create PTY
        self.master_fd, slave_fd = pty.openpty()
        
        print(f"[Runner] Starting: {self.cmd}")
        self.process = subprocess.Popen(
            self.cmd,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid
        )
        os.close(slave_fd)
        
        # Update status with child PID
        self.update_status("MONITORING", info="Process started")
        
        # Setup cron in background (slow CLI call)
        cron_thread = threading.Thread(target=self.setup_cron, daemon=True)
        cron_thread.start()
        
        # Start input gate thread
        input_thread = threading.Thread(target=self.input_gate_thread, daemon=True)
        input_thread.start()
        
        print(f"[Runner] Monitoring PID {self.process.pid}...")
        self.last_activity_time = time.time()
        
        try:
            while self.process.poll() is None:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
                
                if self.master_fd in r:
                    try:
                        data = os.read(self.master_fd, 10240)
                    except OSError:
                        break
                        
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        sys.stdout.write(text)
                        sys.stdout.flush()
                        self.log_output(text)
                        
                        self.last_activity_time = time.time()
                        self.last_log_chunk = text
                
                # Check timers
                now = time.time()
                elapsed = now - self.last_activity_time
                
                # IO Wait: short timeout + no trailing newline
                if elapsed > self.IO_WAIT_TIMEOUT and self.last_log_chunk and not self.last_log_chunk.endswith('\n'):
                    self.trigger_ai("IO_WAIT")
                    self.last_log_chunk = ""
                
                # Stall: long timeout
                elif elapsed > self.STALL_TIMEOUT:
                    self.trigger_ai("STALL")
                    self.last_activity_time = time.time()
                    
        except KeyboardInterrupt:
            print("\n[Runner] Interrupted")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources."""
        self.running = False
        
        if self.process and self.process.poll() is None:
            print("[Runner] Killing child process...")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except:
                pass
        
        if self.cron_id:
            print(f"[Runner] Removing cron job: {self.cron_id}")
            self.run_openclaw_cmd(["cron", "rm", self.cron_id])
        
        # Clean up named pipe
        if os.path.exists(self.input_pipe):
            try:
                os.unlink(self.input_pipe)
            except:
                pass
            
        print("[Runner] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmartRunner - PTY monitor with input gate")
    parser.add_argument("--cmd", required=True, help="Command to run")
    parser.add_argument("--name", required=True, help="Cron job name for AI triggering")
    parser.add_argument("--payload", required=True, help="Task description for AI context")
    args = parser.parse_args()
    
    runner = SmartRunner(args.cmd, args.name, args.payload, os.getcwd())
    runner.run()
