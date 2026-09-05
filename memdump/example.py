#!/usr/bin/env python3
"""Minimal client for the 86Box memdump server.

Dumps guest physical memory (DEBUG-style by default, or raw with
--raw), or asserts a value onto the guest I/O bus. Requires a VM
running with "[General] memdump_port = <port>".

Usage:
    ./memdump-example.py [--host H] [--port P] [--raw] <address> [length]
    ./memdump-example.py [--host H] [--port P] --out[W|D] <port> <value>

Addresses are hex in any accepted form: absolute (ffff0, 0xFFFFF,
FFFFFh), or 8086 segment:offset (F000:FFF0). Length defaults to 128
bytes on the server side, just like DEBUG.

Examples:
    ./memdump-example.py --port 12348 ffff0 20        # DEBUG-style dump
    ./memdump-example.py --port 12348 F000:FFF6        # by segment:offset
    ./memdump-example.py --port 12348 --raw ffff0 20   # raw hex
    ./memdump-example.py --port 12348 --out 7F 02      # byte I/O write
    ./memdump-example.py --port 12348 --outW 61 00FF   # forced word
"""
import argparse
import socket
import sys


def command(host, port, line, raw=False):
    """Send one command; return the whole reply, prompt stripped.

    The server emits a "- " prompt when the connection opens and after
    every reply (DEBUG-style).  Raw replies end with one newline;
    DEBUG-style dumps end with an empty line, which is what we wait for
    when raw=False."""
    with socket.create_connection((host, port), timeout=5.0) as sock:
        sock.sendall(line.encode("ascii") + b"\n")
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            # Stop at the next DEBUG-style prompt, or at the reply's own
            # terminator (raw ends with "\n", pretty with an empty line).
            if data.endswith(b"- "):
                break
            if raw and data.endswith(b"\n"):
                break
            if (not raw) and data.endswith(b"\n\n"):
                break
    text = data.decode("ascii")
    if text.startswith("- "):
        text = text[2:]
    while text.endswith("- "):
        text = text[:-2]
    return text


def hexdump(data, base):
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in row)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"{base + off:08x}  {hexpart:<47}  |{asciipart}|")


def main():
    parser = argparse.ArgumentParser(description="86Box memdump client example")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12348)
    parser.add_argument("--raw", action="store_true",
                        help="raw hex dump (dr) instead of DEBUG-style (d)")
    parser.add_argument("--out", nargs=2, metavar=("PORT", "VALUE"),
                        help="assert VALUE onto I/O PORT (hex; width sensed)")
    parser.add_argument("--outW", nargs=2, metavar=("PORT", "VALUE"),
                        help="assert VALUE as a word (ow)")
    parser.add_argument("--outD", nargs=2, metavar=("PORT", "VALUE"),
                        help="assert VALUE as a dword (od)")
    parser.add_argument("address", nargs="?",
                        help="guest address, hex (ffff0 / 0xFFFFF / F000:FFF0)")
    parser.add_argument("length", nargs="?",
                        help="byte count, hex, <= 1000h (default: server's 128)")
    args = parser.parse_args()

    try:
        for flag, verb in (("--outW", "ow"), ("--outD", "od"), ("--out", "o")):
            val = getattr(args, flag[2:])
            if val:
                reply = command(args.host, args.port,
                                f"{verb} {val[0]} {val[1]}", raw=True).strip()
                print(reply or "<closed>")
                return 0 if reply == "ok" else 1

        if not args.address:
            parser.error("an address is required unless --out is given")

        line = f"{'dr' if args.raw else 'd'} {args.address}"
        if args.length:
            line += f" {args.length}"
        reply = command(args.host, args.port, line, raw=args.raw)
    except (OSError, ValueError) as err:
        print(f"memdump-example: {err}", file=sys.stderr)
        return 1

    if args.raw:
        data = bytes.fromhex(reply.strip())

        # For display, resolve segment:offset forms the way the server does.
        if ":" in args.address:
            seg, off = args.address.split(":", 1)
            base = (int(seg.rstrip("hH").removeprefix("0x"), 16) << 4) \
                + int(off.rstrip("hH").removeprefix("0x"), 16)
        else:
            base = int(args.address.rstrip("hH").removeprefix("0x"), 16)

        hexdump(data, base)
    else:
        sys.stdout.write(reply if reply.endswith("\n") else reply + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
