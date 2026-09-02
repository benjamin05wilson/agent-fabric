"""Reports what the sandbox actually looks like from inside."""

import json
import os
import platform

report = {"uid": os.getuid(), "gid": os.getgid(), "kernel": platform.release()}
with open("/proc/self/status") as status:
    for line in status:
        if line.startswith(("CapEff", "CapBnd", "NoNewPrivs")):
            key, value = line.split(":", 1)
            report[key] = value.strip()
for path in ("/etc/probe", "/usr/probe", "/probe"):
    try:
        with open(path, "w") as handle:
            handle.write("x")
        report[f"write:{path}"] = "WRITABLE"
    except OSError as error:
        report[f"write:{path}"] = f"denied ({error.errno})"
try:
    with open("/workspace/probe", "w") as handle:
        handle.write("x")
    report["write:/workspace"] = "writable"
except OSError as error:
    report["write:/workspace"] = f"denied ({error.errno})"
report["docker_socket"] = os.path.exists("/var/run/docker.sock")
report["devices"] = sorted(os.listdir("/dev"))[:20]
try:
    with open("/proc/1/cmdline", "rb") as handle:
        report["pid1"] = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
except OSError as error:
    report["pid1"] = str(error)
print(json.dumps(report, indent=2, sort_keys=True))
