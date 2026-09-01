with open("fill.bin", "wb") as output:
    block = b"0" * (1024 * 1024)
    while True:
        output.write(block)
        output.flush()
