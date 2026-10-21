"""Open the served workbench; reuse a running instance or keep a new server in this console."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def ready(base_url):
    # Local traffic must not be sent through a configured system HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url + "/api/config", timeout=1) as response:
            config = json.load(response)
        if "RepairLab" not in config.get("name", ""):
            return False
        with opener.open(base_url + "/", timeout=1) as response:
            html = response.read()
        return b'RepairLab' in html and b'id="fixture"' in html
    except (OSError, ValueError, urllib.error.URLError):
        return False


def show(base_url, no_browser):
    print(f"[RepairLab] Ready: {base_url}/", flush=True)
    if not no_browser:
        try:
            if not webbrowser.open(base_url + "/", new=2):
                print("[RepairLab] Open this address in your browser manually.", flush=True)
        except OSError:
            print("[RepairLab] Browser launch failed; open the address above manually.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Start RepairLab and open the actual HTTP workbench.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    base_url = f"http://127.0.0.1:{args.port}"
    if ready(base_url):
        print("[RepairLab] Reusing the existing workbench.", flush=True)
        show(base_url, args.no_browser)
        return 0
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"[RepairLab] Port {args.port} belongs to another service or an unhealthy instance."
                  " Choose another -Port, or close that instance first.", file=sys.stderr)
            return 1
    print(f"[RepairLab] Starting workbench at {base_url}/", flush=True)
    print("[RepairLab] Keep this console open. Press Ctrl+C to stop the server.", flush=True)
    server = subprocess.Popen([sys.executable, "-m", "repairlab.cli", "serve", "--port", str(args.port)], cwd=ROOT)
    stop = threading.Event()

    def wait_until_ready():
        deadline = time.monotonic() + 45
        while not stop.is_set() and server.poll() is None:
            if ready(base_url):
                show(base_url, args.no_browser)
                return
            if time.monotonic() >= deadline:
                print("[RepairLab] Startup did not become ready. Check the server messages in this console.", flush=True)
                return
            stop.wait(0.3)

    watcher = threading.Thread(target=wait_until_ready, daemon=True)
    watcher.start()
    try:
        return server.wait()
    except KeyboardInterrupt:
        # The console interrupt is also delivered to uvicorn, allowing its normal shutdown.
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate()
            server.wait(timeout=10)
        return 0
    finally:
        stop.set()
        watcher.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
