"""Run the local application without exposing it on the network."""
from pathlib import Path
import argparse
import os
import sys
import threading
import webbrowser
import socket
import json
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

parser = argparse.ArgumentParser(description="Footprint local application")
parser.add_argument("--open", action="store_true", help="Open the application in your browser")
args = parser.parse_args()

port = int(os.environ.get("FOOTPRINT_PORT", "8765"))
ports = [port] if "FOOTPRINT_PORT" in os.environ else [8765, 8766]
for port in ports:
    try:
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", port))
        break
    except OSError:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                existing = json.load(response)
            if existing.get("application") == "footprint":
                url = f"http://127.0.0.1:{port}"
                print(f"Footprint is already running: {url}")
                if args.open:
                    webbrowser.open(url)
                sys.exit(0)
        except (OSError, ValueError, AttributeError):
            pass
else:
    print("Local port is already in use. Set FOOTPRINT_PORT to another port.")
    sys.exit(1)
os.environ["FOOTPRINT_PORT"] = str(port)
url = f"http://127.0.0.1:{port}"

if not (ROOT / "dist" / "index.html").exists():
    print("Please run npm run build first, or use start.command.")
    sys.exit(1)

if args.open:
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

print(f"\nFootprint: {url}\nPress Ctrl+C to stop.\n", flush=True)
import uvicorn
uvicorn.run("backend.app:app", host="127.0.0.1", port=port, log_level="info")
