#!/usr/bin/env python3
"""Receive 86Box LPT output and split it into form-feed-delimited pages."""

from __future__ import annotations

import argparse
import configparser
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import BinaryIO, TextIO

PAGE_PATTERN = re.compile(r"page(\d{4,})\.txt\Z")
READ_SIZE = 1
ROWS_PER_PAGE = 63
DEFAULT_VM_ROOT = Path.home() / "Library/Application Support/86Box/Virtual Machines"

# lpt_mode=3 status bytes. Bits 0..4 map to nError, Select, PaperOut,
# nAck, and Busy; 86Box turns them into the PC status register with
# (status << 3) ^ 0x80. Match the native printer's one-microsecond ACK pulse.
READY_STATUS = 0x0B
ACK_LOW_STATUS = 0x03
ACK_SEQUENCE = bytes((ACK_LOW_STATUS, READY_STATUS))
ACK_PULSE_SECONDS = 0.000001


class PrinterStatus:
    def __init__(self, output: BinaryIO) -> None:
        self.output = output

    def _write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = self.output.write(remaining)
            if not written:
                raise BrokenPipeError("86Box status FIFO closed")
            remaining = remaining[written:]
        self.output.flush()

    def ready(self) -> None:
        self._write_all(bytes((READY_STATUS,)))

    def acknowledge(self, byte_count: int) -> None:
        for _ in range(byte_count):
            self._write_all(bytes((ACK_LOW_STATUS,)))
            time.sleep(ACK_PULSE_SECONDS)
            self._write_all(bytes((READY_STATUS,)))


class TerminalMonitor:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.color = self.stream.isatty() and "NO_COLOR" not in os.environ
        self.at_line_start = True

    def _styled(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _line(self, text: str = "", style: str = "") -> None:
        self._ensure_line()
        self.stream.write(self._styled(text, style) if style else text)
        self.stream.write("\n")
        self.stream.flush()
        self.at_line_start = True

    def _ensure_line(self) -> None:
        if not self.at_line_start:
            self.stream.write("\n")
            self.at_line_start = True

    def start(self, data_fifo: Path, status_fifo: Path, output_dir: Path) -> None:
        self._line("86Box FIFO Printer Monitor", "1;36")
        self._line(f"  Data:   {data_fifo}")
        self._line(f"  Status: {status_fifo}")
        self._line(f"  Pages:  {output_dir}")
        self._line("waiting for printer data", "2")

    def connected(self) -> None:
        self._line("printer connected", "32")

    def waiting(self) -> None:
        self._line("printer disconnected; waiting for data", "2")

    def page_started(self, path: Path) -> None:
        self._line(f"── {path.name} ──", "1;36")

    def write(self, data: bytes) -> None:
        display: list[str] = []
        for byte in data:
            if byte in (0x09, 0x0a, 0x0d) or 0x20 <= byte <= 0x7e:
                character = chr(byte)
            elif byte == 0x7f:
                character = "^?"
            elif byte < 0x20:
                character = f"^{chr(byte + 0x40)}"
            else:
                character = f"\\x{byte:02x}"
            display.append(character)
            self.at_line_start = character == "\n"
        self.stream.write("".join(display))
        self.stream.flush()

    def page_finished(self, path: Path) -> None:
        self._line(f"new page — saved {path.name}", "1;32")

    def page_saved(self, path: Path) -> None:
        self._line(f"saved {path.name}", "1;32")

    def row_page_break(self) -> None:
        self._line("new page — 63 rows", "1;32")

    def stopped(self, partial_page: Path | None) -> None:
        if partial_page is not None:
            self._line(f"stopped — partial page saved as {partial_page.name}", "33")
        else:
            self._line("stopped", "33")




class PageWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.next_number = self._find_next_number()
        self.output: BinaryIO | None = None
        self.current_path: Path | None = None

    def _find_next_number(self) -> int:
        numbers = (
            int(match.group(1))
            for path in self.output_dir.iterdir()
            if path.is_file() and (match := PAGE_PATTERN.fullmatch(path.name))
        )
        return max(numbers, default=0) + 1

    def _ensure_page(self) -> tuple[Path, bool]:
        if self.output is not None and self.current_path is not None:
            return self.current_path, False
        while True:
            path = self.output_dir / f"page{self.next_number:04d}.txt"
            self.next_number += 1
            try:
                self.output = path.open("xb")
                self.current_path = path
                return path, True
            except FileExistsError:
                continue

    def write(self, data: bytes) -> tuple[Path, bool]:
        path, started = self._ensure_page()
        output = self.output
        assert output is not None
        output.write(data)
        output.flush()
        return path, started

    def form_feed(self) -> tuple[Path, bool]:
        path, started = self._ensure_page()
        output = self.output
        assert output is not None
        output.close()
        self.output = None
        self.current_path = None
        return path, started

    def close(self) -> Path | None:
        path = self.current_path
        if self.output is not None:
            self.output.close()
            self.output = None
            self.current_path = None
        return path
class Paginator:
    def __init__(self, pages: PageWriter) -> None:
        self.pages = pages
        self.rows = 0
        self.automatic_break_pending = False
        self.suppress_after_break = False
        self.suppressed_cr = False
        self.suppressed_lf = False

    def _begin_suffix_suppression(self) -> None:
        self.suppress_after_break = True
        self.suppressed_cr = False
        self.suppressed_lf = False

    def _flush(
        self,
        disk_data: bytearray,
        display_data: bytearray,
        events: list[tuple[str, object]],
    ) -> None:
        if disk_data:
            path, started = self.pages.write(bytes(disk_data))
            disk_data.clear()
            if started:
                events.append(("start", path))
        if display_data:
            events.append(("data", bytes(display_data)))
            display_data.clear()

    def consume(self, data: bytes) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        disk_data = bytearray()
        display_data = bytearray()
        for byte in data:
            if byte == 0x0C:
                self._flush(disk_data, display_data, events)
                path, started = self.pages.form_feed()
                if started:
                    events.append(("start", path))
                events.append(
                    ("saved" if self.automatic_break_pending else "finish", path)
                )
                self.rows = 0
                self.automatic_break_pending = False
                self._begin_suffix_suppression()
                continue

            disk_data.append(byte)
            if self.suppress_after_break:
                if byte == 0x0D and not self.suppressed_cr:
                    self.suppressed_cr = True
                    continue
                if byte == 0x0A and not self.suppressed_lf:
                    self.suppressed_lf = True
                    continue
                self.suppress_after_break = False

            self.automatic_break_pending = False
            display_data.append(byte)
            if byte == 0x0A:
                self.rows += 1
                if self.rows == ROWS_PER_PAGE:
                    self._flush(disk_data, display_data, events)
                    events.append(("row_break", None))
                    self.rows = 0
                    self.automatic_break_pending = True

        self._flush(disk_data, display_data, events)
        return events

    def port_closed(self) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        if self.pages.current_path is not None:
            path = self.pages.close()
            assert path is not None
            events.append(
                ("saved" if self.automatic_break_pending else "finish", path)
            )
        self.rows = 0
        self.automatic_break_pending = False
        self._begin_suffix_suppression()
        return events




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read bytes written by an 86Box UNIX Named Pipe (LPT) device and "
            "save form-feed-delimited pages as page0001.txt, page0002.txt, and so on."
        )
    )
    parser.add_argument(
        "--vm",
        required=True,
        type=Path,
        metavar="NAME_OR_PATH",
        help=f"VM name under {DEFAULT_VM_ROOT}, or an absolute VM/config path",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directory for page files (default: <VM>/printed-pages)",
    )
    return parser.parse_args()


def resolve_vm_path(vm: Path, vm_root: Path = DEFAULT_VM_ROOT) -> Path:
    vm = vm.expanduser()
    return vm if vm.is_absolute() else vm_root / vm


def config_path_for(vm: Path) -> Path:
    return vm / "86box.cfg" if vm.is_dir() else vm


def output_directory_for(vm: Path, requested: Path | None) -> Path:
    output_dir = (
        config_path_for(vm).parent / "printed-pages"
        if requested is None
        else requested.expanduser()
    )
    if any(character in str(output_dir) for character in "\r\n\0"):
        raise RuntimeError(f"invalid control character in output directory: {output_dir!r}")
    return output_dir


def read_lpt1_pipe_base(vm: Path) -> Path:
    config_path = config_path_for(vm)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with config_path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except FileNotFoundError:
        raise RuntimeError(f"86Box configuration does not exist: {config_path}") from None
    except configparser.Error as error:
        raise RuntimeError(f"invalid 86Box configuration {config_path}: {error}") from error

    if parser.get("Ports (COM & LPT)", "lpt1_device", fallback="") != "pipe":
        raise RuntimeError(f"LPT1 is not configured as a named pipe in {config_path}")

    lpt_mode = parser.getint("Named Pipe (LPT) #1", "lpt_mode", fallback=-1)
    if lpt_mode != 3:
        raise RuntimeError(
            "LPT1 named pipe must use Parallel cable "
            "'Raw SPP with handshaking' (lpt_mode = 3)"
        )

    pipe_value = parser.get("Named Pipe (LPT) #1", "path", fallback="").strip()
    if not pipe_value:
        raise RuntimeError(f"LPT1 named-pipe path is missing from {config_path}")

    pipe_base = Path(pipe_value).expanduser()
    if not pipe_base.is_absolute():
        pipe_base = config_path.parent / pipe_base
    return pipe_base


def require_fifos(pipe_base: Path) -> tuple[Path, Path]:
    data_fifo = Path(f"{pipe_base}.out")
    status_fifo = Path(f"{pipe_base}.in")
    for role, fifo_path in (("data", data_fifo), ("status", status_fifo)):
        try:
            mode = fifo_path.stat().st_mode
        except FileNotFoundError:
            raise RuntimeError(
                f"86Box {role} FIFO does not exist: {fifo_path}; start the VM first"
            ) from None
        if not stat.S_ISFIFO(mode):
            raise RuntimeError(f"86Box {role} path is not a FIFO: {fifo_path}")
    return data_fifo, status_fifo


def emit_events(events: list[tuple[str, object]], monitor: TerminalMonitor) -> None:
    for kind, value in events:
        if kind == "start":
            assert isinstance(value, Path)
            monitor.page_started(value)
        elif kind == "data":
            assert isinstance(value, bytes)
            monitor.write(value)
        elif kind == "saved":
            assert isinstance(value, Path)
            monitor.page_saved(value)
        elif kind == "row_break":
            monitor.row_page_break()
        else:
            assert isinstance(value, Path)
            monitor.page_finished(value)


def process_chunk(chunk: bytes, paginator: Paginator, monitor: TerminalMonitor) -> None:
    emit_events(paginator.consume(chunk), monitor)

def receive(
    data_fifo_path: Path,
    status_fifo_path: Path,
    paginator: Paginator,
    monitor: TerminalMonitor,
) -> None:
    while True:
        with data_fifo_path.open("rb", buffering=0) as data_fifo:
            with status_fifo_path.open("wb", buffering=0) as status_fifo:
                status = PrinterStatus(status_fifo)
                status.ready()
                monitor.connected()
                while chunk := data_fifo.read(READ_SIZE):
                    process_chunk(chunk, paginator, monitor)
                    status.acknowledge(len(chunk))
                emit_events(paginator.port_closed(), monitor)
                monitor.waiting()


def main() -> int:
    args = parse_args()
    try:
        vm = resolve_vm_path(args.vm)
        pipe_base = read_lpt1_pipe_base(vm)
        data_fifo, status_fifo = require_fifos(pipe_base)
        pages = PageWriter(output_directory_for(vm, args.output_dir))
        paginator = Paginator(pages)
        monitor = TerminalMonitor()
        monitor.start(data_fifo, status_fifo, pages.output_dir)
        try:
            receive(data_fifo, status_fifo, paginator, monitor)
        except KeyboardInterrupt:
            return 0
        finally:
            monitor.stopped(pages.close())
    except (OSError, RuntimeError) as error:
        print(f"fifo_printer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
