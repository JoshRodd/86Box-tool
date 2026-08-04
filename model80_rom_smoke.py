#!/usr/bin/env python3
"""Patch the Model 80 POST text, boot it in real 86Box, and verify the screen."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence


TOOL_DIRECTORY = Path(__file__).resolve().parent
CONTROLLER = TOOL_DIRECTORY / "86boxctl.py"
RSP_CLIENT = TOOL_DIRECTORY / "rsp.py"
LOW_ROM = Path("machines/ibmps2_m80/15f6637.bin")
HIGH_ROM = Path("machines/ibmps2_m80/15f6639.bin")
LANE_SIZE = 64 * 1024
IMAGE_SIZE = 2 * LANE_SIZE
BIOS_BASE = 0xE0000
CHECKSUM_REGION_SIZE = 0x10000
ORIGINAL_TEXT = b"KB OK"
PATCHED_TEXT = b"OKBMR"


class SmokeError(RuntimeError):
    """The ROM proof could not be constructed or observed."""


@dataclass(frozen=True)
class PatchResult:
    path: Path
    source_sha256: str
    patched_sha256: str
    text_offset: int
    checksum_offset: int
    checksum_before: int
    checksum_after: int


def _sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def _controller_module() -> Any:
    spec = importlib.util.spec_from_file_location("box_control_for_rom_smoke", CONTROLLER)
    if spec is None or spec.loader is None:
        raise SmokeError(f"cannot load controller: {CONTROLLER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _interleave(low: bytes, high: bytes) -> bytearray:
    if len(low) != LANE_SIZE or len(high) != LANE_SIZE:
        raise SmokeError(
            "Model 80 Type 2 lanes must each be exactly "
            f"{LANE_SIZE} bytes; found {len(low)} and {len(high)}"
        )
    image = bytearray(IMAGE_SIZE)
    image[0::2] = low
    image[1::2] = high
    return image


def _verify_region_checksums(image: bytes | bytearray) -> None:
    for start in range(0, len(image), CHECKSUM_REGION_SIZE):
        residual = sum(image[start : start + CHECKSUM_REGION_SIZE]) & 0xFF
        if residual:
            raise SmokeError(
                f"BIOS checksum region {start:#07x}-{start + CHECKSUM_REGION_SIZE - 1:#07x} "
                f"has residual {residual:#04x}, expected 0x00"
            )


def build_patched_image(source_root: Path, output_directory: Path) -> PatchResult:
    """Build a sequential 128 KiB image with the POST text and checksum patched."""

    source = source_root.expanduser().resolve()
    low_path = source / LOW_ROM
    high_path = source / HIGH_ROM
    if not low_path.is_file() or not high_path.is_file():
        raise SmokeError(
            f"source ROM root lacks {LOW_ROM} and {HIGH_ROM}: {source}"
        )

    image = _interleave(low_path.read_bytes(), high_path.read_bytes())
    _verify_region_checksums(image)
    source_sha256 = _sha256(image)

    text_offset = image.find(ORIGINAL_TEXT)
    if text_offset < 0:
        raise SmokeError(f"source BIOS does not contain {ORIGINAL_TEXT!r}")
    if image.find(ORIGINAL_TEXT, text_offset + 1) >= 0:
        raise SmokeError(f"source BIOS contains more than one {ORIGINAL_TEXT!r}")
    if len(ORIGINAL_TEXT) != len(PATCHED_TEXT):
        raise AssertionError("the smoke-test text replacement must preserve length")
    image[text_offset : text_offset + len(ORIGINAL_TEXT)] = PATCHED_TEXT

    region_start = text_offset & ~(CHECKSUM_REGION_SIZE - 1)
    checksum_offset = region_start + CHECKSUM_REGION_SIZE - 1
    checksum_before = image[checksum_offset]
    residual = sum(image[region_start : region_start + CHECKSUM_REGION_SIZE]) & 0xFF
    image[checksum_offset] = (checksum_before - residual) & 0xFF
    checksum_after = image[checksum_offset]
    _verify_region_checksums(image)

    if bytes(image[text_offset : text_offset + len(PATCHED_TEXT)]) != PATCHED_TEXT:
        raise AssertionError("POST text patch did not persist")
    patched_sha256 = _sha256(image)
    output = output_directory.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    image_path = output / f"model80-okbmr-{source_sha256[:16]}.bin"
    if not image_path.is_file() or image_path.read_bytes() != image:
        temporary = image_path.with_name(f".{image_path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_bytes(image)
            temporary.replace(image_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    return PatchResult(
        path=image_path,
        source_sha256=source_sha256,
        patched_sha256=patched_sha256,
        text_offset=text_offset,
        checksum_offset=checksum_offset,
        checksum_before=checksum_before,
        checksum_after=checksum_after,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clone the stock Model 80 ROM set, replace the POST memory text "
            "'KB OK' with 'OKBMR', launch real 86Box, and observe it via RSP."
        )
    )
    parser.add_argument("instance", help="Model 80 VM name or directory")
    parser.add_argument("--vm-root", type=Path)
    parser.add_argument("--rom-source", type=Path)
    parser.add_argument("--binary", type=Path, help="GDB-stub-enabled 86Box binary")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for OKBMR on the guest screen (default: 120)",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave the verified VM running instead of stopping its tracked PID",
    )
    return parser


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        details = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part)
        raise SmokeError(
            f"command failed with status {result.returncode}: "
            f"{subprocess.list2cmdline(command)}\n{details}"
        )
    return result


def _controller_command(
    instance: Path,
    action: str,
    *,
    vm_root: Path,
    run_directory: Path,
) -> list[str]:
    return [
        sys.executable,
        str(CONTROLLER),
        str(instance),
        action,
        "--vm-root",
        str(vm_root),
        "--run-dir",
        str(run_directory),
    ]


def main(arguments: Sequence[str] | None = None) -> int:
    options = _argument_parser().parse_args(arguments)
    if options.wait_timeout <= 0:
        print("model80_rom_smoke.py: error: --wait-timeout must be positive", file=sys.stderr)
        return 2

    controller = _controller_module()
    vm_root = (
        options.vm_root.expanduser().resolve()
        if options.vm_root is not None
        else controller.DEFAULT_VM_ROOT
    )
    run_directory = (
        options.run_dir.expanduser().resolve()
        if options.run_dir is not None
        else controller.DEFAULT_RUN_DIRECTORY
    )

    launched = False
    verified = False
    vm = None
    try:
        vm = controller.resolve_vm(options.instance, vm_root)
        if vm.machine != "ibmps2_m80":
            raise SmokeError(
                f"{vm.name} uses machine {vm.machine!r}; expected 'ibmps2_m80'"
            )
        if vm.gdbstub_port is None:
            raise SmokeError(f"{vm.config_path} has no [General] gdbstub_port")
        source = controller.resolve_source_rom_root(options.rom_source)
        patch = build_patched_image(source, vm.directory / "rom-patches")

        print(f"Stock ROM root: {source}")
        print(f"Sequential source SHA-256: {patch.source_sha256}")
        print(
            f"Patched {ORIGINAL_TEXT!r} -> {PATCHED_TEXT!r} at file offset "
            f"{patch.text_offset:#07x}, physical address {BIOS_BASE + patch.text_offset:#07x}"
        )
        print(
            f"Checksum byte {patch.checksum_offset:#07x}: "
            f"{patch.checksum_before:#04x} -> {patch.checksum_after:#04x}"
        )
        print(f"Patched image: {patch.path}")
        print(f"Patched image SHA-256: {patch.patched_sha256}")

        launch = _controller_command(
            vm.directory,
            "start",
            vm_root=vm_root,
            run_directory=run_directory,
        )
        launch.extend(
            (
                "--gdb-stub",
                "--model-80-type-2-rom",
                str(patch.path),
                "--rom-source",
                str(source),
                "--startup-grace",
                "0.25",
                "--timeout",
                "15",
                "--json",
            )
        )
        if options.binary is not None:
            launch.extend(("--binary", str(options.binary.expanduser().resolve())))
        launch_result = _run(launch)
        launched = True
        launch_data = json.loads(launch_result.stdout)
        print(
            f"Launched real 86Box PID {launch_data['pid']} with ROM path "
            f"{launch_data['rom_path']}"
        )

        wait_result = _run(
            (
                sys.executable,
                str(RSP_CLIENT),
                "--port",
                str(vm.gdbstub_port),
                "--timeout",
                "10",
                "wait-screen",
                PATCHED_TEXT.decode("ascii"),
                "--interval",
                "0.05",
                "--wait-timeout",
                str(options.wait_timeout),
            )
        )
        matching_lines = [
            line for line in wait_result.stdout.splitlines() if PATCHED_TEXT.decode("ascii") in line
        ]
        if not matching_lines:
            raise SmokeError("RSP reported success without returning the patched text")
        print("Observed guest POST text:")
        for line in matching_lines:
            print(f"  {line}")
        verified = True
        return 0
    except (controller.ControlError, SmokeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"model80_rom_smoke.py: error: {error}", file=sys.stderr)
        return 2
    finally:
        if launched and not (verified and options.keep_running) and vm is not None:
            stop = _controller_command(
                vm.directory,
                "stop",
                vm_root=vm_root,
                run_directory=run_directory,
            )
            stop.extend(("--timeout", "10", "--kill-after-timeout"))
            stopped = subprocess.run(
                stop,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if stopped.returncode:
                print(stopped.stderr.rstrip(), file=sys.stderr)
            elif stopped.stdout:
                print(stopped.stdout.rstrip())


if __name__ == "__main__":
    raise SystemExit(main())
