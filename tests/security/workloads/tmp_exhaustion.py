written = 0
with open("/tmp/fill.bin", "wb") as output:
    block = b"0" * (1024 * 1024)
    while True:
        output.write(block)
        output.flush()
        written += 1
        if written % 16 == 0:
            print(f"wrote {written} MiB to /tmp", flush=True)
