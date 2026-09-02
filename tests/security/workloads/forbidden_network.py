import socket
import sys
from urllib.request import urlopen

try:
    urlopen("https://example.com", timeout=5)
except Exception as error:  # noqa: BLE001
    print(f"blocked: {type(error).__name__}: {error}", flush=True)
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
    except OSError as raw:
        print(f"raw socket blocked: {raw}", flush=True)
        sys.exit(4)
print("NETWORK REACHABLE", flush=True)
sys.exit(0)
