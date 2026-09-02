import os
import sys

children = 0
while True:
    try:
        pid = os.fork()
    except BlockingIOError as error:
        print(f"fork refused after {children} children: {error}", flush=True)
        sys.exit(3)
    if pid == 0:
        while True:
            os.fork()
    children += 1
