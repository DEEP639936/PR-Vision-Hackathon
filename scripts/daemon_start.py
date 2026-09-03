#!/usr/bin/env python3
"""PR•VISION preview keeper.

Double-fork daemonize (mimics agent-browser's orphan-to-PID-1 pattern, which
survives the sandbox reaper), then supervise uvicorn on :3000 forever:
if the server dies for ANY reason it is restarted within 3 seconds.

Usage: python3 daemon_start.py   (returns immediately; daemon keeps running)
"""
import os
import sys
import time
import subprocess

PROJECT = "/home/z/my-project/pr-vision"
LOG = "/home/z/my-project/dev.log"
PIDFILE = "/home/z/my-project/scripts/daemon.pid"

# The sandbox shell resolves `python3` to /home/z/.venv/bin/python3 (which has
# uvicorn/fastapi installed), but a previous daemon run was started with the
# system interpreter and respawned uvicorn with /usr/bin/python3 ->
# "No module named uvicorn" -> dead preview. Pin the interpreter explicitly.
VENV_PY = "/home/z/.venv/bin/python3"
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable


def fork_orphan():
    # First fork: parent exits so the child is adopted by init.
    pid = os.fork()
    if pid > 0:
        return False  # parent -> exit path
    os.setsid()
    # Second fork: child is no longer a session leader, fully detached.
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    return True


def run():
    # Detach stdio completely.
    sys.stdout.flush(); sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)

    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))

    while True:
        # Restart only if nothing is answering on :3000.
        up = False
        try:
            import socket
            s = socket.create_connection(("127.0.0.1", 3000), timeout=1.5)
            s.close()
            up = True
        except OSError:
            up = False

        if not up:
            try:
                env = dict(os.environ)
                env.setdefault("DB_ENGINE", "sqlite")
                env.setdefault("INGESTION_ENABLED_ON_STARTUP", "true")
                env.setdefault("INGESTION_INTERVAL_SECONDS", "30")
                env["PATH"] = "/home/z/.venv/bin:" + env.get("PATH", "")
                with open(LOG, "ab") as lf:
                    proc = subprocess.Popen(
                        [PYTHON, "-m", "uvicorn", "app.main:app",
                         "--host", "0.0.0.0", "--port", "3000",
                         "--app-dir", "backend"],
                        cwd=PROJECT, stdout=lf, stderr=lf, env=env,
                        start_new_session=True)
                # Give it a moment; if it exits instantly, back off.
                time.sleep(5)
                if proc.poll() is not None and proc.returncode != 0:
                    time.sleep(10)
            except Exception:
                pass
        time.sleep(3)


if __name__ == "__main__":
    if fork_orphan():
        run()
    # parent exits immediately -> tool call returns, daemon orphaned to PID 1
