#!/usr/bin/env python3
"""Launch and control exact 86Box VM processes without global process matching."""

from __future__ import annotations

import argparse
from collections import deque
import configparser
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


TOOL_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_VM_ROOT = Path.home() / "Library/Application Support/86Box/Virtual Machines"
DEFAULT_RELEASE_BINARY = Path("/Applications/86Box.app/Contents/MacOS/86Box")
DEFAULT_DEBUG_BINARY = (
    Path.home() / "src/86Box/build/regular/src/86Box.app/Contents/MacOS/86Box"
)
DEFAULT_RUN_DIRECTORY = TOOL_DIRECTORY / "run"
MODEL80_TYPE2_ROM_SIZE = 128 * 1024
MODEL80_TYPE2_LOW_PATH = Path("machines/ibmps2_m80/15f6637.bin")
MODEL80_TYPE2_HIGH_PATH = Path("machines/ibmps2_m80/15f6639.bin")
ROM_SET_MANIFEST = ".86boxctl-romset.json"
ROM_SET_FORMAT = 1


class ControlError(RuntimeError):
    """The requested operation is unsafe or cannot be completed."""


@dataclass(frozen=True)
class VmInstance:
    name: str
    directory: Path
    config_path: Path
    machine: str | None
    gdbstub_port: int | None


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    vm_name: str
    vm_directory: str
    config_path: str
    machine: str | None
    binary: str
    command: tuple[str, ...]
    log_path: str
    gdbstub_port: int | None
    started_at: float
    rom_path: str | None = None

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> ProcessRecord:
        try:
            command = data["command"]
            if not isinstance(command, list) or not all(
                isinstance(argument, str) for argument in command
            ):
                raise TypeError("command must be a string list")
            return cls(
                pid=int(data["pid"]),
                vm_name=str(data["vm_name"]),
                vm_directory=str(data["vm_directory"]),
                config_path=str(data["config_path"]),
                machine=None if data.get("machine") is None else str(data["machine"]),
                binary=str(data["binary"]),
                command=tuple(command),
                log_path=str(data["log_path"]),
                gdbstub_port=(
                    None
                    if data.get("gdbstub_port") is None
                    else int(data["gdbstub_port"])
                ),
                rom_path=(
                    None if data.get("rom_path") is None else str(data["rom_path"])
                ),
                started_at=float(data["started_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ControlError(f"invalid process record: {error}") from error


@dataclass(frozen=True)
class ProcessObservation:
    running: bool
    identity_verified: bool
    command_line: str | None


@dataclass(frozen=True)
class PreparedRomSet:
    root: Path
    source_root: Path
    manifest_path: Path
    image_sha256: str
    reused: bool


def _positive_float(value: str) -> float:
    result = float(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="86boxctl",
        description=(
            "Control one exact 86Box VM process. Never scans for or signals every "
            "86Box process."
        ),
    )
    parser.add_argument("instance", help="VM name under --vm-root, or a VM path")
    parser.add_argument(
        "action",
        choices=(
            "command",
            "prepare-roms",
            "start",
            "restart",
            "stop",
            "status",
            "wait",
            "pid",
            "port",
            "logs",
            "forget",
        ),
    )
    parser.add_argument(
        "--vm-root",
        type=Path,
        default=Path(os.environ.get("86BOX_VM_ROOT", DEFAULT_VM_ROOT)),
        help=f"named-VM root (default: {DEFAULT_VM_ROOT})",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        help="86Box executable; defaults to the installed app or debug build",
    )
    parser.add_argument(
        "--gdb-stub",
        action="store_true",
        help="use the custom GDB-stub build and wait for its configured port",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="launch without taking focus (macOS: launch via `open -g`)",
    )
    rom_selection = parser.add_mutually_exclusive_group()
    rom_selection.add_argument(
        "--rom-path",
        type=Path,
        help="use an existing complete ROM root through 86Box --rompath",
    )
    rom_selection.add_argument(
        "--model-80-type-2-rom",
        "--model80-type2-rom",
        dest="model80_type2_rom",
        type=Path,
        metavar="128K_IMAGE",
        help="clone the standard ROM set and install this sequential 128 KiB BIOS",
    )
    parser.add_argument(
        "--rom-source",
        type=Path,
        default=(
            Path(os.environ["86BOX_ROM_ROOT"])
            if os.environ.get("86BOX_ROM_ROOT")
            else None
        ),
        help="complete ROM root to clone; defaults to the platform 86Box location",
    )
    parser.add_argument(
        "--refresh-rom-set",
        action="store_true",
        help="rebuild an existing generated ROM set from its source",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(os.environ.get("86BOX_RUN_DIR", DEFAULT_RUN_DIRECTORY)),
        help=f"process records and logs (default: {DEFAULT_RUN_DIRECTORY})",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        help="seconds to wait; wait has no timeout when omitted",
    )
    parser.add_argument(
        "--startup-grace",
        type=_positive_float,
        default=1.0,
        help="seconds the process must remain alive after launch (default: 1)",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="start on the VM settings dialog",
    )
    parser.add_argument(
        "--kill-after-timeout",
        action="store_true",
        help="after stop times out, SIGKILL this exact verified PID",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=100,
        help="lines printed by logs (default: 100)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable status")
    return parser


def resolve_vm(instance: str, vm_root: Path) -> VmInstance:
    candidate = Path(instance).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        directory = candidate.resolve()
    else:
        directory = (vm_root.expanduser() / candidate).resolve()
    if not directory.is_dir():
        raise ControlError(f"VM directory does not exist: {directory}")

    config_path = directory / "86box.cfg"
    if not config_path.is_file():
        raise ControlError(f"VM has no 86box.cfg: {directory}")

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with config_path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
        machine = parser.get("Machine", "machine", fallback=None)
        gdbstub_port = parser.getint("General", "gdbstub_port", fallback=None)
    except (OSError, configparser.Error, ValueError) as error:
        raise ControlError(f"cannot read {config_path}: {error}") from error

    return VmInstance(
        name=directory.name,
        directory=directory,
        config_path=config_path,
        machine=machine,
        gdbstub_port=gdbstub_port,
    )


def platform_rom_root_candidates(
    *,
    platform_name: str = sys.platform,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return 86Box ROM roots in platform search order."""

    values = os.environ if environment is None else environment
    home_directory = Path.home() if home is None else home
    candidates: list[Path] = []
    if platform_name == "darwin":
        for data in (
            home_directory / "Library/Application Support",
            Path("/Library/Application Support"),
        ):
            candidates.extend(
                (
                    data / "net.86box.86Box/roms",
                    data / "86Box/roms",
                )
            )
    elif platform_name == "win32":
        for variable in ("LOCALAPPDATA", "PROGRAMDATA", "APPDATA"):
            if values.get(variable):
                candidates.append(Path(values[variable]) / "86Box/roms")
    else:
        data_home = values.get("XDG_DATA_HOME")
        candidates.append(
            Path(data_home) / "86Box/roms"
            if data_home
            else home_directory / ".local/share/86Box/roms"
        )
        data_directories = values.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
        candidates.extend(
            Path(directory) / "86Box/roms"
            for directory in data_directories.split(":")
            if directory
        )

    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded not in unique:
            unique.append(expanded)
    return tuple(unique)


def resolve_source_rom_root(explicit: Path | None = None) -> Path:
    """Resolve the complete stock ROM tree used as a patching source."""

    if explicit is not None:
        candidates = (explicit.expanduser(),)
    else:
        candidates = platform_rom_root_candidates()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and (resolved / "machines").is_dir():
            return resolved
    searched = ", ".join(str(path) for path in candidates)
    raise ControlError(
        f"cannot locate a complete 86Box ROM root; searched: {searched}; "
        "supply --rom-source"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_model80_type2_image(path: Path) -> tuple[Path, bytes, str]:
    image_path = path.expanduser().resolve()
    if not image_path.is_file():
        raise ControlError(f"Model 80 Type 2 ROM image does not exist: {image_path}")
    image = image_path.read_bytes()
    if len(image) != MODEL80_TYPE2_ROM_SIZE:
        raise ControlError(
            f"Model 80 Type 2 ROM must be exactly {MODEL80_TYPE2_ROM_SIZE} bytes; "
            f"{image_path} is {len(image)} bytes"
        )
    return image_path, image, _sha256(image)


def _generated_rom_set_path(
    vm: VmInstance,
    source_root: Path,
    image_sha256: str,
) -> Path:
    source_key = hashlib.sha256(os.fsencode(source_root)).hexdigest()[:8]
    return (
        vm.directory
        / "romsets"
        / f"ibmps2-m80-type2-{image_sha256[:16]}-{source_key}"
    )


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _generated_rom_set_is_current(
    destination: Path,
    *,
    source_root: Path,
    image_sha256: str,
) -> bool:
    manifest = _read_manifest(destination / ROM_SET_MANIFEST)
    if manifest is None:
        return False
    expected = {
        "format": ROM_SET_FORMAT,
        "kind": "ibm-ps2-model-80-type-2-sequential-128k",
        "source_rom_root": str(source_root),
        "image_sha256": image_sha256,
        "low_path": str(MODEL80_TYPE2_LOW_PATH),
        "high_path": str(MODEL80_TYPE2_HIGH_PATH),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    low = destination / MODEL80_TYPE2_LOW_PATH
    high = destination / MODEL80_TYPE2_HIGH_PATH
    return (
        low.is_file()
        and high.is_file()
        and low.stat().st_size == MODEL80_TYPE2_ROM_SIZE // 2
        and high.stat().st_size == MODEL80_TYPE2_ROM_SIZE // 2
        and _sha256_file(low) == manifest.get("low_sha256")
        and _sha256_file(high) == manifest.get("high_sha256")
    )


def prepare_model80_type2_rom_set(
    vm: VmInstance,
    image_path: Path,
    *,
    source_root: Path | None = None,
    refresh: bool = False,
) -> PreparedRomSet:
    """Clone a complete ROM set and split a sequential Model 80 Type 2 BIOS."""

    if vm.machine != "ibmps2_m80":
        raise ControlError(
            f"{vm.name} uses machine {vm.machine!r}; the Model 80 Type 2 patch "
            "requires machine 'ibmps2_m80'"
        )
    source = resolve_source_rom_root(source_root)
    if (
        source == vm.directory
        or vm.directory in source.parents
        or source in vm.directory.parents
    ):
        raise ControlError("ROM source and VM directory cannot overlap")
    resolved_image, image, image_sha256 = _read_model80_type2_image(image_path)
    destination = _generated_rom_set_path(vm, source, image_sha256)
    manifest_path = destination / ROM_SET_MANIFEST

    if destination.exists() and not refresh:
        if _generated_rom_set_is_current(
            destination,
            source_root=source,
            image_sha256=image_sha256,
        ):
            return PreparedRomSet(
                destination,
                source,
                manifest_path,
                image_sha256,
                True,
            )
        raise ControlError(
            f"generated ROM set exists but failed validation: {destination}; "
            "inspect it or use --refresh-rom-set"
        )
    replacing_existing = destination.exists()
    if replacing_existing:
        existing_manifest = _read_manifest(manifest_path)
        if (
            existing_manifest is None
            or existing_manifest.get("format") != ROM_SET_FORMAT
            or existing_manifest.get("kind")
            != "ibm-ps2-model-80-type-2-sequential-128k"
        ):
            raise ControlError(
                f"refusing to replace unrecognized directory: {destination}"
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    unique_suffix = f"{os.getpid()}-{time.time_ns()}"
    temporary = destination.with_name(f".{destination.name}.tmp-{unique_suffix}")
    previous = destination.with_name(f".{destination.name}.old-{unique_suffix}")
    moved_previous = False
    try:
        shutil.copytree(source, temporary)
        low = temporary / MODEL80_TYPE2_LOW_PATH
        high = temporary / MODEL80_TYPE2_HIGH_PATH
        if not low.parent.is_dir():
            raise ControlError(
                f"source ROM set has no Model 80 directory: {low.parent}"
            )
        low_bytes = image[0::2]
        high_bytes = image[1::2]
        low.write_bytes(low_bytes)
        high.write_bytes(high_bytes)
        manifest = {
            "format": ROM_SET_FORMAT,
            "kind": "ibm-ps2-model-80-type-2-sequential-128k",
            "source_rom_root": str(source),
            "input_image": str(resolved_image),
            "image_size": len(image),
            "image_sha256": image_sha256,
            "low_path": str(MODEL80_TYPE2_LOW_PATH),
            "low_sha256": _sha256(low_bytes),
            "high_path": str(MODEL80_TYPE2_HIGH_PATH),
            "high_sha256": _sha256(high_bytes),
            "created_at": time.time(),
        }
        (temporary / ROM_SET_MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not _generated_rom_set_is_current(
            temporary,
            source_root=source,
            image_sha256=image_sha256,
        ):
            raise ControlError(f"generated ROM set failed validation: {temporary}")

        if replacing_existing:
            destination.rename(previous)
            moved_previous = True
        temporary.rename(destination)
        if not _generated_rom_set_is_current(
            destination,
            source_root=source,
            image_sha256=image_sha256,
        ):
            raise ControlError(
                f"generated ROM set failed post-write validation: {destination}"
            )
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if moved_previous and previous.exists():
            if destination.exists():
                shutil.rmtree(destination)
            previous.rename(destination)
        raise
    if moved_previous:
        shutil.rmtree(previous)
    return PreparedRomSet(destination, source, manifest_path, image_sha256, False)


def resolve_launch_rom_path(
    vm: VmInstance,
    options: argparse.Namespace,
    *,
    prepare: bool,
) -> Path | None:
    if options.rom_path is not None:
        root = options.rom_path.expanduser().resolve()
        if not root.is_dir() or not (root / "machines").is_dir():
            raise ControlError(f"--rom-path is not a complete ROM root: {root}")
        return root
    if options.model80_type2_rom is None:
        if options.rom_source is not None or options.refresh_rom_set:
            raise ControlError(
                "--rom-source and --refresh-rom-set require "
                "--model-80-type-2-rom"
            )
        return None

    source = resolve_source_rom_root(options.rom_source)
    if prepare:
        return prepare_model80_type2_rom_set(
            vm,
            options.model80_type2_rom,
            source_root=source,
            refresh=options.refresh_rom_set,
        ).root
    _, _, image_sha256 = _read_model80_type2_image(options.model80_type2_rom)
    if vm.machine != "ibmps2_m80":
        raise ControlError(
            f"{vm.name} uses machine {vm.machine!r}; the Model 80 Type 2 patch "
            "requires machine 'ibmps2_m80'"
        )
    return _generated_rom_set_path(vm, source, image_sha256)


def _record_key(vm: VmInstance) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", vm.name).strip("-.") or "vm"
    digest = hashlib.sha256(os.fsencode(vm.directory)).hexdigest()[:10]
    return f"{slug}-{digest}"


def record_path(vm: VmInstance, run_directory: Path) -> Path:
    return run_directory / f"{_record_key(vm)}.json"


def log_path(vm: VmInstance, run_directory: Path) -> Path:
    return run_directory / f"{_record_key(vm)}.log"


def _load_record(path: Path) -> ProcessRecord | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ControlError(f"cannot read process record {path}: {error}") from error
    if not isinstance(data, dict):
        raise ControlError(f"invalid process record {path}: expected an object")
    return ProcessRecord.from_data(data)


def _write_record(path: Path, record: ProcessRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(record), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _process_command(pid: int) -> str | None:
    result = subprocess.run(
        ("ps", "-ww", "-p", str(pid), "-o", "command="),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    command_line = result.stdout.strip()
    return command_line if result.returncode == 0 and command_line else None


def observe_process(record: ProcessRecord) -> ProcessObservation:
    command_line = _process_command(record.pid)
    if command_line is None:
        return ProcessObservation(False, False, None)
    expected_path = f"--vmpath {record.vm_directory}"
    verified = expected_path in command_line and record.binary in command_line
    if record.rom_path is not None:
        verified = verified and f"--rompath {record.rom_path}" in command_line
    return ProcessObservation(True, verified, command_line)


def _choose_binary(options: argparse.Namespace) -> Path:
    if options.binary is not None:
        binary = options.binary.expanduser().resolve()
    elif os.environ.get("86BOX_BIN"):
        binary = Path(os.environ["86BOX_BIN"]).expanduser().resolve()
    elif options.gdb_stub:
        binary = DEFAULT_DEBUG_BINARY
    else:
        binary = DEFAULT_RELEASE_BINARY
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ControlError(f"86Box executable is not runnable: {binary}")
    return binary


def build_command(
    vm: VmInstance,
    binary: Path,
    *,
    settings: bool = False,
    rom_path: Path | None = None,
) -> tuple[str, ...]:
    command = [
        str(binary),
        "--vmpath",
        str(vm.directory),
        "--vmname",
        vm.name,
    ]
    if rom_path is not None:
        command.extend(("--rompath", str(rom_path)))
    if settings:
        command.append("--settings")
    return tuple(command)


def _port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_port(record: ProcessRecord, timeout: float) -> None:
    assert record.gdbstub_port is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observation = observe_process(record)
        if not observation.running:
            raise ControlError(
                f"{record.vm_name} exited before GDB stub port "
                f"{record.gdbstub_port} became ready; log: {record.log_path}"
            )
        if not observation.identity_verified:
            raise ControlError(
                f"PID {record.pid} no longer belongs to {record.vm_name}; refusing to continue"
            )
        if _port_is_listening(record.gdbstub_port):
            return
        time.sleep(0.2)
    raise ControlError(
        f"GDB stub port {record.gdbstub_port} did not listen within {timeout:g}s; "
        f"process remains tracked; log: {record.log_path}"
    )


def _find_pid_by_command(pattern: str, timeout: float) -> int | None:
    """Poll for a process whose command line matches the regex pattern."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ("pgrep", "-f", pattern),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pids = [int(x) for x in result.stdout.split() if x.isdigit()]
        if pids:
            return pids[0]
        time.sleep(0.2)
    return None


def launch(
    vm: VmInstance,
    options: argparse.Namespace,
    *,
    rom_path: Path | None = None,
) -> ProcessRecord:
    run_directory = options.run_dir.expanduser().resolve()
    state_path = record_path(vm, run_directory)
    previous = _load_record(state_path)
    if previous is not None:
        observation = observe_process(previous)
        if observation.running and observation.identity_verified:
            raise ControlError(f"{vm.name} is already running as PID {previous.pid}")
        if observation.running:
            raise ControlError(
                f"recorded PID {previous.pid} belongs to another process; "
                f"refusing to overwrite {state_path}"
            )

    binary = _choose_binary(options)
    if options.gdb_stub and vm.gdbstub_port is None:
        raise ControlError(
            f"{vm.config_path} has no [General] gdbstub_port for --gdb-stub"
        )
    command = build_command(
        vm,
        binary,
        settings=options.settings,
        rom_path=rom_path,
    )
    output_path = log_path(vm, run_directory)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    background = options.background and (sys.platform == "darwin")
    launch_command = command
    if background:
        # `open -g -n` hands a NEW bundle instance to LaunchServices without
        # activating it (`-n` is required so a second 86Box instance can run
        # alongside an already-running one); the 86Box process is reparented,
        # so its PID is re-discovered below.
        bundle = binary.parent.parent.parent  # .../86Box.app from .../Contents/MacOS/86Box
        launch_command = ("open", "-g", "-n", str(bundle), "--args") + tuple(command[1:])

    with output_path.open("ab", buffering=0) as output:
        banner = (
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"launch: {shlex.join(launch_command)} ===\n"
        ).encode()
        output.write(banner)
        process = subprocess.Popen(
            launch_command,
            cwd=vm.directory,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    if background:
        process.wait()
        pattern = (
            re.escape(str(binary)) + r".*--vmpath " + re.escape(str(vm.directory))
        )
        pid = _find_pid_by_command(pattern, options.startup_grace)
        if pid is None:
            raise ControlError(
                f"{vm.name} did not appear after background launch; log: {output_path}"
            )
    else:
        deadline = time.monotonic() + options.startup_grace
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                raise ControlError(
                    f"{vm.name} exited during startup with status {exit_code}; log: {output_path}"
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        pid = process.pid

    record = ProcessRecord(
        pid=pid,
        vm_name=vm.name,
        vm_directory=str(vm.directory),
        config_path=str(vm.config_path),
        machine=vm.machine,
        binary=str(binary),
        command=command,
        log_path=str(output_path),
        gdbstub_port=vm.gdbstub_port,
        rom_path=None if rom_path is None else str(rom_path),
        started_at=time.time(),
    )
    _write_record(state_path, record)

    observation = observe_process(record)
    if not observation.running:
        raise ControlError(f"{vm.name} exited during startup; log: {output_path}")
    if not observation.identity_verified:
        raise ControlError(
            f"cannot verify that PID {record.pid} belongs to {vm.name}; refusing control"
        )
    if options.gdb_stub:
        _wait_for_port(record, options.timeout if options.timeout is not None else 60.0)
    return record


def _require_record(vm: VmInstance, run_directory: Path) -> tuple[Path, ProcessRecord]:
    state_path = record_path(vm, run_directory)
    record = _load_record(state_path)
    if record is None:
        raise ControlError(f"{vm.name} has no process record: {state_path}")
    if Path(record.vm_directory) != vm.directory:
        raise ControlError(f"process record does not belong to {vm.directory}: {state_path}")
    return state_path, record


def stop_process(record: ProcessRecord, timeout: float, *, kill_after: bool) -> None:
    observation = observe_process(record)
    if not observation.running:
        return
    if not observation.identity_verified:
        raise ControlError(
            f"PID {record.pid} does not match {record.vm_directory}; refusing to signal it"
        )

    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not observe_process(record).running:
            return
        time.sleep(0.1)
    if not kill_after:
        raise ControlError(
            f"PID {record.pid} did not exit within {timeout:g}s; it was not force-killed"
        )

    observation = observe_process(record)
    if observation.running and not observation.identity_verified:
        raise ControlError(
            f"PID {record.pid} changed identity after SIGTERM; refusing SIGKILL"
        )
    if observation.running:
        os.kill(record.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not observe_process(record).running:
                return
            time.sleep(0.1)
        raise ControlError(f"PID {record.pid} remains present after SIGKILL")


def wait_for_exit(record: ProcessRecord, timeout: float | None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        observation = observe_process(record)
        if not observation.running:
            return
        if not observation.identity_verified:
            raise ControlError(
                f"PID {record.pid} changed identity; refusing to treat it as {record.vm_name}"
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise ControlError(f"{record.vm_name} is still running as PID {record.pid}")
        time.sleep(0.25)


def _status_data(vm: VmInstance, record: ProcessRecord | None) -> dict[str, Any]:
    if record is None:
        return {
            "vm": vm.name,
            "vm_directory": str(vm.directory),
            "machine": vm.machine,
            "tracked": False,
            "running": False,
            "gdbstub_port": vm.gdbstub_port,
        }
    observation = observe_process(record)
    return {
        "vm": vm.name,
        "vm_directory": str(vm.directory),
        "machine": vm.machine,
        "tracked": True,
        "running": observation.running,
        "identity_verified": observation.identity_verified,
        "pid": record.pid,
        "binary": record.binary,
        "log": record.log_path,
        "rom_path": record.rom_path,
        "gdbstub_port": record.gdbstub_port,
        "gdbstub_listening": (
            _port_is_listening(record.gdbstub_port)
            if observation.running and record.gdbstub_port is not None
            else False
        ),
        "started_at": record.started_at,
    }


def _emit_status(data: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    state = "running" if data["running"] else "not running"
    if data.get("tracked"):
        state += f", PID {data['pid']}"
        if data.get("identity_verified"):
            state += ", identity verified"
        elif data["running"]:
            state += ", IDENTITY MISMATCH"
    print(f"{data['vm']}: {state}")
    print(f"  VM: {data['vm_directory']}")
    print(f"  machine: {data.get('machine') or 'unknown'}")
    if data.get("binary"):
        print(f"  binary: {data['binary']}")
        print(f"  log: {data['log']}")
        if data.get("rom_path"):
            print(f"  ROM path: {data['rom_path']}")
    if data.get("gdbstub_port") is not None:
        listening = "listening" if data.get("gdbstub_listening") else "not listening"
        print(f"  GDB stub: 127.0.0.1:{data['gdbstub_port']} ({listening})")


def _print_logs(path: Path, lines: int) -> None:
    if lines < 1:
        raise ControlError("--lines must be positive")
    if not path.is_file():
        raise ControlError(f"log does not exist: {path}")
    with path.open(encoding="utf-8", errors="replace") as log_file:
        for line in deque(log_file, maxlen=lines):
            print(line, end="")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    options = parser.parse_args(arguments)
    try:
        vm = resolve_vm(options.instance, options.vm_root)
        run_directory = options.run_dir.expanduser().resolve()
        state_path = record_path(vm, run_directory)

        if options.action == "command":
            binary = _choose_binary(options)
            rom_path = resolve_launch_rom_path(vm, options, prepare=False)
            print(
                shlex.join(
                    build_command(
                        vm,
                        binary,
                        settings=options.settings,
                        rom_path=rom_path,
                    )
                )
            )
            return 0

        if options.action == "prepare-roms":
            if options.model80_type2_rom is None:
                raise ControlError(
                    "prepare-roms requires --model-80-type-2-rom"
                )
            prepared = prepare_model80_type2_rom_set(
                vm,
                options.model80_type2_rom,
                source_root=options.rom_source,
                refresh=options.refresh_rom_set,
            )
            state = "reused" if prepared.reused else "created"
            print(f"{vm.name}: {state} patched ROM set {prepared.root}")
            print(f"  source: {prepared.source_root}")
            print(f"  image SHA-256: {prepared.image_sha256}")
            print(f"  manifest: {prepared.manifest_path}")
            return 0

        if options.action == "start":
            rom_path = resolve_launch_rom_path(vm, options, prepare=True)
            record = launch(vm, options, rom_path=rom_path)
            _emit_status(_status_data(vm, record), as_json=options.json)
            return 0

        if options.action == "restart":
            rom_path = resolve_launch_rom_path(vm, options, prepare=True)
            previous = _load_record(state_path)
            if previous is not None and observe_process(previous).running:
                stop_process(
                    previous,
                    options.timeout if options.timeout is not None else 15.0,
                    kill_after=options.kill_after_timeout,
                )
            record = launch(vm, options, rom_path=rom_path)
            _emit_status(_status_data(vm, record), as_json=options.json)
            return 0

        if options.action == "status":
            record = _load_record(state_path)
            data = _status_data(vm, record)
            _emit_status(data, as_json=options.json)
            return 0 if data["running"] and data.get("identity_verified", False) else 1

        _, record = _require_record(vm, run_directory)
        if options.action == "stop":
            stop_process(
                record,
                options.timeout if options.timeout is not None else 15.0,
                kill_after=options.kill_after_timeout,
            )
            print(f"{vm.name}: stopped tracked PID {record.pid}")
        elif options.action == "wait":
            wait_for_exit(record, options.timeout)
            print(f"{vm.name}: tracked PID {record.pid} exited")
        elif options.action == "pid":
            print(record.pid)
        elif options.action == "port":
            if record.gdbstub_port is None:
                raise ControlError(f"{vm.config_path} has no gdbstub_port")
            print(record.gdbstub_port)
        elif options.action == "logs":
            _print_logs(Path(record.log_path), options.lines)
        elif options.action == "forget":
            observation = observe_process(record)
            if observation.running and observation.identity_verified:
                raise ControlError(
                    f"{vm.name} is still running as PID {record.pid}; stop it before forget"
                )
            state_path.unlink()
            print(f"{vm.name}: removed {state_path}")
        else:
            raise AssertionError(options.action)
        return 0
    except (ControlError, OSError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
