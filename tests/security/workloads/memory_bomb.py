chunks = []
while True:
    chunks.append(bytearray(64 * 1024 * 1024))
    print(f"allocated {len(chunks) * 64} MiB", flush=True)
