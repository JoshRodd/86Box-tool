#!/usr/bin/env python3
"""Dependency-free RSP client for observing an 86Box emulated machine."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Iterable, Sequence
import zlib


REG_EAX, REG_ESP, REG_EIP, REG_CS, REG_SS = 0, 4, 8, 10, 11
TEXT_VIDEO_ADDRESS = 0xB8000
TEXT_COLUMNS = 80
TEXT_ROWS = 25
BDA_VIDEO_STATE_ADDRESS = 0x449
VGA_GRAPHICS_ADDRESS = 0xA0000
VGA_MODE_11 = 0x11
VGA_MODE_11_WIDTH = 640
VGA_MODE_11_HEIGHT = 480
VGA_MODE_11_STRIDE = VGA_MODE_11_WIDTH // 8
VGA_MODE_11_BYTES = VGA_MODE_11_STRIDE * VGA_MODE_11_HEIGHT
VGA_MODE_12 = 0x12
VGA_MODE_12_WIDTH = 640
VGA_MODE_12_HEIGHT = 480
VGA_MODE_12_PLANE_STRIDE = VGA_MODE_12_WIDTH // 8
VGA_MODE_12_PLANE_BYTES = VGA_MODE_12_PLANE_STRIDE * VGA_MODE_12_HEIGHT
VGA_MODE_12_PACKED_STRIDE = VGA_MODE_12_WIDTH // 2
VGA_MODE_12_PACKED_BYTES = VGA_MODE_12_PACKED_STRIDE * VGA_MODE_12_HEIGHT
VGA_GRAPHICS_INDEX_PORT = 0x3CE
VGA_GRAPHICS_DATA_PORT = 0x3CF
VGA_ATTRIBUTE_ADDRESS_PORT = 0x3C0
VGA_ATTRIBUTE_DATA_PORT = 0x3C1
VGA_DAC_STATE_PORT = 0x3C7
VGA_DAC_WRITE_INDEX_PORT = 0x3C8
VGA_DAC_DATA_PORT = 0x3C9
VGA_INPUT_STATUS_1_PORT = 0x3DA
VGA_ATTRIBUTE_MODE_CONTROL = 0x10
VGA_ATTRIBUTE_COLOR_SELECT = 0x14
VGA_16_COLOR_PALETTE = bytes(
    component
    for colour in (
        (0x00, 0x00, 0x00),
        (0x00, 0x00, 0xAA),
        (0x00, 0xAA, 0x00),
        (0x00, 0xAA, 0xAA),
        (0xAA, 0x00, 0x00),
        (0xAA, 0x00, 0xAA),
        (0xAA, 0x55, 0x00),
        (0xAA, 0xAA, 0xAA),
        (0x55, 0x55, 0x55),
        (0x55, 0x55, 0xFF),
        (0x55, 0xFF, 0x55),
        (0x55, 0xFF, 0xFF),
        (0xFF, 0x55, 0x55),
        (0xFF, 0x55, 0xFF),
        (0xFF, 0xFF, 0x55),
        (0xFF, 0xFF, 0xFF),
    )
    for component in colour
)
RSP_MEMORY_READ_CHUNK = 0x1FFF
DEFAULT_VISION_QUESTION = (
    "Inspect this 640x480 86Box guest display. Describe the active screen and "
    "quote all legible text verbatim. Report any error or dialog. State "
    "uncertainty where pixels are ambiguous."
)
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

    def monitor_output(self, command: str) -> str:
        """Run an 86Box GDB monitor command and return its console output."""

        self._send_packet(b"qRcmd," + command.encode().hex().encode())
        output = bytearray()
        while True:
            response = self._recv_packet()
            if response == b"OK":
                return output.decode(errors="replace")
            if response.startswith(b"O"):
                try:
                    output.extend(bytes.fromhex(response[1:].decode()))
                except (UnicodeError, ValueError) as error:
                    raise OSError(
                        f"malformed monitor output for {command!r}: {response!r}"
                    ) from error
                continue
            try:
                output.extend(bytes.fromhex(response.decode()))
            except (UnicodeError, ValueError):
                pass
            else:
                return output.decode(errors="replace")
            raise OSError(
                f"monitor command failed: {command!r}: {response!r}"
            )

    def monitor(self, command: str) -> None:
        """Run a side-effect-only 86Box GDB monitor command."""

        self.monitor_output(command)

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
            conn.sendall(f"dr {address:x} {length:x}\n".encode())
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
class VideoGraphicsFrame:
    mode: int
    width: int
    height: int
    pixels: bytes
    palette: bytes | None = None

@dataclass(frozen=True)
class BIOSKey:
    ascii: int
    scan: int


def read_bios_video_mode(client: RSP) -> int:
    """Read the active BIOS video mode number from the BIOS data area."""

    return client.read_mem(BDA_VIDEO_STATE_ADDRESS, 1)[0]


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


def _read_graphics_memory(client: RSP, length: int) -> bytes:
    pixels = bytearray()
    for offset in range(0, length, RSP_MEMORY_READ_CHUNK):
        chunk = min(RSP_MEMORY_READ_CHUNK, length - offset)
        pixels.extend(client.read_mem(VGA_GRAPHICS_ADDRESS + offset, chunk))
    if len(pixels) != length:
        raise ValueError(
            f"graphics framebuffer has {len(pixels)} bytes; expected {length}"
        )
    return bytes(pixels)


def read_mode_11_frame(client: RSP) -> VideoGraphicsFrame:
    mode = read_bios_video_mode(client)
    if mode != VGA_MODE_11:
        raise ValueError(
            f"BIOS video mode is {mode:#04x}, not mode {VGA_MODE_11:#04x}"
        )
    return VideoGraphicsFrame(
        mode=mode,
        width=VGA_MODE_11_WIDTH,
        height=VGA_MODE_11_HEIGHT,
        pixels=_read_graphics_memory(client, VGA_MODE_11_BYTES),
    )


def _read_io_byte(client: RSP, port: int) -> int:
    output = client.monitor_output(f"ib {port:x} 1").strip()
    try:
        return int(output.rsplit(":", 1)[1].strip().split()[0], 16)
    except (IndexError, ValueError) as error:
        raise OSError(
            f"malformed I/O read response for port {port:#06x}: {output!r}"
        ) from error


def _write_io_byte(client: RSP, port: int, value: int) -> None:
    client.monitor(f"ob {port:x} {value & 0xFF:x}")


def read_mode_12_palette(client: RSP) -> bytes:
    """Read the live Attribute Controller mapping and its 16 DAC colours."""

    saved_attribute = _read_io_byte(client, VGA_ATTRIBUTE_ADDRESS_PORT)
    registers: list[int] = []
    try:
        for index in range(16):
            _read_io_byte(client, VGA_INPUT_STATUS_1_PORT)
            _write_io_byte(
                client,
                VGA_ATTRIBUTE_ADDRESS_PORT,
                (saved_attribute & 0x20) | index,
            )
            registers.append(_read_io_byte(client, VGA_ATTRIBUTE_DATA_PORT))
        for index in (VGA_ATTRIBUTE_MODE_CONTROL, VGA_ATTRIBUTE_COLOR_SELECT):
            _read_io_byte(client, VGA_INPUT_STATUS_1_PORT)
            _write_io_byte(
                client,
                VGA_ATTRIBUTE_ADDRESS_PORT,
                (saved_attribute & 0x20) | index,
            )
            registers.append(_read_io_byte(client, VGA_ATTRIBUTE_DATA_PORT))
    finally:
        _read_io_byte(client, VGA_INPUT_STATUS_1_PORT)
        _write_io_byte(client, VGA_ATTRIBUTE_ADDRESS_PORT, saved_attribute)

    mode_control, color_select = registers[16:]
    dac_indices = []
    for value in registers[:16]:
        middle = (
            (color_select & 0x03) << 4
            if mode_control & 0x80
            else value & 0x30
        )
        dac_indices.append(
            ((color_select & 0x0C) << 4) | middle | (value & 0x0F)
        )

    dac_state = _read_io_byte(client, VGA_DAC_STATE_PORT) & 0x03
    saved_index = _read_io_byte(client, VGA_DAC_WRITE_INDEX_PORT)
    palette = bytearray()
    try:
        for index in dac_indices:
            _write_io_byte(client, VGA_DAC_STATE_PORT, index)
            for _ in range(3):
                component = _read_io_byte(client, VGA_DAC_DATA_PORT) & 0x3F
                palette.append((component << 2) | (component >> 4))
    finally:
        if dac_state == 3:
            _write_io_byte(client, VGA_DAC_STATE_PORT, saved_index)
        else:
            _write_io_byte(client, VGA_DAC_WRITE_INDEX_PORT, saved_index)
    return bytes(palette)


def pack_mode_12_planes(planes: Sequence[bytes]) -> bytes:
    """Combine four MSB-first VGA planes into packed 4bpp pixel rows."""

    if len(planes) != 4:
        raise ValueError(f"mode 12h requires four planes, received {len(planes)}")
    if any(len(plane) != VGA_MODE_12_PLANE_BYTES for plane in planes):
        lengths = ", ".join(str(len(plane)) for plane in planes)
        raise ValueError(
            f"mode 12h planes must each be {VGA_MODE_12_PLANE_BYTES} bytes; "
            f"received {lengths}"
        )
    packed = bytearray(VGA_MODE_12_PACKED_BYTES)
    for offset, (plane_0, plane_1, plane_2, plane_3) in enumerate(
        zip(*planes, strict=True)
    ):
        target = offset * 4
        for pair in range(4):
            low_bit = 6 - pair * 2
            high_bit = low_bit + 1
            high = (
                ((plane_0 >> high_bit) & 1)
                | (((plane_1 >> high_bit) & 1) << 1)
                | (((plane_2 >> high_bit) & 1) << 2)
                | (((plane_3 >> high_bit) & 1) << 3)
            )
            low = (
                ((plane_0 >> low_bit) & 1)
                | (((plane_1 >> low_bit) & 1) << 1)
                | (((plane_2 >> low_bit) & 1) << 2)
                | (((plane_3 >> low_bit) & 1) << 3)
            )
            packed[target + pair] = (high << 4) | low
    return bytes(packed)


def read_mode_12_frame(client: RSP) -> VideoGraphicsFrame:
    mode = read_bios_video_mode(client)
    if mode != VGA_MODE_12:
        raise ValueError(
            f"BIOS video mode is {mode:#04x}, not mode {VGA_MODE_12:#04x}"
        )

    original_index = _read_io_byte(client, VGA_GRAPHICS_INDEX_PORT)
    original_read_map = original_mode = 0
    have_read_map = have_mode = False
    planes: list[bytes] = []
    try:
        _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, 4)
        original_read_map = _read_io_byte(client, VGA_GRAPHICS_DATA_PORT)
        have_read_map = True
        _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, 5)
        original_mode = _read_io_byte(client, VGA_GRAPHICS_DATA_PORT)
        have_mode = True
        _write_io_byte(client, VGA_GRAPHICS_DATA_PORT, original_mode & ~0x08)
        _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, 4)
        for plane in range(4):
            _write_io_byte(client, VGA_GRAPHICS_DATA_PORT, plane)
            planes.append(
                _read_graphics_memory(client, VGA_MODE_12_PLANE_BYTES)
            )
    finally:
        if have_mode:
            _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, 5)
            _write_io_byte(client, VGA_GRAPHICS_DATA_PORT, original_mode)
        if have_read_map:
            _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, 4)
            _write_io_byte(client, VGA_GRAPHICS_DATA_PORT, original_read_map)
        _write_io_byte(client, VGA_GRAPHICS_INDEX_PORT, original_index)

    return VideoGraphicsFrame(
        mode=mode,
        width=VGA_MODE_12_WIDTH,
        height=VGA_MODE_12_HEIGHT,
        pixels=pack_mode_12_planes(planes),
        palette=read_mode_12_palette(client),
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def encode_monochrome_png(frame: VideoGraphicsFrame) -> bytes:
    """Encode packed, MSB-first 1bpp rows as a dependency-free PNG."""

    stride = (frame.width + 7) // 8
    expected = stride * frame.height
    if frame.width <= 0 or frame.height <= 0:
        raise ValueError("frame dimensions must be positive")
    if frame.width % 8:
        raise ValueError("1bpp PNG width must be a multiple of 8 pixels")
    if len(frame.pixels) != expected:
        raise ValueError(
            f"framebuffer has {len(frame.pixels)} bytes; expected {expected}"
        )

    scanlines = bytearray((stride + 1) * frame.height)
    for row in range(frame.height):
        source = row * stride
        target = row * (stride + 1) + 1
        scanlines[target : target + stride] = frame.pixels[source : source + stride]
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def encode_mode_12_png(frame: VideoGraphicsFrame) -> bytes:
    """Encode packed 4bpp mode 12h pixels with the captured VGA palette."""

    stride = (frame.width + 1) // 2
    expected = stride * frame.height
    if frame.width <= 0 or frame.height <= 0:
        raise ValueError("frame dimensions must be positive")
    if frame.width % 2:
        raise ValueError("4bpp PNG width must be an even number of pixels")
    if len(frame.pixels) != expected:
        raise ValueError(
            f"framebuffer has {len(frame.pixels)} bytes; expected {expected}"
        )
    scanlines = bytearray((stride + 1) * frame.height)
    for row in range(frame.height):
        source = row * stride
        target = row * (stride + 1) + 1
        scanlines[target : target + stride] = frame.pixels[source : source + stride]
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 4, 3, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"PLTE", frame.palette or VGA_16_COLOR_PALETTE)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def encode_graphics_png(frame: VideoGraphicsFrame) -> bytes:
    if frame.mode == VGA_MODE_11:
        return encode_monochrome_png(frame)
    if frame.mode == VGA_MODE_12:
        return encode_mode_12_png(frame)
    raise ValueError(f"unsupported graphics mode {frame.mode:#04x}")


def interpret_graphics_frame(
    frame: VideoGraphicsFrame,
    question: str = DEFAULT_VISION_QUESTION,
) -> str:
    """Send a graphics frame to the harness's configured vision model."""

    omp = shutil.which("omp")
    if omp is None:
        raise OSError("cannot interpret graphics screen: omp is not on PATH")
    with TemporaryDirectory(prefix="86box-screen-") as temporary:
        image_path = Path(temporary) / f"mode{frame.mode:02x}.png"
        image_path.write_bytes(encode_graphics_png(frame))
        command = [
            omp,
            "--print",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-lsp",
            "--model",
            "@vision",
            "--system-prompt",
            (
                "You are a precise visual screen reader. Answer only from the "
                "attached image and do not infer hidden state."
            ),
            f"@{image_path}",
            question,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    answer = result.stdout.strip()
    if result.returncode:
        detail = result.stderr.strip() or answer or f"exit status {result.returncode}"
        raise OSError(f"vision model failed: {detail}")
    if not answer:
        raise OSError("vision model returned no interpretation")
    return answer


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

    commands.add_parser("video-mode", help="inspect the active BIOS video mode")

    screen = commands.add_parser(
        "screen",
        help="render text VRAM or interpret a mode 11h graphics screen",
    )
    screen.add_argument("--address", type=_hex_integer)
    screen.add_argument("--columns", type=int)
    screen.add_argument("--rows", type=int)
    screen.add_argument(
        "--png",
        type=Path,
        help="write a graphics-mode capture to this PNG instead of invoking vision",
    )
    screen.add_argument(
        "--vision-question",
        default=DEFAULT_VISION_QUESTION,
        help="question sent with a mode 11h screen to the harness vision model",
    )

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
        number = read_bios_video_mode(client)
        if number == VGA_MODE_11:
            detail = (
                f"mode {number} (0x11): {VGA_MODE_11_WIDTH}x{VGA_MODE_11_HEIGHT} "
                f"1bpp graphics, base {VGA_GRAPHICS_ADDRESS:05X}h, "
                f"{VGA_MODE_11_BYTES} bytes"
            )
        elif number == VGA_MODE_12:
            detail = (
                f"mode {number} (0x12): {VGA_MODE_12_WIDTH}x{VGA_MODE_12_HEIGHT} "
                f"4bpp planar graphics, base {VGA_GRAPHICS_ADDRESS:05X}h, "
                f"4 x {VGA_MODE_12_PLANE_BYTES} bytes"
            )
        else:
            mode = read_video_text_mode(client)
            kind = "colour" if mode.colour else "mono"
            detail = (
                f"mode {mode.number}: {mode.columns}x{mode.rows} {kind}, "
                f"base {mode.base_address:05X}h, page {mode.active_page}, "
                f"offset {mode.page_offset:04X}h, "
                f"address {mode.memory_address:05X}h, "
                f"cursor {mode.cursor_row},{mode.cursor_column}"
            )
    finally:
        client.detach()
    print(detail)
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
    graphics = None
    text = ""
    try:
        client.break_cpu()
        number = read_bios_video_mode(client)
        if number in (VGA_MODE_11, VGA_MODE_12):
            if any(
                value is not None
                for value in (options.address, options.columns, options.rows)
            ):
                raise ValueError(
                    "--address, --columns, and --rows apply only to text modes"
                )
            if number == VGA_MODE_11:
                graphics = read_mode_11_frame(client)
            else:
                graphics = read_mode_12_frame(client)
        else:
            _, text = _selected_text_screen(client, options)
    finally:
        client.detach()
    if graphics is not None:
        if options.png is not None:
            options.png.write_bytes(encode_graphics_png(graphics))
            text = str(options.png)
        else:
            text = interpret_graphics_frame(graphics, options.vision_question)
    elif options.png is not None:
        raise ValueError("--png requires VGA graphics mode 11h or 12h")
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
