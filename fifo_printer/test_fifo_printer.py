import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from fifo_printer import (
    ACK_PULSE_SECONDS,
    ACK_SEQUENCE,
    READY_STATUS,
    PageWriter,
    Paginator,
    PrinterStatus,
    TerminalMonitor,
    process_chunk,
    output_directory_for,
    read_lpt1_pipe_base,
    resolve_vm_path,
    require_fifos,
)


class FifoPrinterTest(unittest.TestCase):
    def test_streams_safe_preview_and_saves_raw_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            display = io.StringIO()
            monitor = TerminalMonitor(display)
            pages = PageWriter(output_dir)
            paginator = Paginator(pages)

            monitor.start(
                Path("/tmp/lpt1fifo.out"),
                Path("/tmp/lpt1fifo.in"),
                output_dir,
            )
            process_chunk(b"first line\r\n", paginator, monitor)
            process_chunk(b"raw bytes: \x00\x1b\xff\fsecond page\f", paginator, monitor)
            self.assertIsNone(pages.close())

            self.assertEqual(
                (output_dir / "page0001.txt").read_bytes(),
                b"first line\r\nraw bytes: \x00\x1b\xff",
            )
            self.assertEqual(
                (output_dir / "page0002.txt").read_bytes(),
                b"second page",
            )

            preview = display.getvalue()
            self.assertIn("86Box FIFO Printer Monitor", preview)
            self.assertIn("first line\r\nraw bytes: ^@^[\\xff", preview)
            self.assertNotIn("\\F", preview)
            self.assertIn("new page — saved page0001.txt", preview)
            self.assertIn("new page — saved page0002.txt", preview)
            self.assertNotIn("\x1b", preview)


    def test_omits_form_feed_suffix_from_console_without_changing_disk_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            paginator = Paginator(PageWriter(output_dir))
            data = b"A\fB\f\rC\f\nD\f\r\nE\f\n\rF\f"

            events = paginator.consume(data)

            self.assertEqual(
                [path.read_bytes() for path in sorted(output_dir.glob("page*.txt"))],
                [b"A", b"B", b"\rC", b"\nD", b"\r\nE", b"\n\rF"],
            )
            displayed = b"".join(
                value
                for kind, value in events
                if kind == "data" and isinstance(value, bytes)
            )
            self.assertEqual(displayed, b"ABCDEF")

    def test_preserves_first_character_after_form_feed_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            paginator = Paginator(PageWriter(output_dir))

            events = paginator.consume(b"previous\f\r\nThis is attempt\f")

            self.assertEqual(
                [path.read_bytes() for path in sorted(output_dir.glob("page*.txt"))],
                [b"previous", b"\r\nThis is attempt"],
            )
            displayed = b"".join(
                value
                for kind, value in events
                if kind == "data" and isinstance(value, bytes)
            )
            self.assertEqual(displayed, b"previousThis is attempt")


    def test_63_rows_breaks_console_without_rewriting_disk_pages(self) -> None:
        full_page = b"row\r\n" * 63
        cases = (
            (b"NEXT", [full_page + b"NEXT"]),
            (b"\f\r\nNEXT", [full_page, b"\r\nNEXT"]),
        )
        for continuation, expected_pages in cases:
            with self.subTest(continuation=continuation):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    output_dir = Path(temporary_directory)
                    pages = PageWriter(output_dir)
                    paginator = Paginator(pages)

                    events = paginator.consume(full_page)
                    events.extend(paginator.consume(continuation))
                    pages.close()

                    self.assertEqual(
                        [
                            path.read_bytes()
                            for path in sorted(output_dir.glob("page*.txt"))
                        ],
                        expected_pages,
                    )
                    self.assertEqual(
                        [kind for kind, _ in events].count("row_break"),
                        1,
                    )
                    if continuation.startswith(b"\f"):
                        self.assertIn(("saved", output_dir / "page0001.txt"), events)

    def test_port_close_finishes_page_and_suppresses_reconnect_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            paginator = Paginator(PageWriter(output_dir))

            paginator.consume(b"before close")
            events = paginator.port_closed()
            reconnect_events = paginator.consume(b"\r\nafter close\f")

            self.assertEqual(events[0][0], "finish")
            self.assertEqual(
                [path.read_bytes() for path in sorted(output_dir.glob("page*.txt"))],
                [b"before close", b"\r\nafter close"],
            )
            displayed = b"".join(
                value
                for kind, value in reconnect_events
                if kind == "data" and isinstance(value, bytes)
            )
            self.assertEqual(displayed, b"after close")


    def test_never_creates_86box_fifos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            pipe_base = Path(temporary_directory) / "lpt1fifo"
            data_fifo = Path(f"{pipe_base}.out")
            status_fifo = Path(f"{pipe_base}.in")

            with self.assertRaisesRegex(RuntimeError, "start the VM first"):
                require_fifos(pipe_base)
            self.assertFalse(data_fifo.exists())
            self.assertFalse(status_fifo.exists())

    def test_reports_ready_and_acknowledges_each_byte(self) -> None:
        output = io.BytesIO()
        status = PrinterStatus(output)

        status.ready()
        with mock.patch("fifo_printer.time.sleep") as sleep:
            status.acknowledge(2)

        self.assertEqual(
            output.getvalue(),
            bytes((READY_STATUS,)) + ACK_SEQUENCE * 2,
        )
        self.assertEqual(ACK_SEQUENCE, bytes((0x03, READY_STATUS)))
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(ACK_PULSE_SECONDS), mock.call(ACK_PULSE_SECONDS)],
        )


    def test_reads_relative_fifo_path_from_vm_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vm_path = Path(temporary_directory)
            (vm_path / "86box.cfg").write_text(
                "[Named Pipe (LPT) #1]\n"
                "lpt_mode = 3\n"
                "path = devices/lpt1fifo\n"
                "\n"
                "[Ports (COM & LPT)]\n"
                "lpt1_device = pipe\n",
                encoding="utf-8",
            )

            self.assertEqual(
                read_lpt1_pipe_base(vm_path),
                vm_path / "devices/lpt1fifo",
            )

    def test_rejects_non_strobed_lpt_pipe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vm_path = Path(temporary_directory)
            (vm_path / "86box.cfg").write_text(
                "[Named Pipe (LPT) #1]\n"
                "lpt_mode = 0\n"
                "path = lpt1fifo\n"
                "\n"
                "[Ports (COM & LPT)]\n"
                "lpt1_device = pipe\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Printer .* strobe"):
                read_lpt1_pipe_base(vm_path)

    def test_defaults_pages_to_vm_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vm_path = Path(temporary_directory)

            self.assertEqual(
                output_directory_for(vm_path, None),
                vm_path / "printed-pages",
            )

    def test_rejects_newline_in_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vm_path = Path(temporary_directory)

            with self.assertRaisesRegex(RuntimeError, "control character"):
                output_directory_for(vm_path, Path("printed-pages\n"))



    def test_resolves_vm_name_under_default_root(self) -> None:
        vm_root = Path("/virtual-machines")

        self.assertEqual(
            resolve_vm_path(Path("OS21302"), vm_root),
            vm_root / "OS21302",
        )

    def test_preserves_absolute_vm_path(self) -> None:
        vm_path = Path("/custom/VMs/OS21302")

        self.assertEqual(resolve_vm_path(vm_path, Path("/unused")), vm_path)


if __name__ == "__main__":
    unittest.main()
