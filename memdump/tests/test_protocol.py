#!/usr/bin/env python3
"""Protocol conformance suite for the 86Box memdump server.

Covers every command: DEBUG-style dumps (d), raw dumps (dr), I/O
output (o/ow/od), quit, help, the "- " prompt, and " ^ Error" errors.

Prerequisites: an 86Box (JoshRodd/86Box-contributions memdump branch or
newer) with "[General] memdump_port = <port>" set, and a VM running
that you can read known bytes from.  The checks below expect an IBM PC
(5150) with the 19OCT81 BIOS for the reset-vector/date-string bytes;
other guests will fail the content-specific checks.

Usage:
    python3 test_protocol.py [host] [port]      (defaults: 127.0.0.1 12349)
"""
import re
import socket
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 12349

failures = 0


def cmd(line):
    """Send a command; return the reply with the DEBUG prompt stripped.

    Collects until 0.35 s of silence, so multi-line replies (pretty
    dumps, help) arrive in full; the trailing prompt is stripped."""
    with socket.create_connection((HOST, PORT), timeout=5.0) as s:
        s.settimeout(0.35)
        s.sendall(line.encode() + b"\n")
        data = b""
        try:
            while True:
                data += s.recv(65536)
        except socket.timeout:
            pass
    text = data.decode()
    if text.startswith("- "):
        text = text[2:]
    if text.endswith("- "):
        text = text[:-2]
    return text


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" ({detail!r})" if not ok else ""))
    if not ok:
        failures += 1


LINE_RE = re.compile(
    r"^([0-9A-F]{8}|[0-9A-F]{4}:[0-9A-F]{4})  "
    r"([0-9A-F]{2} ){7}[0-9A-F]{2}-([0-9A-F]{2} ){7}[0-9A-F]{2}   [ -~]+$"
)

HELP_TEXT = (
    "dump                   D address [length]\n"
    "dump raw               DR address [length]\n"
    "output                 O[W|D] port value\n"
    "quit                   Q\n"
    "help                   ?\n"
)

# prompt is sent on connect, before anything else
with socket.create_connection((HOST, PORT), timeout=5.0) as s:
    s.settimeout(1.0)
    check("prompt on connect", s.recv(65536) == b"- ")

# segmented dump: two lines (20 bytes), labels SEG:OFF, offset wraps mod 64K
d = [x for x in cmd("d F000:FFF0 20").split("\n") if x]
check("two lines", len(d) == 2, d)
check("line 1 label F000:FFF0", d[0].startswith("F000:FFF0  "), d[0])
check("line 2 label F000:0000 (offset wrap)", d[1].startswith("F000:0000  "), d[1])
check("line shape", all(LINE_RE.match(x) for x in d), d)
check("dash between bytes 8 and 9", d[0][11 + 23] == "-", d[0][11 + 23])

# linear dump: labels are 8-digit linear addresses
d = [x for x in cmd("d ffff0 20").split("\n") if x]
check("linear labels", d[0].startswith("000FFFF0  ") and d[1].startswith("00100000  "), d)

# bytes in d match dr (uppercase pretty vs lowercase raw)
pretty = [x for x in cmd("d F000:FFF5 8").split("\n") if x][0]
raw = cmd("dr F000:FFF5 8").strip()
hexpart = pretty[11:11 + 23].replace(" ", "")
check("d bytes == dr bytes", hexpart.lower() == raw, f"{hexpart.lower()} vs {raw}")

# ASCII column: reset vector + date (segmented line: 9 addr + 2 + 47 + 3)
check("ASCII column", pretty[11 + 47 + 3:] == "10/19/81", pretty[11 + 47 + 3:])

# dr keeps the raw behavior: default length 128, clamp at 0x1000
check("dr default 128", len(cmd("dr 0").strip()) == 256)
check("dr clamped", len(cmd("dr 0 2000").strip()) == 0x2000)

# partial line keeps columns aligned: 10 addr + 47 hex + 3 + 5 ASCII
line = [x for x in cmd("d ffff0 5").split("\n") if x][0]
check("partial line padded", len(line) == 10 + 47 + 3 + 5 and line.endswith(".[..."), repr(line))

# errors use the DEBUG-style " ^ Error"
for bad in ("b8000:fa0", "d 0xZZ 4", "d", "d 0 10 20", "", "o 61"):
    check(f"error format {bad!r}", cmd(bad).strip("\n") == " ^ Error", cmd(bad))

# o / ow / od accept
check("o byte -> ok", cmd("o 61 03").strip() == "ok")
check("ow forced -> ok", cmd("ow 61 0003").strip() == "ok")
check("od forced -> ok", cmd("od 61 0003").strip() == "ok")

# help: full table, including dump raw and the width overrides
check("? help text", cmd("?").strip("\n") == HELP_TEXT.strip("\n"), cmd("?"))

sys.exit(1 if failures else 0)
