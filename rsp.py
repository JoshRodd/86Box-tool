#!/usr/bin/env python3
"""Dependency-free RSP client for observing an 86Box emulated machine."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import socket
import sys
import time
from typing import Iterable, Sequence


REG_EAX, REG_ESP, REG_EIP, REG_CS, REG_SS = 0, 4, 8, 10, 11
TEXT_VIDEO_ADDRESS = 0xB8000
TEXT_COLUMNS = 80
TEXT_ROWS = 25
BDA_VIDEO_STATE_ADDRESS = 0x449
BDA_VIDEO_STATE_SIZE = 0x1A
BDA_KEYBOARD_HEAD = 0x41A
BDA_KEYBOARD_TAIL = 0x41C
BDA_KEYBOARD_BOUNDS = 0x480
BIOS_DATA_BASE = 0x400
CLASSIC_KEYBOARD_START = 0x1E
CLASSIC_KEYBOARD_END = 0x3E
VIDEO_MODE_LAYOUTS = {
    0: (40, 25, 0xB8000, True),
    1: (40, 25, 0xB8000, True),
    2: (80, 25, 0xB8000, True),
    3: (80, 25, 0xB8000, True),
    7: (80, 25, 0xB0000, False),
}


class RSP:
    """Minimal client for 86Box's guest GDB remote-serial-protocol stub."""

    def __init__(self, host: str = "127.0.0.1", port: int = 12345, timeout: float = 15.0):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(timeout)
        # Prime response_event. The 86Box stub otherwise blocks the first
        # non-continue packet until it sees an ACK.
        self.s.sendall(b"+")

    def _send_raw(self, data: bytes) -> None:
        self.s.sendall(data)

    def _send_packet(self, payload: bytes) -> None:
        checksum = sum(payload) & 0xFF
        self.s.sendall(b"$" + payload + b"#" + f"{checksum:02x}".encode())

    def _recv_packet(self) -> bytes:
        packet = bytearray()
        while True:
            byte = self.s.recv(1)
            if not byte:
                raise ConnectionError("RSP connection closed before a packet")
            if byte == b"+":
                continue
            if byte == b"-":
                raise OSError("RSP peer rejected the packet")
            if byte == b"$":
                break
        while True:
            byte = self.s.recv(1)
            if not byte:
                raise ConnectionError("RSP connection closed inside a packet")
            if byte == b"#":
                break
            packet.extend(byte)
        checksum = self.s.recv(2)
        if len(checksum) != 2:
            raise ConnectionError("RSP connection closed before the checksum")
        expected = sum(packet) & 0xFF
        try:
            actual = int(checksum, 16)
        except ValueError as error:
            raise OSError(f"invalid RSP checksum {checksum!r}") from error
        if actual != expected:
            self.s.sendall(b"-")
            raise OSError(
                f"RSP checksum mismatch: received {actual:02x}, expected {expected:02x}"
            )
        self.s.sendall(b"+")
        return bytes(packet)

    def break_cpu(self) -> bytes:
        """Interrupt guest execution and retrieve its stop packet."""
        self._send_raw(b"\x03")
        return self.cmd(b"?")

    def resume(self) -> None:
        """Resume the guest without waiting for its next stop packet."""

        self._send_packet(b"c")

    def interrupt(self) -> bytes:
        """Interrupt a guest resumed with resume() and receive its stop packet."""

        self._send_raw(b"\x03")
        return self._recv_packet()

    def cmd(self, payload: bytes) -> bytes:
        self._send_packet(payload)
        return self._recv_packet()

    def read_mem(self, address: int, length: int) -> bytes:
        if address < 0 or length < 0:
            raise ValueError("address and length must be non-negative")
        response = self.cmd(f"m{address:x},{length:x}".encode())
        if response.startswith(b"E"):
            raise OSError(response.decode(errors="replace"))
        try:
            data = bytes.fromhex(response.decode())
        except (UnicodeError, ValueError) as error:
            raise OSError(f"malformed memory response: {response!r}") from error
        if len(data) != length:
            raise OSError(f"short memory response: expected {length}, received {len(data)}")
        return data

    def write_mem(self, address: int, data: bytes) -> None:
        response = self.cmd(f"M{address:x},{len(data):x}:{data.hex()}".encode())
        if response != b"OK":
            raise OSError(f"memory write failed: {response!r}")

    def set_hw_bp(self, address: int) -> None:
        response = self.cmd(f"Z1,{address:x},1".encode())
        if response != b"OK":
            # Hardware breakpoints persist across client connections.
            self.cmd(f"z1,{address:x},1".encode())
            response = self.cmd(f"Z1,{address:x},1".encode())
            if response != b"OK":
                raise OSError(f"hardware breakpoint failed: {response!r}")

    def clear_hw_bp(self, address: int) -> None:
        response = self.cmd(f"z1,{address:x},1".encode())
        if response not in (b"OK", b""):
            raise OSError(f"clear hardware breakpoint failed: {response!r}")

    def monitor(self, command: str) -> None:
        """Run a side-effect-only 86Box GDB monitor command."""

        response = self.cmd(b"qRcmd," + command.encode().hex().encode())
        if response != b"OK":
            raise OSError(f"monitor command failed: {command!r}: {response!r}")

    def detach(self) -> None:
        try:
            self.cmd(b"D")
        except (OSError, socket.timeout):
            pass
        finally:
            self.s.close()

    @staticmethod
    def parse_regs(packet: bytes) -> dict[int, bytes]:
        """Parse indexed register fields from a T stop packet leniently."""
        registers: dict[int, bytes] = {}
        index = 0
        while index < len(packet):
            if packet[index : index + 1] == b":":
                start = index
                while start > 0 and packet[start - 1 : start] in b"0123456789abcdef":
                    start -= 1
                number_text = packet[start:index]
                if number_text and len(number_text) <= 2:
                    separator = packet.find(b";", index)
                    if separator < 0:
                        break
                    registers[int(number_text, 16)] = packet[index + 1 : separator]
                    index = separator + 1
                    continue
            index += 1
        return registers


class MemDump:
    """CPU-suspension-free guest memory reads via the 86Box memdump server.

    The GDB stub suspends the guest CPU to service memory reads; the
    memdump server (src/memdump.c) reads the emulated memory straight
    from a dedicated thread instead, so polling the guest screen here
    never pauses the emulation.  Tearing is accepted.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 12348, timeout: float = 5.0):
        self.addr = (host, port)
        self.timeout = timeout

    def read(self, address: int, length: int) -> bytes:
        if address < 0 or length < 0:
            raise ValueError("address and length must be non-negative")
        with socket.create_connection(self.addr, timeout=self.timeout) as conn:
            conn.sendall(f"{address:x}:{length:x}\n".encode())
            response = bytearray()
            while len(response) < length * 2 + 1:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        text = response.decode(errors="replace").strip()
        data = bytes.fromhex(text)
        if len(data) != length:
            raise OSError(f"short memdump response: expected {length}, received {len(data)}")
        return data


def stop_regs(packet: bytes) -> dict[int, bytes]:
    return RSP.parse_regs(packet)


def reg_val(hex_string: bytes) -> int:
    """Decode an x86 RSP register value (hex-encoded little-endian bytes)."""
    return int.from_bytes(bytes.fromhex(hex_string.decode()), "little")


def reg16(hex_string: bytes) -> int:
    return reg_val(hex_string) & 0xFFFF



@dataclass(frozen=True)
class VideoTextMode:
    number: int
    columns: int
    rows: int
    base_address: int
    page_offset: int
    active_page: int
    cursor_row: int
    cursor_column: int
    colour: bool

    @property
    def memory_address(self) -> int:
        return self.base_address + self.page_offset


@dataclass(frozen=True)
class VideoTextFrame:
    mode: VideoTextMode
    cells: bytes


@dataclass(frozen=True)
class BIOSKey:
    ascii: int
    scan: int


def read_video_text_mode(client: RSP) -> VideoTextMode:
    """Read and validate the active BIOS text mode from the BIOS data area."""

    state = client.read_mem(BDA_VIDEO_STATE_ADDRESS, BDA_VIDEO_STATE_SIZE)
    number = state[0]
    try:
        columns, rows, base_address, colour = VIDEO_MODE_LAYOUTS[number]
    except KeyError as error:
        raise ValueError(
            f"unsupported BIOS video mode {number:#04x}; expected 0, 1, 2, 3, or 7"
        ) from error
    reported_columns = int.from_bytes(state[1:3], "little")
    if reported_columns not in (0, columns):
        raise ValueError(
            f"BIOS mode {number} reports {reported_columns} columns, expected {columns}"
        )
    page_offset = int.from_bytes(state[5:7], "little")
    screen_size = columns * rows * 2
    if page_offset + screen_size > 0x8000:
        raise ValueError(
            f"BIOS video page offset {page_offset:#06x} exceeds text memory"
        )
    active_page = state[25] & 0x07
    cursor_offset = 7 + active_page * 2
    cursor = int.from_bytes(state[cursor_offset : cursor_offset + 2], "little")
    cursor_column = cursor & 0xFF
    cursor_row = (cursor >> 8) & 0xFF
    if cursor_column >= columns:
        cursor_column = columns - 1
    if cursor_row >= rows:
        cursor_row = rows - 1
    return VideoTextMode(
        number=number,
        columns=columns,
        rows=rows,
        base_address=base_address,
        page_offset=page_offset,
        active_page=active_page,
        cursor_row=cursor_row,
        cursor_column=cursor_column,
        colour=colour,
    )


def read_video_text_frame(client: RSP) -> VideoTextFrame:
    mode = read_video_text_mode(client)
    cells = client.read_mem(mode.memory_address, mode.columns * mode.rows * 2)
    return VideoTextFrame(mode=mode, cells=cells)


_ASCII_SCANS = {
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "-": 0x0C,
    "=": 0x0D,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "[": 0x1A,
    "]": 0x1B,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    ";": 0x27,
    "'": 0x28,
    "`": 0x29,
    "\\": 0x2B,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
    ",": 0x33,
    ".": 0x34,
    "/": 0x35,
    " ": 0x39,
}
for shifted, base in zip("!@#$%^&*()_+{}:\"~|<>?", "1234567890-=[];'`\\,./"):
    _ASCII_SCANS[shifted] = _ASCII_SCANS[base]
for letter in "abcdefghijklmnopqrstuvwxyz":
    _ASCII_SCANS[letter.upper()] = _ASCII_SCANS[letter]

_NAMED_KEYS = {
    "ESC": BIOSKey(0x1B, 0x01),
    "BACKSPACE": BIOSKey(0x08, 0x0E),
    "TAB": BIOSKey(0x09, 0x0F),
    "ENTER": BIOSKey(0x0D, 0x1C),
    "UP": BIOSKey(0x00, 0x48),
    "DOWN": BIOSKey(0x00, 0x50),
    "LEFT": BIOSKey(0x00, 0x4B),
    "RIGHT": BIOSKey(0x00, 0x4D),
    "HOME": BIOSKey(0x00, 0x47),
    "END": BIOSKey(0x00, 0x4F),
    "PAGEUP": BIOSKey(0x00, 0x49),
    "PAGEDOWN": BIOSKey(0x00, 0x51),
    "INSERT": BIOSKey(0x00, 0x52),
    "DELETE": BIOSKey(0x00, 0x53),
    **{f"F{number}": BIOSKey(0x00, 0x3A + number) for number in range(1, 11)},
    "F11": BIOSKey(0x00, 0x85),
    "F12": BIOSKey(0x00, 0x86),
}


def named_bios_key(name: str) -> BIOSKey:
    try:
        return _NAMED_KEYS[name.upper()]
    except KeyError:
        pass

    # Accept a single printable ASCII character (letters, digits, punctuation).
    if len(name) == 1:
        character = name[0]
        if character in "\r\n":
            return _NAMED_KEYS["ENTER"]
        if character == "\b" or character == "\x7f":
            return _NAMED_KEYS["BACKSPACE"]
        if character == "\t":
            return _NAMED_KEYS["TAB"]
        if character == "\x1b":
            return _NAMED_KEYS["ESC"]
        if "\x01" <= character <= "\x1a":
            letter = chr(ord("a") + ord(character) - 1)
            return BIOSKey(ord(character), _ASCII_SCANS[letter])
        try:
            return BIOSKey(ord(character), _ASCII_SCANS[character])
        except KeyError:
            pass

    expected = ", ".join(sorted(_NAMED_KEYS))
    raise ValueError(f"unknown key {name!r}; expected one of: {expected}") from None


def encode_bios_text(text: str) -> list[BIOSKey]:
    keys: list[BIOSKey] = []
    for character in text:
        if character in "\r\n":
            keys.append(_NAMED_KEYS["ENTER"])
        elif character == "\b" or character == "\x7f":
            keys.append(_NAMED_KEYS["BACKSPACE"])
        elif character == "\t":
            keys.append(_NAMED_KEYS["TAB"])
        elif character == "\x1b":
            keys.append(_NAMED_KEYS["ESC"])
        elif "\x01" <= character <= "\x1a":
            letter = chr(ord("a") + ord(character) - 1)
            keys.append(BIOSKey(ord(character), _ASCII_SCANS[letter]))
        else:
            try:
                scan = _ASCII_SCANS[character]
            except KeyError as error:
                raise ValueError(
                    f"cannot inject non-US-ASCII keyboard character {character!r}"
                ) from error
            keys.append(BIOSKey(ord(character), scan))
    return keys


def _keyboard_bounds(client: RSP) -> tuple[int, int]:
    bounds = client.read_mem(BDA_KEYBOARD_BOUNDS, 4)
    start = int.from_bytes(bounds[:2], "little")
    end = int.from_bytes(bounds[2:], "little")
    valid = (
        start >= CLASSIC_KEYBOARD_START
        and end > start
        and end <= 0x100
        and start % 2 == 0
        and end % 2 == 0
    )
    return (start, end) if valid else (CLASSIC_KEYBOARD_START, CLASSIC_KEYBOARD_END)


def inject_bios_keys(client: RSP, keys: Iterable[BIOSKey]) -> int:
    """Queue as many keys as fit in the active BIOS keyboard ring."""

    pending = list(keys)
    if not pending:
        return 0
    pointers = client.read_mem(BDA_KEYBOARD_HEAD, 4)
    head = int.from_bytes(pointers[:2], "little")
    tail = int.from_bytes(pointers[2:], "little")
    start, end = _keyboard_bounds(client)
    if (
        head < start
        or head >= end
        or tail < start
        or tail >= end
        or head % 2
        or tail % 2
    ):
        raise ValueError(
            f"invalid BIOS keyboard ring: head={head:#06x}, tail={tail:#06x}, "
            f"bounds={start:#06x}-{end:#06x}"
        )

    written = 0
    for key in pending:
        next_tail = tail + 2
        if next_tail >= end:
            next_tail = start
        if next_tail == head:
            break
        client.write_mem(BIOS_DATA_BASE + tail, bytes((key.ascii, key.scan)))
        tail = next_tail
        written += 1
    if written:
        client.write_mem(BDA_KEYBOARD_TAIL, tail.to_bytes(2, "little"))
    return written


_SHIFTED_ASCII = frozenset('~!@#$%^&*()_+{}|:"<>?')
_LETTER_SCANS = frozenset(_ASCII_SCANS[letter] for letter in "abcdefghijklmnopqrstuvwxyz")
_EXTENDED_BIOS_SCANS = frozenset(
    (0x47, 0x48, 0x49, 0x4B, 0x4D, 0x4F, 0x50, 0x51, 0x52, 0x53)
)


def hardware_scan_bytes(keys: Iterable[BIOSKey]) -> list[int]:
    """Encode BIOS key words as XT set-1 make/break bytes for the 8042."""

    result: list[int] = []
    for key in keys:
        scan = key.scan
        extended = key.ascii == 0 and scan in _EXTENDED_BIOS_SCANS
        if scan == 0x85:
            scan = 0x57
        elif scan == 0x86:
            scan = 0x58
        if scan <= 0 or scan >= 0x80:
            raise ValueError(f"cannot encode BIOS scan code {key.scan:#04x}")

        shift = (
            0x41 <= key.ascii <= 0x5A
            or chr(key.ascii) in _SHIFTED_ASCII
            if key.ascii
            else False
        )
        control = 0x01 <= key.ascii <= 0x1A and scan in _LETTER_SCANS
        if shift:
            result.append(0x2A)
        if control:
            result.append(0x1D)
        if extended:
            result.append(0xE0)
        result.append(scan)
        if extended:
            result.append(0xE0)
        result.append(scan | 0x80)
        if control:
            result.append(0x9D)
        if shift:
            result.append(0xAA)
    return result


def inject_keyboard_scan_byte(client: RSP, scan_byte: int, make: bool = True) -> None:
    """Feed one scan byte through the emulated keyboard device.

    Uses the GDB monitor `kb` command, which calls 86Box's own
    keyboard_input() path - the same route a physical key takes.
    This works on every machine, including the PS/2 Model 25/30
    gate-array keyboard controller, which has no 8042 port 64 for
    the ob 64 d2 / ob 60 trick.
    """

    if not 0 <= scan_byte <= 0xFF:
        raise ValueError(f"keyboard scan byte is out of range: {scan_byte}")
    # Explicit 0x prefix: the GDB monitor parser treats a bare "0b" as a
    # binary literal, so scan codes like 0x0B would parse as 0.
    client.monitor(f"kb 0x{scan_byte:02x} {'1' if make else '0'}")

def render_text_screen(
    data: bytes,
    *,
    columns: int = TEXT_COLUMNS,
    rows: int = TEXT_ROWS,
) -> str:
    """Render color-text VRAM character/attribute pairs as CP437 text."""
    expected = columns * rows * 2
    if len(data) != expected:
        raise ValueError(f"screen data must be {expected} bytes, received {len(data)}")
    lines: list[str] = []
    for row in range(rows):
        offset = row * columns * 2
        characters = bytes(
            byte if byte != 0 else 0x20
            for byte in data[offset : offset + columns * 2 : 2]
        )
        lines.append(characters.decode("cp437").rstrip())
    return "\n".join(lines)


def read_text_screen(
    client: RSP,
    *,
    address: int = TEXT_VIDEO_ADDRESS,
    columns: int = TEXT_COLUMNS,
    rows: int = TEXT_ROWS,
) -> str:
    return render_text_screen(
        client.read_mem(address, columns * rows * 2),
        columns=columns,
        rows=rows,
    )


def render_video_text_frame(frame: VideoTextFrame) -> str:
    return render_text_screen(
        frame.cells,
        columns=frame.mode.columns,
        rows=frame.mode.rows,
    )


def _hex_integer(value: str) -> int:
    try:
        return int(value, 16)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a hexadecimal integer: {value}") from error


def _non_negative_float(value: str) -> float:
    result = float(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsp.py",
        description="Observe and debug an emulated 86Box guest through its RSP stub.",
    )
    parser.add_argument("--host", default=os.environ.get("RSP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RSP_PORT", "12345")),
    )
    parser.add_argument("--timeout", type=_non_negative_float, default=15.0)
    commands = parser.add_subparsers(dest="action", required=True)

    commands.add_parser("probe", help="pause and report CS:EIP")

    dump = commands.add_parser("dump", help="pause and dump guest memory")
    dump.add_argument("address", type=_hex_integer)
    dump.add_argument("length", type=_hex_integer)

    commands.add_parser("video-mode", help="inspect the active BIOS text mode")

    screen = commands.add_parser("screen", help="render the active BIOS text screen")
    screen.add_argument("--address", type=_hex_integer)
    screen.add_argument("--columns", type=int)
    screen.add_argument("--rows", type=int)

    wait_screen = commands.add_parser(
        "wait-screen",
        help="poll active text VRAM until it contains a string",
    )
    wait_screen.add_argument("text")
    wait_screen.add_argument("--address", type=_hex_integer)
    wait_screen.add_argument("--columns", type=int)
    wait_screen.add_argument("--rows", type=int)
    wait_screen.add_argument("--interval", type=_non_negative_float, default=1.0)
    wait_screen.add_argument("--wait-timeout", type=_non_negative_float, default=300.0)

    type_command = commands.add_parser(
        "type",
        help="inject text and named keys through the emulated keyboard",
    )
    type_command.add_argument("text", nargs="?", default="")
    type_command.add_argument("--enter", action="store_true")
    type_command.add_argument("--key", action="append", default=[])
    type_command.add_argument(
        "--bios-buffer",
        action="store_true",
        help="queue BIOS key words instead of injecting hardware scan codes",
    )
    type_command.add_argument("--wait-timeout", type=_non_negative_float, default=5.0)

    trace = commands.add_parser("trace", help="trace one or more hardware breakpoints")
    trace.add_argument("addresses", nargs="*", type=_hex_integer, default=[0xF24A0])
    trace.add_argument("--count", type=int, default=0, help="stop after this many hits")
    return parser


def _connect(options: argparse.Namespace) -> RSP:
    return RSP(host=options.host, port=options.port, timeout=options.timeout)


def _probe(options: argparse.Namespace) -> int:
    client = _connect(options)
    try:
        registers = stop_regs(client.break_cpu())
        eip = reg_val(registers.get(REG_EIP, b"0"))
        cs = reg16(registers.get(REG_CS, b"0"))
        print(f"halted at {cs:04X}:{eip & 0xFFFF:04X} (linear EIP {eip:08X})")
    finally:
        client.detach()
    return 0


def _dump(options: argparse.Namespace) -> int:
    client = _connect(options)
    try:
        client.break_cpu()
        data = client.read_mem(options.address, options.length)
    finally:
        client.detach()
    for offset in range(0, len(data), 16):
        print(f"{options.address + offset:06x}  {data[offset : offset + 16].hex(' ')}")
    return 0


def _video_mode(options: argparse.Namespace) -> int:
    client = _connect(options)
    try:
        client.break_cpu()
        mode = read_video_text_mode(client)
    finally:
        client.detach()
    kind = "colour" if mode.colour else "mono"
    print(
        f"mode {mode.number}: {mode.columns}x{mode.rows} {kind}, "
        f"base {mode.base_address:05X}h, page {mode.active_page}, "
        f"offset {mode.page_offset:04X}h, address {mode.memory_address:05X}h, "
        f"cursor {mode.cursor_row},{mode.cursor_column}"
    )
    return 0


def _selected_text_screen(
    client: RSP,
    options: argparse.Namespace,
) -> tuple[VideoTextMode, str]:
    mode = read_video_text_mode(client)
    address = mode.memory_address if options.address is None else options.address
    columns = mode.columns if options.columns is None else options.columns
    rows = mode.rows if options.rows is None else options.rows
    return mode, render_text_screen(
        client.read_mem(address, columns * rows * 2),
        columns=columns,
        rows=rows,
    )


def _type_keys(options: argparse.Namespace) -> int:
    keys = encode_bios_text(options.text)
    keys.extend(named_bios_key(name) for name in options.key)
    if options.enter:
        keys.append(named_bios_key("ENTER"))
    if not keys:
        raise ValueError("type requires text, --key, or --enter")

    client = _connect(options)
    deadline = time.monotonic() + options.wait_timeout
    try:
        client.break_cpu()
        if options.bios_buffer:
            pending = list(keys)
            sent = 0
            while pending:
                written = inject_bios_keys(client, pending)
                del pending[:written]
                sent += written
                if pending:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"BIOS keyboard buffer accepted only {sent} keys"
                        )
                    client.resume()
                    time.sleep(1 / 70)
                    client.interrupt()
            detail = f"{sent} BIOS key words"
        else:
            scan_bytes = hardware_scan_bytes(keys)
            total_scans = len(scan_bytes)
            for index, scan_byte in enumerate(scan_bytes):
                inject_keyboard_scan_byte(client, scan_byte)
                if index + 1 < total_scans:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"hardware keyboard accepted only "
                            f"{index + 1} of {total_scans} scan bytes"
                        )
                    client.resume()
                    time.sleep(1 / 70)
                    client.interrupt()
            detail = f"{len(keys)} keys ({total_scans} hardware scan bytes)"
    finally:
        client.detach()
    print(f"injected {detail}")
    return 0


def _screen(options: argparse.Namespace) -> int:
    client = _connect(options)
    try:
        client.break_cpu()
        _, text = _selected_text_screen(client, options)
    finally:
        client.detach()
    print(text)
    return 0


def _wait_screen(options: argparse.Namespace) -> int:
    deadline = time.monotonic() + options.wait_timeout
    latest = ""
    while True:
        client = _connect(options)
        try:
            client.break_cpu()
            _, latest = _selected_text_screen(client, options)
        finally:
            client.detach()
        if options.text in latest:
            print(latest)
            return 0
        if time.monotonic() >= deadline:
            print(latest)
            print(
                f"rsp.py: text not observed within {options.wait_timeout:g}s: "
                f"{options.text!r}",
                file=sys.stderr,
            )
            return 1
        time.sleep(options.interval)


def _trace(options: argparse.Namespace) -> int:
    breakpoints = options.addresses or [0xF24A0]
    client = _connect(options)
    hits: list[tuple[int, int, int]] = []
    try:
        client.break_cpu()
        for address in breakpoints:
            client.set_hw_bp(address)
        while True:
            try:
                packet = client.cmd(b"c")
            except (socket.timeout, TimeoutError):
                break
            registers = stop_regs(packet)
            eip = reg_val(registers.get(REG_EIP, b"0"))
            if eip not in breakpoints:
                continue
            eax = reg_val(registers.get(REG_EAX, b"0"))
            ss = reg16(registers.get(REG_SS, b"0"))
            stack_pointer = reg16(registers.get(REG_ESP, b"0"))
            try:
                caller = int.from_bytes(
                    client.read_mem(((ss << 4) + stack_pointer) & 0xFFFFF, 2),
                    "little",
                )
            except OSError:
                caller = -1
            hits.append((caller, eax & 0xFF, eip))
            if options.count and len(hits) >= options.count:
                break
    finally:
        client.detach()

    for caller, character, eip in hits:
        printable = chr(character) if 32 <= character < 127 else " "
        print(f"[{eip & 0xFFFF:04x}->{caller:04x}]{printable}", end="")
    print(f"\n({len(hits)} hits)")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    options = _argument_parser().parse_args(arguments)
    try:
        if options.action == "probe":
            return _probe(options)
        if options.action == "dump":
            return _dump(options)
        if options.action == "video-mode":
            return _video_mode(options)
        if options.action == "screen":
            return _screen(options)
        if options.action == "type":
            return _type_keys(options)
        if options.action == "wait-screen":
            return _wait_screen(options)
        if options.action == "trace":
            return _trace(options)
        raise AssertionError(options.action)
    except (ConnectionError, OSError, ValueError, socket.timeout) as error:
        print(f"rsp.py: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
