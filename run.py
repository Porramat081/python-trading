#!/usr/bin/env python
"""Start everything with one command: migrate, then the web server + strategy scheduler.

    python run.py                      # http://127.0.0.1:8000, day-trading loop per Strategy settings
    python run.py --port 8080 --interval 1
    python run.py --no-scheduler       # web server only

Ctrl+C stops both processes. If either one exits, the other is stopped too.
Works on macOS, Linux and Windows. Uses the project's .venv automatically if it exists.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_venv():
    """Re-run this script with the project's virtualenv Python if it isn't active."""
    if VENV_PY.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        if os.name == "nt":
            sys.exit(subprocess.call([str(VENV_PY), *sys.argv]))
        os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])


def manage(*args):
    return [sys.executable, "-u", str(ROOT / "manage.py"), *args]


def stream(proc, label, color):
    """Print a child's output with a coloured [label] prefix."""
    prefix = f"\033[{color}m[{label}]\033[0m " if sys.stdout.isatty() else f"[{label}] "
    for line in proc.stdout:
        sys.stdout.write(prefix + line)
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Run the Algo Trader web server and scheduler together.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8000")
    parser.add_argument(
        "--interval", type=int, help="Override minutes between day-trading loops (default: Strategy settings)."
    )
    parser.add_argument("--ignore-market-hours", action="store_true", help="Scheduler runs 24/7.")
    parser.add_argument("--no-scheduler", action="store_true", help="Start only the web server.")
    parser.add_argument("--skip-migrate", action="store_true")
    opts = parser.parse_args()

    os.chdir(ROOT)
    if not (ROOT / ".env").exists():
        print("Warning: no .env file found. Copy .env.example to .env and add your API keys.\n")

    if not opts.skip_migrate:
        print("Applying database migrations...")
        if subprocess.call(manage("migrate", "--noinput")) != 0:
            sys.exit("Migration failed; not starting.")

    commands = {"web": (manage("runserver", f"{opts.host}:{opts.port}", "--noreload"), "36")}
    if not opts.no_scheduler:
        sched = manage("run_scheduler")
        if opts.interval:
            sched += ["--interval", str(opts.interval)]
        if opts.ignore_market_hours:
            sched.append("--ignore-market-hours")
        commands["scheduler"] = (sched, "35")

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    procs = {}
    for label, (cmd, color) in commands.items():
        procs[label] = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
        )
        threading.Thread(target=stream, args=(procs[label], label, color), daemon=True).start()

    print(f"\nAlgo Trader running at http://{opts.host}:{opts.port}/  (Ctrl+C to stop)\n")

    exit_code = 0
    try:
        while True:
            for label, proc in procs.items():
                code = proc.poll()
                if code is not None:
                    print(f"\n[{label}] exited with code {code}; stopping everything.")
                    exit_code = code or 1
                    raise KeyboardInterrupt
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
        deadline = time.time() + 10
        for proc in procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Stopped.")
    sys.exit(exit_code)


if __name__ == "__main__":
    ensure_venv()
    main()
