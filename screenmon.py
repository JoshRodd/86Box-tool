#!/usr/bin/env python3
"""Live delta view of a guest text screen via the 86Box memdump server.

Polls the text-mode framebuffer through the memdump server (which reads
guest memory without suspending the CPU) and renders only what changed
between frames.

Rendering mode is chosen from the output stream:

  * interactive terminal (stdout.isatty()): full-screen ANSI view -- the
    first frame is painted once, then changed cells are overwritten in
    place, TTY-style.
  * piped or redirected stdout: simple delta text -- the first frame is
    printed in full, then only the changed rows (row-numbered), which is
    what agents and transcripts want.

Override the automatic choice with --full or --plain.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import TextIO

from guest_console import TextScreenBuffer
from rsp import (
    MemDump,
    TEXT_COLUMNS,
    TEXT_ROWS,
    TEXT_VIDEO_ADDRESS,
    VideoTextFrame,
    VideoTextMode,
)

DEFAULT_INTERVAL = 0.1


def _positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _non_negative_int(value: str) -> int:
    result = int(value, 0)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def choose_mode(stream: TextIO, override: str | None) -> str:
    """full (ANSI in-place) or simple (delta lines), honoring --full/--plain."""
    if override is not None:
        return override
    return "full" if stream.isatty() else "simple"


def render_full(update) -> str:
    """ANSI full-screen delta: paint on reset, overwrite changed cells."""
    if update.reset:
        out = ["\x1b[2J\x1b[H\x1b[?25l"]
        for line in update.lines:
            out.append(line)
            out.append("\r\n")
        return "".join(out)
    out = []
    for delta in update.deltas:
        out.append(f"\x1b[{delta.row + 1};{delta.column + 1}H{delta.text}")
    return "".join(out)


def render_simple(update, elapsed: float) -> str:
    """Plain delta text: full numbered frame on reset, changed rows after."""
    out = []
    if update.reset:
        out.append(f"=== {elapsed:6.1f}s — reset — full frame ===")
        for row, line in enumerate(update.lines):
            if line.strip():
                out.append(f"R{row:02d}: {line}")
        return "\n".join(out) + "\n"
    changed_rows = sorted({delta.row for delta in update.deltas})
    if not changed_rows:
        return ""
    out.append(f"--- {elapsed:6.1f}s — {len(changed_rows)} changed row(s) ---")
    for row in changed_rows:
        out.append(f"R{row:02d}: {update.lines[row]}")
    return "\n".join(out) + "\n"


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screenmon.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=os.environ.get("RSP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--memdump-port",
        type=int,
        default=int(os.environ.get("MEMDUMP_PORT", "12348")),
        help="memdump server port (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=DEFAULT_INTERVAL,
        help="poll interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--address",
        type=lambda s: int(s, 0),
        default=TEXT_VIDEO_ADDRESS,
        help=f"text framebuffer address (default: {TEXT_VIDEO_ADDRESS:#x})",
    )
    parser.add_argument(
        "--rows", type=int, default=TEXT_ROWS, help=f"rows (default: {TEXT_ROWS})"
    )
    parser.add_argument(
        "--cols", type=int, default=TEXT_COLUMNS, help=f"columns (default: {TEXT_COLUMNS})"
    )
    parser.add_argument(
        "--count",
        type=_non_negative_int,
        default=0,
        help="exit after this many polls (0 = run until interrupted)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full", action="store_const", const="full", dest="mode",
        help="force the ANSI full-screen delta view",
    )
    mode.add_argument(
        "--plain", action="store_const", const="simple", dest="mode",
        help="force the plain delta-line output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    options = _argument_parser().parse_args(argv)
    mode = choose_mode(sys.stdout, options.mode)
    if options.count == 0 and mode == "full" and not sys.stdout.isatty():
        # --full with a non-tty stream would spray ANSI escapes; the user
        # explicitly asked for it, so allow it, but say so.
        pass

    dump = MemDump(options.host, options.memdump_port)
    buffer = TextScreenBuffer()
    mode_info = VideoTextMode(
        number=3,
        columns=options.cols,
        rows=options.rows,
        base_address=options.address,
        page_offset=0,
        active_page=0,
        cursor_row=0,
        cursor_column=0,
        colour=True,
    )
    length = options.rows * options.cols * 2

    started = time.monotonic()
    polls = 0
    try:
        while options.count == 0 or polls < options.count:
            cells = dump.read(options.address, length)
            update = buffer.update(VideoTextFrame(mode_info, cells))
            elapsed = time.monotonic() - started
            if mode == "full":
                sys.stdout.write(render_full(update))
            else:
                sys.stdout.write(render_simple(update, elapsed))
            sys.stdout.flush()
            polls += 1
            if options.count == 0 or polls < options.count:
                time.sleep(options.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if mode == "full":
            # restore the terminal cursor and park it below the frame
            sys.stdout.write("\x1b[?25h")
            sys.stdout.write(f"\x1b[{options.rows + 1};1H")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
