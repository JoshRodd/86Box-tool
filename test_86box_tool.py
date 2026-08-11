from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zlib


TOOL_DIRECTORY = Path(__file__).resolve().parent
CONTROL = TOOL_DIRECTORY / "86boxctl.py"
RSP_PATH = TOOL_DIRECTORY / "rsp.py"
SMOKE_PATH = TOOL_DIRECTORY / "model80_rom_smoke.py"
CONSOLE_PATH = TOOL_DIRECTORY / "guest_console.py"
SCREENMON_PATH = TOOL_DIRECTORY / "screenmon.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_rsp_module():
    return _load_module("box_rsp", RSP_PATH)


def _load_screenmon_modules():
    rsp = _load_module("rsp", RSP_PATH)
    console = _load_module("box_guest_console", CONSOLE_PATH)
    screenmon = _load_module("box_screenmon", SCREENMON_PATH)
    return rsp, console, screenmon


class ControllerTests(unittest.TestCase):
    def test_two_instances_are_tracked_and_stopped_independently(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm_root = root / "vms"
            run_directory = root / "run"
            fake_binary = root / "fake-86box"
            fake_binary.write_text(
                "#!/usr/bin/env python3\n"
                "import signal, time\n"
                "running = True\n"
                "def stop(*_):\n"
                "    global running\n"
                "    running = False\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "print('fake 86Box ready', flush=True)\n"
                "while running:\n"
                "    time.sleep(0.05)\n",
                encoding="utf-8",
            )
            fake_binary.chmod(0o755)
            for name, port in (("Alpha VM", 12345), ("Beta VM", 12346)):
                directory = vm_root / name
                directory.mkdir(parents=True)
                (directory / "86box.cfg").write_text(
                    f"[General]\ngdbstub_port = {port}\n"
                    "[Machine]\nmachine = ibmps2_m80\n",
                    encoding="utf-8",
                )

            def control(name: str, action: str, *, check: bool = True):
                return subprocess.run(
                    (
                        sys.executable,
                        str(CONTROL),
                        name,
                        action,
                        "--vm-root",
                        str(vm_root),
                        "--run-dir",
                        str(run_directory),
                        "--binary",
                        str(fake_binary),
                        "--startup-grace",
                        "0.05",
                        "--timeout",
                        "2",
                    ),
                    check=check,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

            alpha_pid = beta_pid = None
            try:
                alpha = control("Alpha VM", "start")
                beta = control("Beta VM", "start")
                alpha_match = re.search(r"PID (\d+)", alpha.stdout)
                beta_match = re.search(r"PID (\d+)", beta.stdout)
                self.assertIsNotNone(alpha_match)
                self.assertIsNotNone(beta_match)
                assert alpha_match is not None and beta_match is not None
                alpha_pid = int(alpha_match.group(1))
                beta_pid = int(beta_match.group(1))
                self.assertNotEqual(alpha_pid, beta_pid)

                self.assertEqual(control("Alpha VM", "status").returncode, 0)
                self.assertEqual(control("Beta VM", "status").returncode, 0)

                control("Alpha VM", "stop")
                self.assertEqual(
                    control("Alpha VM", "status", check=False).returncode,
                    1,
                )
                beta_status = control("Beta VM", "status")
                self.assertIn(f"PID {beta_pid}", beta_status.stdout)
                self.assertIn("identity verified", beta_status.stdout)

                command = control("Beta VM", "command").stdout
                self.assertIn(f"--vmpath '{(vm_root / 'Beta VM').resolve()}'", command)
                self.assertIn("--vmname 'Beta VM'", command)
                self.assertNotIn("86box.cfg", command)
            finally:
                if alpha_pid is not None:
                    control("Alpha VM", "stop", check=False)
                if beta_pid is not None:
                    control("Beta VM", "stop", check=False)

    def test_stop_refuses_a_reused_or_unrelated_pid(self) -> None:
        controller = _load_module("box_control", CONTROL)
        unrelated = subprocess.Popen(("sleep", "30"))
        record = controller.ProcessRecord(
            pid=unrelated.pid,
            vm_name="Not This Process",
            vm_directory="/tmp/not-this-vm",
            config_path="/tmp/not-this-vm/86box.cfg",
            machine="ibmps2_m80",
            binary="/Applications/86Box.app/Contents/MacOS/86Box",
            command=(),
            log_path="/tmp/not-this-vm.log",
            gdbstub_port=None,
            started_at=0.0,
        )
        try:
            observation = controller.observe_process(record)
            self.assertTrue(observation.running)
            self.assertFalse(observation.identity_verified)
            with self.assertRaisesRegex(controller.ControlError, "refusing to signal"):
                controller.stop_process(record, 0.1, kill_after=False)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


class PatchedRomTests(unittest.TestCase):
    def test_platform_rom_roots_match_86box_conventions(self) -> None:
        controller = _load_module("box_control_paths", CONTROL)
        home = Path("/home/tester")
        self.assertEqual(
            controller.platform_rom_root_candidates(
                platform_name="darwin",
                environment={},
                home=home,
            )[0],
            home / "Library/Application Support/net.86box.86Box/roms",
        )
        self.assertEqual(
            controller.platform_rom_root_candidates(
                platform_name="linux",
                environment={"XDG_DATA_HOME": "/data/home"},
                home=home,
            )[0],
            Path("/data/home/86Box/roms"),
        )
        self.assertEqual(
            controller.platform_rom_root_candidates(
                platform_name="win32",
                environment={"LOCALAPPDATA": "C:/Local"},
                home=home,
            )[0],
            Path("C:/Local/86Box/roms"),
        )

    def test_patched_launch_clones_and_splits_sequential_model80_rom(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm_root = root / "vms"
            vm = vm_root / "Patched VM"
            vm.mkdir(parents=True)
            (vm / "86box.cfg").write_text(
                "[General]\n[Machine]\nmachine = ibmps2_m80\n",
                encoding="utf-8",
            )

            source = root / "stock-roms"
            model80 = source / "machines/ibmps2_m80"
            model80.mkdir(parents=True)
            original_low = b"L" * 65536
            original_high = b"H" * 65536
            (model80 / "15f6637.bin").write_bytes(original_low)
            (model80 / "15f6639.bin").write_bytes(original_high)
            unrelated = source / "video/example/unchanged.bin"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b"unchanged ROM asset")

            invalid_image = root / "invalid.bin"
            invalid_image.write_bytes(b"not 128 KiB")
            controller = _load_module("box_control_roms", CONTROL)
            resolved_vm = controller.resolve_vm("Patched VM", vm_root)
            with self.assertRaisesRegex(controller.ControlError, "exactly 131072"):
                controller.prepare_model80_type2_rom_set(
                    resolved_vm,
                    invalid_image,
                    source_root=source,
                )

            image = bytes(range(256)) * 512
            image_path = root / "patched-model80.bin"
            image_path.write_bytes(image)
            fake_binary = root / "fake-86box"
            fake_binary.write_text(
                "#!/usr/bin/env python3\n"
                "import signal, time\n"
                "running = True\n"
                "def stop(*_):\n"
                "    global running\n"
                "    running = False\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while running:\n"
                "    time.sleep(0.05)\n",
                encoding="utf-8",
            )
            fake_binary.chmod(0o755)
            run_directory = root / "run"

            def control(action: str, *, check: bool = True):
                return subprocess.run(
                    (
                        sys.executable,
                        str(CONTROL),
                        "Patched VM",
                        action,
                        "--vm-root",
                        str(vm_root),
                        "--run-dir",
                        str(run_directory),
                        "--binary",
                        str(fake_binary),
                        "--model-80-type-2-rom",
                        str(image_path),
                        "--rom-source",
                        str(source),
                        "--startup-grace",
                        "0.05",
                        "--timeout",
                        "2",
                    ),
                    check=check,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

            started = False
            try:
                launch = control("start")
                started = True
                self.assertIn("ROM path:", launch.stdout)
                records = list(run_directory.glob("*.json"))
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text(encoding="utf-8"))
                patched_root = Path(record["rom_path"])
                self.assertTrue(patched_root.is_relative_to((vm / "romsets").resolve()))
                self.assertIn("--rompath", record["command"])
                self.assertIn(str(patched_root), record["command"])

                low = (patched_root / controller.MODEL80_TYPE2_LOW_PATH).read_bytes()
                high = (patched_root / controller.MODEL80_TYPE2_HIGH_PATH).read_bytes()
                self.assertEqual(low, image[0::2])
                self.assertEqual(high, image[1::2])
                recombined = bytearray(len(image))
                recombined[0::2] = low
                recombined[1::2] = high
                self.assertEqual(bytes(recombined), image)
                self.assertEqual(
                    (patched_root / "video/example/unchanged.bin").read_bytes(),
                    unrelated.read_bytes(),
                )
                self.assertEqual(
                    (model80 / "15f6637.bin").read_bytes(),
                    original_low,
                )
                self.assertEqual(
                    (model80 / "15f6639.bin").read_bytes(),
                    original_high,
                )

                status = control("status")
                self.assertIn(str(patched_root), status.stdout)
            finally:
                if started:
                    control("stop", check=False)

            prepared_again = control("prepare-roms")
            self.assertIn("reused patched ROM set", prepared_again.stdout)

            unrelated.write_bytes(b"updated stock asset")
            refreshed = controller.prepare_model80_type2_rom_set(
                resolved_vm,
                image_path,
                source_root=source,
                refresh=True,
            )
            self.assertFalse(refreshed.reused)
            self.assertEqual(
                (refreshed.root / "video/example/unchanged.bin").read_bytes(),
                b"updated stock asset",
            )
            self.assertEqual(
                (refreshed.root / controller.MODEL80_TYPE2_LOW_PATH).read_bytes(),
                image[0::2],
            )
            generated_names = {path.name for path in (vm / "romsets").iterdir()}
            self.assertFalse(
                any(".tmp-" in name or ".old-" in name for name in generated_names)
            )

    def test_smoke_patch_rewrites_post_text_and_repairs_checksum(self) -> None:
        smoke = _load_module("box_model80_rom_smoke", SMOKE_PATH)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "roms"
            model80 = source / smoke.LOW_ROM.parent
            model80.mkdir(parents=True)

            image = bytearray(smoke.IMAGE_SIZE)
            text_offset = 0x1E05F
            image[text_offset : text_offset + len(smoke.ORIGINAL_TEXT)] = (
                smoke.ORIGINAL_TEXT
            )
            for start in range(0, len(image), smoke.CHECKSUM_REGION_SIZE):
                checksum_offset = start + smoke.CHECKSUM_REGION_SIZE - 1
                residual = (
                    sum(image[start : start + smoke.CHECKSUM_REGION_SIZE]) & 0xFF
                )
                image[checksum_offset] = (image[checksum_offset] - residual) & 0xFF
            original = bytes(image)
            (source / smoke.LOW_ROM).write_bytes(original[0::2])
            (source / smoke.HIGH_ROM).write_bytes(original[1::2])

            result = smoke.build_patched_image(source, root / "output")
            patched = result.path.read_bytes()
            self.assertEqual(len(patched), smoke.IMAGE_SIZE)
            self.assertEqual(result.text_offset, text_offset)
            self.assertEqual(
                patched[text_offset : text_offset + len(smoke.PATCHED_TEXT)],
                smoke.PATCHED_TEXT,
            )
            self.assertNotIn(smoke.ORIGINAL_TEXT, patched)
            self.assertEqual(
                result.checksum_after,
                (result.checksum_before - 0x34) & 0xFF,
            )
            self.assertEqual(sum(patched[:0x10000]) & 0xFF, 0)
            self.assertEqual(sum(patched[0x10000:]) & 0xFF, 0)
            self.assertEqual((source / smoke.LOW_ROM).read_bytes(), original[0::2])
            self.assertEqual((source / smoke.HIGH_ROM).read_bytes(), original[1::2])



class GuestConsoleTests(unittest.TestCase):
    @staticmethod
    def _memory_client():
        class MemoryClient:
            def __init__(self) -> None:
                self.memory = bytearray(0x100000)

            def read_mem(self, address: int, length: int) -> bytes:
                return bytes(self.memory[address : address + length])

            def write_mem(self, address: int, data: bytes) -> None:
                self.memory[address : address + len(data)] = data

        return MemoryClient()

    @staticmethod
    def _load_console_modules():
        rsp = _load_module("rsp", RSP_PATH)
        console = _load_module("box_guest_console", CONSOLE_PATH)
        return rsp, console


    def test_all_supported_bios_text_modes_select_geometry_and_memory(self) -> None:
        rsp, _ = self._load_console_modules()
        client = self._memory_client()
        for number, (columns, rows, base, colour) in rsp.VIDEO_MODE_LAYOUTS.items():
            client.memory[rsp.BDA_VIDEO_STATE_ADDRESS] = number
            client.memory[
                rsp.BDA_VIDEO_STATE_ADDRESS + 1 : rsp.BDA_VIDEO_STATE_ADDRESS + 3
            ] = columns.to_bytes(2, "little")
            client.memory[
                rsp.BDA_VIDEO_STATE_ADDRESS + 5 : rsp.BDA_VIDEO_STATE_ADDRESS + 7
            ] = (0x100).to_bytes(2, "little")
            client.memory[rsp.BDA_VIDEO_STATE_ADDRESS + 25] = 1
            client.memory[
                rsp.BDA_VIDEO_STATE_ADDRESS + 9 : rsp.BDA_VIDEO_STATE_ADDRESS + 11
            ] = bytes((columns - 1, rows - 1))
            mode = rsp.read_video_text_mode(client)
            self.assertEqual(mode.number, number)
            self.assertEqual((mode.columns, mode.rows), (columns, rows))
            self.assertEqual(mode.memory_address, base + 0x100)
            self.assertEqual(mode.colour, colour)
            self.assertEqual((mode.cursor_row, mode.cursor_column), (24, columns - 1))

        client.memory[rsp.BDA_VIDEO_STATE_ADDRESS] = 0x13
        with self.assertRaisesRegex(ValueError, "expected 0, 1, 2, 3, or 7"):
            rsp.read_video_text_mode(client)

    def test_screen_buffer_emits_only_changed_character_runs(self) -> None:
        rsp, console = self._load_console_modules()
        mode = rsp.VideoTextMode(3, 80, 25, 0xB8000, 0, 0, 0, 0, True)
        cells = bytearray(b" \x07" * (80 * 25))
        initial = console.TextScreenBuffer()
        first = initial.update(rsp.VideoTextFrame(mode, bytes(cells)))
        self.assertTrue(first.reset)
        self.assertEqual(len(first.deltas), 25)

        offset = (1 * 80 + 2) * 2
        cells[offset : offset + 6] = b"A\x07B\x07C\x07"
        second = initial.update(rsp.VideoTextFrame(mode, bytes(cells)))
        self.assertFalse(second.reset)
        self.assertEqual(
            second.deltas,
            (console.ScreenDelta(row=1, column=2, text="ABC"),),
        )

        moved = rsp.VideoTextMode(3, 80, 25, 0xB8000, 0, 0, 4, 10, True)
        third = initial.update(rsp.VideoTextFrame(moved, bytes(cells)))
        self.assertEqual(third.deltas, ())
        self.assertTrue(third.cursor_changed)

    def test_keyboard_injection_obeys_bios_ring_and_host_key_sequences(self) -> None:
        rsp, console = self._load_console_modules()
        client = self._memory_client()
        client.memory[rsp.BDA_KEYBOARD_HEAD : rsp.BDA_KEYBOARD_HEAD + 4] = (
            b"\x1e\x00\x1e\x00"
        )
        client.memory[rsp.BDA_KEYBOARD_BOUNDS : rsp.BDA_KEYBOARD_BOUNDS + 4] = (
            b"\x1e\x00\x3e\x00"
        )
        keys = rsp.encode_bios_text("A\n")
        self.assertEqual(rsp.inject_bios_keys(client, keys), 2)
        self.assertEqual(client.memory[0x41E:0x422], b"A\x1e\r\x1c")
        self.assertEqual(client.memory[0x41C:0x41E], b"\x22\x00")

        decoder = console.HostKeyDecoder()
        decoded, should_exit = decoder.feed(b"dir\r\x1b[A\x1d")
        self.assertTrue(should_exit)
        self.assertEqual(
            decoded,
            rsp.encode_bios_text("dir\r") + [rsp.named_bios_key("UP")],
        )
        self.assertEqual(
            rsp.hardware_scan_bytes(rsp.encode_bios_text("Aa\x03")),
            [0x2A, 0x1E, 0x9E, 0xAA, 0x1E, 0x9E, 0x1D, 0x2E, 0xAE, 0x9D],
        )
        self.assertEqual(
            rsp.hardware_scan_bytes([rsp.named_bios_key("ENTER")]),
            [0x1C, 0x9C],
        )
        self.assertEqual(
            rsp.hardware_scan_bytes([rsp.named_bios_key("UP")]),
            [0xE0, 0x48, 0xE0, 0xC8],
        )
        self.assertEqual(
            rsp.hardware_scan_bytes([rsp.named_bios_key("F11")]),
            [0x57, 0xD7],
        )

        class MonitorClient:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def monitor(self, command: str) -> None:
                self.commands.append(command)

        monitor = MonitorClient()
        rsp.inject_keyboard_scan_byte(monitor, 0x3B)
        self.assertEqual(monitor.commands, ["kb 0x3b 1"])

        monitor.commands.clear()
        hardware_queue = console.GuestInputQueue("hardware")
        hardware_queue.extend([rsp.named_bios_key("F1")])
        # Make is injected immediately; the break waits out the key delay.
        self.assertEqual(hardware_queue.inject(monitor, 0.0), 1)
        self.assertTrue(hardware_queue.waiting_for_break(0.1))
        self.assertEqual(hardware_queue.inject(monitor, 0.1), 0)
        self.assertEqual(hardware_queue.inject(monitor, 0.5), 1)
        self.assertFalse(hardware_queue.waiting_for_break(0.6))
        self.assertEqual(
            monitor.commands,
            ["kb 0x3b 1", "kb 0x3b 0"],
        )

        partial_escape = console.HostKeyDecoder()
        self.assertEqual(partial_escape.feed(b"\x1b["), ([], False))
        partial_escape.escape_started = 0.0
        flushed, should_exit = partial_escape.flush_escape()
        self.assertFalse(should_exit)
        self.assertEqual(
            flushed,
            [rsp.named_bios_key("ESC")] + rsp.encode_bios_text("["),
        )


class RspRenderingTests(unittest.TestCase):
    def test_text_screen_renderer_decodes_cp437_and_ignores_attributes(self) -> None:
        rsp = _load_rsp_module()
        cells = bytearray()
        for character in b"A\x00\xDB ":
            cells.extend((character, 0x1F))
        rendered = rsp.render_text_screen(bytes(cells), columns=4, rows=1)
        self.assertEqual(rendered, "A █")

    def test_mode_11_frame_encodes_exact_packed_pixels_as_png(self) -> None:
        rsp = _load_rsp_module()

        class MemoryClient:
            def __init__(self) -> None:
                self.memory = bytearray(0x100000)

            def read_mem(self, address: int, length: int) -> bytes:
                return bytes(self.memory[address : address + length])

        client = MemoryClient()
        client.memory[rsp.BDA_VIDEO_STATE_ADDRESS] = rsp.VGA_MODE_11
        pixels = bytes(
            (index * 37) & 0xFF for index in range(rsp.VGA_MODE_11_BYTES)
        )
        client.memory[
            rsp.VGA_GRAPHICS_ADDRESS :
            rsp.VGA_GRAPHICS_ADDRESS + len(pixels)
        ] = pixels

        frame = rsp.read_mode_11_frame(client)
        self.assertEqual((frame.width, frame.height), (640, 480))
        self.assertEqual(frame.pixels, pixels)

        png = rsp.encode_monochrome_png(frame)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        chunks: dict[bytes, bytes] = {}
        offset = 8
        while offset < len(png):
            size = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4 : offset + 8]
            payload = png[offset + 8 : offset + 8 + size]
            chunks[kind] = payload
            offset += 12 + size
        self.assertEqual(
            struct.unpack(">IIBBBBB", chunks[b"IHDR"]),
            (640, 480, 1, 0, 0, 0, 0),
        )
        scanlines = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(len(scanlines), 480 * 81)
        for row in range(480):
            start = row * 81
            self.assertEqual(scanlines[start], 0)
            self.assertEqual(
                scanlines[start + 1 : start + 81],
                pixels[row * 80 : (row + 1) * 80],
            )

    def test_mode_12_planes_encode_as_indexed_4bpp_png(self) -> None:
        rsp = _load_rsp_module()
        planes = []
        for first in (0xAA, 0xCC, 0xF0, 0x0F):
            plane = bytearray(rsp.VGA_MODE_12_PLANE_BYTES)
            plane[0] = first
            planes.append(bytes(plane))

        packed = rsp.pack_mode_12_planes(planes)
        self.assertEqual(len(packed), rsp.VGA_MODE_12_PACKED_BYTES)
        self.assertEqual(packed[:4], b"\x76\x54\xBA\x98")
        self.assertFalse(any(packed[4:]))

        frame = rsp.VideoGraphicsFrame(
            rsp.VGA_MODE_12,
            rsp.VGA_MODE_12_WIDTH,
            rsp.VGA_MODE_12_HEIGHT,
            packed,
        )
        png = rsp.encode_graphics_png(frame)
        chunks: dict[bytes, bytes] = {}
        offset = 8
        while offset < len(png):
            size = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4 : offset + 8]
            chunks[kind] = png[offset + 8 : offset + 8 + size]
            offset += 12 + size
        self.assertEqual(
            struct.unpack(">IIBBBBB", chunks[b"IHDR"]),
            (640, 480, 4, 3, 0, 0, 0),
        )
        self.assertEqual(chunks[b"PLTE"], rsp.VGA_16_COLOR_PALETTE)

    def test_graphics_interpreter_invokes_harness_vision_role(self) -> None:
        rsp = _load_rsp_module()
        frame = rsp.VideoGraphicsFrame(0x11, 640, 480, bytes(80 * 480))
        observed: list[str] = []

        def run(command, **options):
            observed.extend(command)
            image_argument = next(item for item in command if item.startswith("@/"))
            self.assertTrue(Path(image_argument[1:]).read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(options["capture_output"], True)
            self.assertEqual(options["text"], True)
            return SimpleNamespace(returncode=0, stdout="Visible dialog", stderr="")

        with (
            patch.object(rsp.shutil, "which", return_value="/opt/bin/omp"),
            patch.object(rsp.subprocess, "run", side_effect=run),
        ):
            answer = rsp.interpret_graphics_frame(frame, "Read this screen")

        self.assertEqual(answer, "Visible dialog")
        self.assertIn("@vision", observed)
        self.assertIn("Read this screen", observed)


    def test_screenmon_choose_mode_honors_isatty_and_overrides(self) -> None:
        _rsp, _console, screenmon = _load_screenmon_modules()

        class FakeStream:
            def __init__(self, isatty_value: bool) -> None:
                self.isatty_value = isatty_value

            def isatty(self) -> bool:
                return self.isatty_value

        self.assertEqual(screenmon.choose_mode(FakeStream(True), None), "full")
        self.assertEqual(screenmon.choose_mode(FakeStream(False), None), "simple")
        self.assertEqual(screenmon.choose_mode(FakeStream(True), "simple"), "simple")
        self.assertEqual(screenmon.choose_mode(FakeStream(False), "full"), "full")

    def test_screenmon_simple_renderer_full_frame_then_changed_rows(self) -> None:
        rsp, console, screenmon = _load_screenmon_modules()
        mode = rsp.VideoTextMode(3, 80, 25, 0xB8000, 0, 0, 0, 0, True)
        cells = bytearray(b" \x07" * (80 * 25))
        buffer = console.TextScreenBuffer()
        first = buffer.update(rsp.VideoTextFrame(mode, bytes(cells)))
        rendered = screenmon.render_simple(first, 1.5)
        self.assertIn("===    1.5s — reset — full frame ===", rendered)
        self.assertNotIn("R00:", rendered)  # blank rows are omitted

        # write "F? 1,C" at row 17, columns 10-15 (chars at even offsets)
        for i, ch in enumerate("F? 1,C"):
            cells[17 * 160 + (10 + i) * 2] = ord(ch)
        second = buffer.update(rsp.VideoTextFrame(mode, bytes(cells)))
        rendered = screenmon.render_simple(second, 3.0)
        self.assertIn("---    3.0s — 1 changed row(s) ---", rendered)
        self.assertIn("R17:", rendered)
        self.assertNotIn("R00:", rendered)

    def test_screenmon_full_renderer_paints_and_overwrites_in_place(self) -> None:
        rsp, console, screenmon = _load_screenmon_modules()
        mode = rsp.VideoTextMode(3, 80, 25, 0xB8000, 0, 0, 0, 0, True)
        cells = bytearray(b" \x07" * (80 * 25))
        buffer = console.TextScreenBuffer()
        first = buffer.update(rsp.VideoTextFrame(mode, bytes(cells)))
        rendered = screenmon.render_full(first)
        self.assertTrue(rendered.startswith("\x1b[2J\x1b[H\x1b[?25l"))
        self.assertIn("\r\n", rendered)

        cells[5 * 160 + 20 * 2] = ord("X")
        second = buffer.update(rsp.VideoTextFrame(mode, bytes(cells)))
        rendered = screenmon.render_full(second)
        self.assertNotIn("\x1b[2J", rendered)
        self.assertIn("\x1b[6;21HX", rendered)

    def test_screenmon_default_mode_matches_stream(self) -> None:
        _rsp, _console, screenmon = _load_screenmon_modules()
        self.assertEqual(screenmon.choose_mode(sys.stdout, None), "simple")


if __name__ == "__main__":
    unittest.main()
