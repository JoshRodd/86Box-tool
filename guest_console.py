#!/usr/bin/env python3
"""Mirror an 86Box BIOS text console and inject host keystrokes through RSP."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import select
import sys
import time
from typing import Generator, Sequence, TextIO

from rsp import (
    BIOSKey,
    RSP,
    VideoTextFrame,
    VideoTextMode,
    encode_bios_text,
    hardware_scan_bytes,
    inject_bios_keys,
    inject_keyboard_scan_byte,
    named_bios_key,
    read_video_text_frame,
)


DEFAULT_RATE = 70.0
EXIT_CHARACTER = 0x1D  # Ctrl-]
WAIT_SETTLE = 3.0       # debounce window for --wait-change
TEXT_COLUMNS_KEEP = 40  # ignore columns 41-80 (the POST clock graphic)


def _key_cells(cells: bytes, columns: int) -> bytes:
    """Return only the first TEXT_COLUMNS_KEEP columns of each row.

    The Model 25/30 POST clock graphic occupies columns 41-80 and changes
    once per second, so change detection must ignore it.
    """

    row_bytes = columns * 2
    keep = TEXT_COLUMNS_KEEP * 2
    return b"".join(cells[row : row + keep] for row in range(0, len(cells), row_bytes))
ESCAPE_SEQUENCE_TIMEOUT = 0.05
_CONTROL_GLYPHS = (
    " ",
    "☺",
    "☻",
    "♥",
    "♦",
    "♣",
    "♠",
    "•",
    "◘",
    "○",
    "◙",
    "♂",
    "♀",
    "♪",
    "♫",
    "☼",
    "►",
    "◄",
    "↕",
    "‼",
    "¶",
    "§",
    "▬",
    "↨",
    "↑",
    "↓",
    "→",
    "←",
    "∟",
    "↔",
    "▲",
    "▼",
)
_HOST_ESCAPE_KEYS = {
    b"\x1b[A": "UP",
    b"\x1b[B": "DOWN",
    b"\x1b[C": "RIGHT",
    b"\x1b[D": "LEFT",
    b"\x1b[H": "HOME",
    b"\x1b[F": "END",
    b"\x1b[2~": "INSERT",
    b"\x1b[3~": "DELETE",
    b"\x1b[5~": "PAGEUP",
    b"\x1b[6~": "PAGEDOWN",
    b"\x1bOP": "F1",
    b"\x1bOQ": "F2",
    b"\x1bOR": "F3",
    b"\x1bOS": "F4",
    b"\x1b[15~": "F5",
    b"\x1b[17~": "F6",
    b"\x1b[18~": "F7",
    b"\x1b[19~": "F8",
    b"\x1b[20~": "F9",
    b"\x1b[21~": "F10",
    b"\x1b[23~": "F11",
    b"\x1b[24~": "F12",
}


@dataclass(frozen=True)
class ScreenDelta:
    row: int
    column: int
    text: str


@dataclass(frozen=True)
class ScreenUpdate:
    reset: bool
    mode: VideoTextMode
    deltas: tuple[ScreenDelta, ...]
    lines: tuple[str, ...]
    cursor_changed: bool


class TextScreenBuffer:
    """Retain the last text-mode frame and produce changed character runs."""

    def __init__(self) -> None:
        self._mode_key: tuple[int, int, int, int] | None = None
        self._cells = b""
        self._cursor: tuple[int, int] | None = None

    def update(self, frame: VideoTextFrame) -> ScreenUpdate:
        expected = frame.mode.columns * frame.mode.rows * 2
        if len(frame.cells) != expected:
            raise ValueError(
                f"video frame must contain {expected} bytes, received {len(frame.cells)}"
            )
        mode_key = (
            frame.mode.number,
            frame.mode.columns,
            frame.mode.rows,
            frame.mode.memory_address,
        )
        reset = mode_key != self._mode_key or len(self._cells) != len(frame.cells)
        old = bytes(expected) if reset else self._cells
        deltas: list[ScreenDelta] = []
        for row in range(frame.mode.rows):
            column = 0
            while column < frame.mode.columns:
                cell = (row * frame.mode.columns + column) * 2
                if old[cell : cell + 2] == frame.cells[cell : cell + 2]:
                    column += 1
                    continue
                start = column
                column += 1
                while column < frame.mode.columns:
                    cell = (row * frame.mode.columns + column) * 2
                    if old[cell : cell + 2] == frame.cells[cell : cell + 2]:
                        break
                    column += 1
                first = (row * frame.mode.columns + start) * 2
                last = (row * frame.mode.columns + column) * 2
                deltas.append(
                    ScreenDelta(
                        row=row,
                        column=start,
                        text=decode_text_cells(frame.cells[first:last]),
                    )
                )
        cursor = (frame.mode.cursor_row, frame.mode.cursor_column)
        cursor_changed = reset or cursor != self._cursor
        self._mode_key = mode_key
        self._cells = frame.cells
        self._cursor = cursor
        return ScreenUpdate(
            reset=reset,
            mode=frame.mode,
            deltas=tuple(deltas),
            lines=tuple(
                decode_text_cells(
                    frame.cells[
                        row * frame.mode.columns * 2 : (row + 1)
                        * frame.mode.columns
                        * 2
                    ]
                )
                for row in range(frame.mode.rows)
            ),
            cursor_changed=cursor_changed,
        )


def decode_text_cells(cells: bytes) -> str:
    """Decode character/attribute pairs without emitting terminal controls."""

    characters: list[str] = []
    for value in cells[0::2]:
        if value < 0x20:
            characters.append(_CONTROL_GLYPHS[value])
        elif value == 0x7F:
            characters.append("⌂")
        else:
            characters.append(bytes((value,)).decode("cp437"))
    return "".join(characters)


class AnsiRenderer:
    def __init__(self, output: TextIO) -> None:
        self.output = output

    def render(self, update: ScreenUpdate, sample: int, elapsed: float) -> None:
        del sample, elapsed
        if not update.reset and not update.deltas and not update.cursor_changed:
            return
        chunks = ["\x1b[?25l"]
        if update.reset:
            chunks.append("\x1b[2J")
        for delta in update.deltas:
            chunks.append(f"\x1b[{delta.row + 1};{delta.column + 1}H{delta.text}")
        chunks.append(
            f"\x1b[{update.mode.cursor_row + 1};{update.mode.cursor_column + 1}H"
            "\x1b[?25h"
        )
        self.output.write("".join(chunks))
        self.output.flush()

    def close(self) -> None:
        self.output.write("\x1b[?25h\n")
        self.output.flush()


class JsonLinesRenderer:
    def __init__(self, output: TextIO) -> None:
        self.output = output

    def render(self, update: ScreenUpdate, sample: int, elapsed: float) -> None:
        if not update.reset and not update.deltas and not update.cursor_changed:
            return
        mode = {
            "number": update.mode.number,
            "columns": update.mode.columns,
            "rows": update.mode.rows,
            "colour": update.mode.colour,
            "base_address": update.mode.base_address,
            "page_offset": update.mode.page_offset,
            "memory_address": update.mode.memory_address,
            "active_page": update.mode.active_page,
        }
        if update.reset:
            event = {
                "type": "screen",
                "sample": sample,
                "elapsed": elapsed,
                "mode": mode,
                "lines": [line.rstrip() for line in update.lines],
                "cursor": [update.mode.cursor_row, update.mode.cursor_column],
            }
        else:
            event = {
                "type": "delta",
                "sample": sample,
                "elapsed": elapsed,
                "changes": [
                    {"row": delta.row, "column": delta.column, "text": delta.text}
                    for delta in update.deltas
                ],
                "cursor": [update.mode.cursor_row, update.mode.cursor_column],
            }
        self.output.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.output.flush()

    def close(self) -> None:
        pass


class HostKeyDecoder:
    """Translate a raw host terminal byte stream into BIOS key words.

    Supports ``<NAME>`` pseudo-keys (for example ``<F1>``, ``<ENTER>``),
    the terminal escape sequences in ``_HOST_ESCAPE_KEYS``, and plain
    printable characters.  ``Ctrl-]`` exits the console.
    """

    def __init__(self) -> None:
        self.pending = bytearray()
        self.escape_started: float | None = None

    def feed(self, data: bytes) -> tuple[list[BIOSKey], bool]:
        self.pending.extend(data)
        return self._consume(flush_escape=False)

    def flush_escape(self) -> tuple[list[BIOSKey], bool]:
        flush = (
            self.pending.startswith(b"\x1b")
            and self.escape_started is not None
            and time.monotonic() - self.escape_started >= ESCAPE_SEQUENCE_TIMEOUT
        )
        return self._consume(flush_escape=flush)

    def _consume(self, *, flush_escape: bool) -> tuple[list[BIOSKey], bool]:
        keys: list[BIOSKey] = []
        exit_requested = False
        while self.pending:
            first = self.pending[0]
            if first == EXIT_CHARACTER:
                del self.pending[0]
                exit_requested = True
                continue
            if first != 0x1B:
                # ``<NAME>`` pseudo-key, e.g. ``<F1>`` or ``<ENTER>``.
                if first == ord("<") and b">" in self.pending:
                    raw = bytes(self.pending)
                    name, _, rest = raw.partition(b">")
                    if len(name) > 1:
                        try:
                            keys.append(named_bios_key(name[1:].decode("ascii")))
                        except ValueError:
                            keys.extend(encode_bios_text(name.decode("ascii")))
                        del self.pending[: len(name) + 1]
                        continue
                del self.pending[0]
                if first >= 0x80:
                    continue
                keys.extend(encode_bios_text(bytes((first,)).decode("ascii")))
                continue

            exact = next(
                (
                    (sequence, name)
                    for sequence, name in _HOST_ESCAPE_KEYS.items()
                    if self.pending.startswith(sequence)
                ),
                None,
            )
            if exact is not None:
                sequence, name = exact
                del self.pending[: len(sequence)]
                keys.append(named_bios_key(name))
                self.escape_started = None
                continue
            if any(sequence.startswith(self.pending) for sequence in _HOST_ESCAPE_KEYS):
                if self.escape_started is None:
                    self.escape_started = time.monotonic()
                if not flush_escape:
                    break
            del self.pending[0]
            keys.append(named_bios_key("ESC"))
            self.escape_started = None
        return keys, exit_requested


class GuestInputQueue:
    """Queue decoded keys for either BIOS-ring or hardware delivery.

    Hardware delivery paces each make/break like a physical key tap:
    the make is injected, the guest CPU is left running for ``key_delay``
    seconds, and only then is the break injected.  This matches how the
    Model 25/30 BIOS consumes the PS/2 interface (make via INT 16H, then
    the port-60 break check) and avoids the spurious beeps caused by
    back-to-back make/break injection.
    """

    def __init__(self, mode: str, key_delay: float = 0.3) -> None:
        if mode not in ("bios", "hardware"):
            raise ValueError(f"unsupported input mode: {mode}")
        self.mode = mode
        self.key_delay = key_delay
        self.bios_keys: deque[BIOSKey] = deque()
        self.scan_bytes: deque[int] = deque()
        self.pending_breaks: list[int] = []
        self.break_due: float | None = None

    @property
    def pending_break(self) -> tuple[int, float] | None:
        """The pending break byte and its due time, if the break phase is armed."""
        if self.pending_breaks and self.break_due is not None:
            return (self.pending_breaks[0] & 0x7F, self.break_due)
        return None

    def extend(self, keys: Sequence[BIOSKey]) -> None:
        if self.mode == "bios":
            self.bios_keys.extend(keys)
        else:
            self.scan_bytes.extend(hardware_scan_bytes(keys))

    def waiting_for_break(self, now: float) -> bool:
        return (
            self.mode == "hardware"
            and bool(self.pending_breaks)
            and self.break_due is not None
            and now < self.break_due
        )

    def inject(self, client: RSP, now: float | None = None) -> int:
        if self.mode == "bios":
            written = inject_bios_keys(client, self.bios_keys)
            for _ in range(written):
                self.bios_keys.popleft()
            return written
        if now is None:
            now = time.monotonic()

        # Break phase: once the make/break gap has elapsed, deliver every
        # break byte of the current key (letter break, then shift break).
        if self.pending_breaks and self.break_due is not None:
            if now < self.break_due:
                return 0
            while self.pending_breaks:
                byte = self.pending_breaks.pop(0)
                inject_keyboard_scan_byte(client, byte & 0x7F, make=False)
            self.break_due = None
            return 1

        if not self.scan_bytes:
            return 0

        # Start a new key: deliver all leading make bytes together so a
        # shift (or control) modifier is still held when the letter key
        # lands - pacing each byte as a separate tap would release the
        # shift before the letter and turn 'Y' into 'y' (or a shifted
        # symbol into its unshifted twin).  Then arm the break phase.
        while self.scan_bytes and not (self.scan_bytes[0] & 0x80):
            byte = self.scan_bytes.popleft()
            inject_keyboard_scan_byte(client, byte & 0x7F, make=True)
        while self.scan_bytes and (self.scan_bytes[0] & 0x80):
            self.pending_breaks.append(self.scan_bytes.popleft())
        self.break_due = now + self.key_delay
        return 1


@contextmanager
def raw_terminal_input(file_descriptor: int | None) -> Generator[None, None, None]:
    if file_descriptor is None or not os.isatty(file_descriptor):
        yield
        return
    try:
        import termios
        import tty
    except ImportError:
        yield
        return

    attributes = termios.tcgetattr(file_descriptor)
    blocking = os.get_blocking(file_descriptor)
    try:
        tty.setraw(file_descriptor)
        os.set_blocking(file_descriptor, False)
        yield
    finally:
        os.set_blocking(file_descriptor, blocking)
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, attributes)


def _positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _non_negative_float(value: str) -> float:
    result = float(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream an 86Box BIOS text screen at 70 Hz and inject host "
            "keystrokes through the BIOS keyboard buffer."
        )
    )
    parser.add_argument("--host", default=os.environ.get("RSP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RSP_PORT", "12345")),
    )
    parser.add_argument("--timeout", type=_positive_float, default=15.0)
    parser.add_argument("--rate", type=_positive_float, default=DEFAULT_RATE)
    parser.add_argument(
        "--duration",
        type=_non_negative_float,
        default=0.0,
        help="stop after this many seconds; zero streams until Ctrl-]",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "ansi", "jsonl"),
        default="auto",
        help="ANSI terminal mirror or structured delta events (default: auto)",
    )
    parser.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="TEXT",
        help="queue initial text; repeat as needed",
    )
    parser.add_argument("--key", action="append", default=[], metavar="NAME")
    parser.add_argument("--enter", action="store_true")
    parser.add_argument(
        "--input-mode",
        choices=("bios", "hardware"),
        default="hardware",
        help=(
            "hardware = make/break scan codes through the keyboard device "
            "(works during POST, Model 25/30); bios = write the INT 16H ring "
            "after boot (default: hardware)"
        ),
    )
    parser.add_argument(
        "--key-delay",
        type=_positive_float,
        default=0.3,
        help="seconds between a key make and its break (default: 0.3)",
    )
    parser.add_argument(
        "--wait-change",
        action="store_true",
        help=(
            "exit once the screen changes and then settles for "
            f"{WAIT_SETTLE:.0f}s; combine with --key/--send to act and wait "
            "for the result"
        ),
    )
    parser.add_argument(
        "--retry",
        type=_positive_float,
        default=3.0,
        help=(
            "with --wait-change: if the screen has not changed, re-inject "
            "the --key/--send input every this many seconds (default: 3.0)"
        ),
    )
    parser.add_argument("--no-input", action="store_true")
    return parser


def _read_available_input(file_descriptor: int) -> bytes:
    try:
        return os.read(file_descriptor, 4096)
    except BlockingIOError:
        return b""


def _wait_until_sample(
    deadline: float,
    file_descriptor: int | None,
    decoder: HostKeyDecoder,
    input_queue: GuestInputQueue,
) -> tuple[int | None, bool]:
    exit_requested = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or exit_requested:
            return file_descriptor, exit_requested
        if file_descriptor is None:
            time.sleep(remaining)
            return None, False
        readable, _, _ = select.select((file_descriptor,), (), (), remaining)
        if not readable:
            return file_descriptor, False
        data = _read_available_input(file_descriptor)
        if not data:
            file_descriptor = None
            continue
        keys, should_exit = decoder.feed(data)
        input_queue.extend(keys)
        exit_requested = exit_requested or should_exit


def stream_console(options: argparse.Namespace) -> int:
    output_format = options.format
    if output_format == "auto":
        output_format = "ansi" if sys.stdout.isatty() else "jsonl"
    renderer = (
        AnsiRenderer(sys.stdout)
        if output_format == "ansi"
        else JsonLinesRenderer(sys.stdout)
    )
    initial_keys = [key for text in options.send for key in encode_bios_text(text)]
    initial_keys.extend(named_bios_key(name) for name in options.key)
    if options.enter:
        initial_keys.append(named_bios_key("ENTER"))
    input_queue = GuestInputQueue(options.input_mode, key_delay=options.key_delay)
    input_queue.extend(initial_keys)
    retry_keys = initial_keys if options.wait_change else []

    input_fd = None if options.no_input else sys.stdin.fileno()
    decoder = HostKeyDecoder()
    buffer = TextScreenBuffer()
    client = RSP(host=options.host, port=options.port, timeout=options.timeout)
    period = 1.0 / options.rate
    started = time.monotonic()
    end_time = started + options.duration if options.duration else None
    next_sample = started
    samples = 0
    exit_requested = False
    if input_fd is not None and os.isatty(input_fd):
        print("guest console attached; press Ctrl-] to exit", file=sys.stderr)
    try:
        # The GDB stub processes packets at CPU instruction boundaries, so
        # the guest can run continuously: frame reads and key injections
        # happen while it executes.  Stopping the CPU to read the frame
        # buffer would stall the emulated keyboard and is unnecessary (we
        # accept tearing).  The make/break gap is paced in real time below.
        stable_screen: bytes | None = None
        changed_at: float | None = None
        last_retry: float = 0.0
        client.resume()
        with raw_terminal_input(input_fd):
            while not exit_requested:
                now = time.monotonic()
                if input_queue.waiting_for_break(now):
                    # A make was injected; wait out the rest of the
                    # make/break gap so the BIOS processes the make like a
                    # physical key tap before the break arrives.
                    while not exit_requested:
                        now = time.monotonic()
                        if now >= input_queue.pending_break[1]:
                            break
                        remaining = input_queue.pending_break[1] - now
                        if input_fd is None:
                            time.sleep(remaining)
                            break
                        readable, _, _ = select.select(
                            (input_fd,), (), (), remaining
                        )
                        if not readable:
                            break
                        data = _read_available_input(input_fd)
                        if not data:
                            break
                        decoded, should_exit = decoder.feed(data)
                        input_queue.extend(decoded)
                        exit_requested = exit_requested or should_exit
                    input_queue.inject(client)
                    now = time.monotonic()
                    if exit_requested or (
                        end_time is not None and now >= end_time
                    ):
                        break
                    continue

                frame = read_video_text_frame(client)
                if options.wait_change:
                    # Debounced change detection: the first frame is only
                    # the baseline; every change from it restarts the
                    # settle timer, and we exit once the screen has been
                    # stable for WAIT_SETTLE seconds after a change.  The
                    # clock graphic on columns 41-80 is ignored.
                    key = _key_cells(frame.cells, frame.mode.columns)
                    if key != stable_screen:
                        if stable_screen is not None:
                            changed_at = time.monotonic()
                        stable_screen = key
                        update = buffer.update(frame)
                        renderer.render(
                            update, samples, time.monotonic() - started
                        )
                        samples += 1
                    elif changed_at is not None and now - changed_at >= WAIT_SETTLE:
                        break
                else:
                    update = buffer.update(frame)
                    renderer.render(update, samples, time.monotonic() - started)
                    samples += 1

                decoded, should_exit = decoder.flush_escape()
                input_queue.extend(decoded)
                exit_requested = exit_requested or should_exit
                input_queue.inject(client)
                now = time.monotonic()
                if exit_requested or (end_time is not None and now >= end_time):
                    break

                if options.wait_change and retry_keys and changed_at is None:
                    # The screen has not changed yet; re-inject the initial
                    # keys on the retry interval.  The Model 25/30 POST
                    # disables the keyboard (F5) in a loop while waiting for
                    # F1, so a single tap can land in a disabled window.
                    if now - last_retry >= options.retry:
                        last_retry = now
                        input_queue.extend(retry_keys)

                next_sample = max(next_sample + period, now)
                input_fd, should_exit = _wait_until_sample(
                    next_sample,
                    input_fd,
                    decoder,
                    input_queue,
                )
                exit_requested = exit_requested or should_exit
    finally:
        # Stop the guest so the RSP connection can detach cleanly.
        try:
            client.interrupt()
        except (ConnectionError, OSError):
            pass
        client.detach()
        renderer.close()
    elapsed = time.monotonic() - started
    print(
        f"guest console detached after {samples} samples in {elapsed:.3f}s "
        f"({samples / elapsed:.1f} checks/s)",
        file=sys.stderr,
    )
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    options = _argument_parser().parse_args(arguments)
    try:
        return stream_console(options)
    except KeyboardInterrupt:
        return 130
    except (ConnectionError, OSError, ValueError, TimeoutError) as error:
        print(f"guest_console.py: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
