#!/usr/bin/env python3
"""Test program for 86Box LPT 8-bit strobe mode.

Connects to an 86Box UNIX named pipe (lpt_mode=3) and exercises the
handshake protocol: writes data bytes, asserts STROBE by toggling the
control register, and reads back status bytes from the pipe reader.

Usage:
    1. Configure 86Box with LPT1 as a named pipe, lpt_mode=3.
    2. Start the VM (this creates the FIFOs).
    3. Run this script with the pipe base path:
           python3 test_lpt_strobe.py /tmp/my-printer

The script writes a short test pattern and verifies that the pipe reader
responds with the expected ACK sequence.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from pathlib import Path

# Protocol constants — must match 86Box and fifo_printer.py
READY_STATUS = 0x0B       # nAck high, Select, not Busy
ACK_LOW_STATUS = 0x03     # nAck low, Select, not Busy
ACK_PULSE_SECONDS = 0.000001  # 1 µs, matches a real printer's ACK pulse

# Test pattern: recognizable ASCII that survives CR/LF translation
TEST_PATTERN = b"86Box LPT strobe test\r\nLine 2\r\n\x0C"  # includes form feed


def require_fifos(pipe_base: Path) -> tuple[Path, Path]:
    """Return (data_fifo, status_fifo) paths, raising if either is missing."""
    data_fifo = pipe_base.parent / f"{pipe_base.name}.out"
    status_fifo = pipe_base.parent / f"{pipe_base.name}.in"
    for name, path in [("data", data_fifo), ("status", status_fifo)]:
        if not path.exists():
            raise FileNotFoundError(
                f"{name} FIFO not found: {path}\n"
                f"Start the 86Box VM first so it creates the FIFOs."
            )
        if not stat.S_ISFIFO(path.stat().st_mode):
            raise TypeError(f"{name} path is not a FIFO: {path}")
    return data_fifo, status_fifo


def wait_for_status(status_fifo: Path, timeout: float = 5.0) -> int:
    """Read one status byte from the status FIFO with a timeout."""
    deadline = time.monotonic() + timeout
    fd = os.open(str(status_fifo), os.O_RDONLY | os.O_NONBLOCK)
    try:
        while time.monotonic() < deadline:
            try:
                data = os.read(fd, 1)
                if data:
                    return data[0]
            except BlockingIOError:
                pass
            time.sleep(0.001)
        raise TimeoutError(f"No status byte within {timeout}s")
    finally:
        os.close(fd)


def send_byte_and_check(
    data_fifo_fd: int,
    status_fifo: Path,
    byte: bytes,
    label: str,
) -> bool:
    """Write one byte, wait for ACK, verify status sequence.

    Returns True if the handshake looks correct.
    """
    os.write(data_fifo_fd, byte)

    # The pipe reader should respond with ACK_LOW then READY
    first = wait_for_status(status_fifo)
    second = wait_for_status(status_fifo)

    ok = True
    if first != ACK_LOW_STATUS:
        print(f"  {label}: expected ACK_LOW (0x{ACK_LOW_STATUS:02X}), "
              f"got 0x{first:02X}")
        ok = False
    if second != READY_STATUS:
        print(f"  {label}: expected READY (0x{READY_STATUS:02X}), "
              f"got 0x{second:02X}")
        ok = False
    if ok:
        print(f"  {label}: ACK OK (byte 0x{byte[0]:02X})")
    return ok


def run_test(pipe_base: Path, verbose: bool = False) -> bool:
    """Run the strobe-mode handshake test.  Returns True on success."""
    data_fifo, status_fifo = require_fifos(pipe_base)
    print(f"Data FIFO:   {data_fifo}")
    print(f"Status FIFO: {status_fifo}")
    print()

    # Open the data FIFO for writing (blocks until a reader is attached)
    print("Opening data FIFO (blocks until pipe reader is ready)...")
    data_fd = os.open(str(data_fifo), os.O_WRONLY)
    print("Connected.")
    print()

    all_ok = True
    try:
        for i, byte in enumerate(TEST_PATTERN):
            label = f"byte {i:3d} (0x{byte:02X})"
            if not send_byte_and_check(data_fd, status_fifo, bytes([byte]), label):
                all_ok = False
            # Small delay between bytes to make output readable
            if verbose:
                time.sleep(0.01)

        print()
        if all_ok:
            print(f"All {len(TEST_PATTERN)} bytes acknowledged correctly.")
        else:
            print("Some bytes were not acknowledged correctly.")
    finally:
        os.close(data_fd)

    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test 86Box LPT 8-bit strobe mode handshake"
    )
    parser.add_argument(
        "pipe_base",
        type=Path,
        help="Named pipe base path (e.g. /tmp/my-printer)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Add delays between bytes for readable output",
    )
    args = parser.parse_args()

    try:
        ok = run_test(args.pipe_base, verbose=args.verbose)
    except (FileNotFoundError, TypeError, TimeoutError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
