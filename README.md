# 86Box-tool — safe launch, lifecycle, and guest observation

A dependency-free toolkit (standard library only, managed with uv) for
running several 86Box VMs concurrently without confusing their processes,
files, ROMs, or debugger ports, and for observing and driving the guests.
It supports both the installed macOS application and custom builds
(GDB stub for RSP access, the memdump server for CPU-pause-free screen
reads).

Installation: see `INSTALL.md` (`uv sync`, then `uv run <script>`).
License: 0-clause MIT, Copyright (C) 2026 Simplebooks Foundation and
Copyright (C) 2026 Josh Rodd — see `LICENSE.md`.

The central invariant is simple: **every operation starts from one resolved VM
directory and one recorded PID**. Nothing here uses `killall`, a broad `pkill`,
or an arbitrary `pgrep` result.

## Quick start

On macOS, named VMs default to:

```text
~/Library/Application Support/86Box/Virtual Machines/<NAME>/
```

Inspect the exact command without launching:

```sh
cd ~/src/86Box-tool
uv run ./86boxctl.sh "Automatic Configuration" command
```

Launch the installed 86Box application and record its PID:

```sh
uv run ./86boxctl.sh "Automatic Configuration" start
uv run ./86boxctl.sh "Automatic Configuration" status
uv run ./86boxctl.sh "Automatic Configuration" pid
uv run ./86boxctl.sh "Automatic Configuration" logs --lines 50
```

Wait for that process to exit after a user shuts the VM down:

```sh
./86boxctl.sh "Automatic Configuration" wait
```

A VM path can replace a name:

```sh
./86boxctl.sh "/path/to/Virtual Machines/Test VM" start
```

Runtime records and captured output live under `86Box-tool/run/`. The record
name includes a hash of the absolute VM path, so equal display names under
different roots do not collide.

## Correct 86Box invocation

Launch a VM with:

```sh
86Box --vmpath "$VM_DIR" --vmname "$DISPLAY_NAME"
```

This is the same contract used by 86Box's Qt VM Manager. In 86Box source,
`--vmpath` becomes `usr_path`, which controls:

- the default `86box.cfg` location;
- primary and secondary NVRAM files;
- relative disk-image paths;
- VM-local `roms/` and `assets/` directories.

Passing `/path/to/86box.cfg` as the lone positional argument selects a config
file but **does not set the VM user-files directory**. NVRAM, relative media,
and local ROM lookup can consequently use the launcher's working directory.
The old version of `86boxctl.sh` made this mistake; the current controller does
not.

The controller launches with the VM directory as its working directory and
passes both `--vmpath` and `--vmname`. Use `command` to audit this before any
launch.

## `86boxctl.py`

`86boxctl.sh` is a compatibility wrapper around the dependency-free Python
controller.

```text
86boxctl INSTANCE ACTION [OPTIONS]
```

Actions:

| Action | Effect |
|---|---|
| `command` | Print the exact quoted launch command without running it. |
| `prepare-roms` | Build or validate an isolated complete ROM set for a patched Model 80 BIOS. |
| `start` | Launch and record the exact PID, command, VM path, binary, and log. |
| `restart` | Signal only the verified tracked PID, then launch a new one. |
| `status` | Report running state, PID identity, binary, VM path, log, and stub port. |
| `wait` | Wait for the tracked PID to disappear; `--timeout` is optional. |
| `stop` | Send SIGTERM only after PID identity verification. |
| `pid` | Print the recorded 86Box PID. |
| `port` | Print `[General] gdbstub_port`. |
| `logs` | Print the last `--lines N` captured lines. |
| `forget` | Remove a stopped/stale record; refuses while its verified PID runs. |

Useful options:

```sh
--vm-root PATH          # alternate root for named VMs
--binary PATH           # explicit 86Box executable
--settings              # launch the VM settings dialog
--gdb-stub              # select custom build and wait for its configured port
--rom-path PATH        # use an existing complete ROM root
--model-80-type-2-rom PATH
                       # clone stock ROMs and install a sequential 128 KiB image
--rom-source PATH      # complete stock ROM root to clone
--refresh-rom-set      # rebuild a controller-generated ROM set
--timeout SECONDS       # start/stop/wait timeout
--json                  # machine-readable start/status output
```

Environment equivalents:

```text
86BOX_VM_ROOT
86BOX_BIN
86BOX_ROM_ROOT
86BOX_RUN_DIR
```

### Process safety

A record is not trusted merely because its PID still exists. Before reporting,
signalling, restarting, or forgetting it, the controller reads that PID's full
command line and verifies both:

1. the recorded executable; and
2. the exact `--vmpath <absolute VM directory>` argument.

If the PID was reused or belongs to another 86Box instance, the controller
refuses to signal it. `stop` sends SIGTERM and waits 15 seconds by default. It
does **not** silently escalate to SIGKILL, because a hard kill can lose NVRAM
or configuration writes. Explicit `--kill-after-timeout` is available when the
caller has accepted that risk.

For important guest configuration, prefer shutting down from the 86Box UI and
then use `wait`/`status` to observe completion.

## Normal build versus GDB-stub build

### Installed application

Default binary:

```text
/Applications/86Box.app/Contents/MacOS/86Box
```

Use this for ordinary interactive configuration. It starts running normally
and does not expose guest debugger state.

### Custom GDB-stub build

The shipped application has hardened-runtime restrictions and is not built
with 86Box's guest GDB stub. A custom build lives at:

```text
~/src/86Box/build/regular/src/86Box.app/Contents/MacOS/86Box
```

Build recipe used here:

```sh
cd ~/src/86Box
cmake --preset regular \
  -DUSE_QT6=ON \
  -DLIBRASHADER=ON \
  -DLIBRASHADER_STATIC=ON \
  -DDISCORD=OFF \
  -DOPENAL=OFF \
  -DGDBSTUB=ON
cmake --build build/regular -j "$(sysctl -n hw.ncpu)"
```

The local `src/gdbstub.c` edit is a working-tree change in `~/src/86Box`
(uncommitted, so it survives only if reapplied after a source refresh). The
exact diff ships here as `gdbstub_port.patch` (26 lines): it makes the stub
read `[General] gdbstub_port` from the VM config instead of hardcoding 12345.
Apply it to a fresh checkout:

```sh
cd ~/src/86Box
git apply 86Box-tool/gdbstub_port.patch
```

It is version-coupled to `src/gdbstub.c` at commit `026ea585f` (hunks in
`gdbstub_init`); if upstream drifts, re-derive by hand — the change is two
`#include` lines plus one `ini_get_int` call. After applying, rebuild
`build/regular` incrementally (one `.c` file). Every concurrently debugged
VM must use a distinct port:

```ini
[General]
gdbstub_port = 12346

[Machine]
cpu_use_dynarec = 0
```

The stub hooks the interpreter, so dynamic recompilation must remain disabled.
Launch with:

```sh
./86boxctl.sh ROMTEST start --gdb-stub
```

`start --gdb-stub` does not report success until that VM's configured port is
listening. A normal launch only verifies that the exact process survived its
startup grace period; a GUI process has no generic readiness port.

## ROM selection and isolation

A VM's `machine = ...` setting selects a machine definition and its required
ROM filenames. For `machine = ibmps2_m80`, 86Box loads:

```text
machines/ibmps2_m80/15f6637.bin   low/even byte lane
machines/ibmps2_m80/15f6639.bin   high/odd byte lane
```

86Box searches an explicit `--rompath` first, then retains its other search
paths as fallbacks. This workflow nevertheless builds a **complete ROM set**,
making the run self-contained so no lookup silently falls through to another
shared tree. `86boxctl` clones the stock tree under the selected VM and passes
that clone explicitly:

```text
<VM>/romsets/ibmps2-m80-type2-<image-hash>-<source-hash>/
```

The shared source tree is read-only. Generated sets contain a
`.86boxctl-romset.json` manifest with the input and output hashes, and are
reused only after the manifest and both active lane files validate.

### Launching a sequential Model 80 Type 2 ROM

Supply one raw, sequential, exactly 131,072-byte image:

```sh
./86boxctl.sh "Model 80 test" command \
  --model-80-type-2-rom /path/to/model80-type2.bin

./86boxctl.sh "Model 80 test" start \
  --model-80-type-2-rom /path/to/model80-type2.bin
```

`start` and `restart` prepare the clone before launch. `command` prints the
future `--rompath` without copying, so use `prepare-roms` when the set should be
built separately:

```sh
./86boxctl.sh "Model 80 test" prepare-roms \
  --model-80-type-2-rom /path/to/model80-type2.bin
```

The split is byte-lane interleaving, not a 64 KiB midpoint split:

```text
15f6637.bin = image bytes 0, 2, 4, ...   (65,536 bytes)
15f6639.bin = image bytes 1, 3, 5, ...   (65,536 bytes)
```

Re-interleaving low byte, high byte reconstructs the input exactly. The
controller verifies the 128 KiB input size and the two generated hashes before
launching. This mapping follows 86Box's
`bios_load_interleaved("15f6637.bin", "15f6639.bin", ...)`: its first file is
copied to even guest addresses and its second to odd addresses.

The stock source root is selected in this order:

| Platform | Default candidates |
|---|---|
| macOS | `~/Library/Application Support/net.86box.86Box/roms`, `~/Library/Application Support/86Box/roms`, then the same two names under `/Library/Application Support` |
| Linux/Unix | `$XDG_DATA_HOME/86Box/roms` or `~/.local/share/86Box/roms`, then each `$XDG_DATA_DIRS/86Box/roms` |
| Windows | `%LOCALAPPDATA%\86Box\roms`, `%PROGRAMDATA%\86Box\roms`, `%APPDATA%\86Box\roms` |

Override discovery with `--rom-source PATH` or `86BOX_ROM_ROOT`. Use
`--refresh-rom-set` to recopy a changed stock tree. It only replaces a
directory carrying a recognized controller manifest; it refuses an unrelated
directory.

For an already complete custom tree, bypass preparation with:

```sh
./86boxctl.sh "Model 80 test" start --rom-path /path/to/complete/roms
```

The resolved ROM path is stored in the process record, shown by `status`, and
included in PID identity verification. Two VMs using different patches receive
different hash-addressed roots and launch commands.

Files ending in `.orig`, `.altered`, or another backup suffix are not selected
by 86Box automatically. Only the exact active filenames above run. Never
overwrite shared ROMs for a one-VM experiment.

### End-to-end real-ROM smoke proof

`model80_rom_smoke.py` proves the complete patch path against a real 86Box
process:

```sh
./model80_rom_smoke.py ROMTEST
```

The selected VM must use `machine = ibmps2_m80`, disable the dynarec, and have
a unique `[General] gdbstub_port`. The harness:

1. interleaves the active stock `15f6637.bin` and `15f6639.bin`;
2. replaces the unique `KB OK` POST string with the same-length `OKBMR`;
3. repairs the containing 64 KiB BIOS checksum;
4. passes the resulting sequential image through the isolated complete-ROM-set
   launcher;
5. starts the real GDB-stub-enabled 86Box binary;
6. waits for `OKBMR` in guest text VRAM through `rsp.py`; and
7. stops only the PID recorded for that launch.

For the stock Model 80 Type 2 ROM currently exercised here, the text is at file
offset `1E05Fh`/physical address `FE05Fh`; its checksum byte is at `1FFFFh`.
The generated image is retained under `<VM>/rom-patches/`, while the stock ROMs
remain unchanged. Add `--keep-running` to leave a successfully verified VM
open for manual inspection.

## Multi-instance operation in the coding harness

For a long interactive run, the harness process manager is preferable to an
unmanaged shell background process. Give every VM a stable, unique process
name and retain that name for every later operation.

Conceptual launch fields:

```text
name:        86box-<vm-slug>
application: /Applications/86Box.app/Contents/MacOS/86Box
args:        --vmpath <absolute VM dir> --vmname <display name>
pty:         false
persist:     true when a user will interact across turns
```

Required workflow:

1. Start through the process manager and record its returned PID.
2. Confirm startup through that named process's logs or a debug-stub port.
3. Address `describe`, `logs`, `restart`, and `stop` by the same stable name.
4. After a crash/restart, record the newly returned PID.
5. Never infer ownership from another visible `86Box` process.
6. Never use `pkill`, `killall`, or an unverified PID; another person may be
   running 86Box concurrently.

When the user asks the harness to wait for an explicit chat interruption, a VM
process is not itself a background *job* watched by the harness's job waiter.
Use a long, finite asynchronous wait sentinel, wait on that job, and cancel the
sentinel when interrupted. Do not proceed merely because the VM window closes
unless the user asked for exit-triggered continuation.

## Observing VM lifecycle and persistent state

Three layers provide different evidence.

### Host process

```sh
./86boxctl.sh VM status
./86boxctl.sh VM logs --lines 100
./86boxctl.sh VM wait --timeout 3600
```

A running PID proves only process liveness. It does not prove the guest booted.
For a GDB build, the listening RSP port is a stronger readiness signal. For an
interactive stock build, inspect the window or a concrete VM artifact.

### VM files

After a clean shutdown, inspect the resolved VM directory—not a global path or
current working directory:

```text
<VM>/86box.cfg
<VM>/nvr/<machine>.nvr
<VM>/nvr/<machine>_sec.nvr
<VM>/screenshots/
```

For extended-NVR PS/2 machines, the secondary file is normally 8192 bytes.
Configuration data may remain blank until the guest Reference Disk writes it
and 86Box shuts down cleanly. Adding a virtual card in the 86Box settings UI is
not equivalent to saving that card's POS assignment in guest NVRAM.

This repository can decode a supported VM directly:

```sh
uv run --project .. refdisk-view-config --vm "Automatic Configuration" \
  --adf-source ../rf7080a/disk
```

### Emulated guest through RSP

The GDB stub exposes guest registers, physical memory, hardware breakpoints,
monitor I/O, text video, and keyboard injection. `rsp.py` provides snapshots
and one-shot input:

```sh
RSP_PORT=12345 ./rsp.py probe
RSP_PORT=12345 ./rsp.py video-mode
RSP_PORT=12345 ./rsp.py screen
RSP_PORT=12345 ./rsp.py wait-screen "PRESS F1" --wait-timeout 600
RSP_PORT=12345 ./rsp.py type "DIR" --enter --bios-buffer
RSP_PORT=12345 ./rsp.py type --key F1
RSP_PORT=12345 ./rsp.py dump b8000 fa0
RSP_PORT=12345 ./rsp.py trace f4300 --count 1
```

`video-mode` reads the current mode byte at BIOS data address `00449h`.
Supported text layouts are:

| BIOS mode | Geometry | Video base |
|---|---|---|
| `0`, `1` | 40x25 colour | `B8000h` |
| `2`, `3` | 80x25 colour | `B8000h` |
| `7` | 80x25 monochrome | `B0000h` |

The active video page offset and cursor also come from the BIOS data area.
`screen` and `wait-screen` therefore follow mode, geometry, page, and the
`B0000h`/`B8000h` distinction automatically. Explicit address and geometry
options remain available for diagnosis.

### Streaming guest console

`guest_console.py` turns the text display into a bidirectional host console:

```sh
# ANSI screen mirror; type normally, Ctrl-] detaches
RSP_PORT=12345 ./guest_console.py

# Machine-readable initial screen plus changed runs
RSP_PORT=12345 ./guest_console.py \
  --format jsonl --no-input --duration 10

# Run a DOS command and stream its response
RSP_PORT=12345 ./guest_console.py \
  --format jsonl --no-input --send "DIR" --enter --duration 10

# Send an early-POST hardware key instead of using the BIOS ring
RSP_PORT=12345 ./guest_console.py \
  --input-mode hardware --key F1 --duration 5
```

The sampler checks the BIOS mode and active video page at **70 Hz**, pauses the
guest only long enough to read the BIOS data and character/attribute cells,
then resumes it until the next deadline. An internal complete screen buffer
compares both bytes of each cell and coalesces adjacent changes into text runs.

On a host TTY, the renderer emits only ANSI cursor moves and changed runs, then
places the cursor at the guest cursor. With redirected stdout, `auto` selects
newline-delimited JSON: the first event contains the complete screen and later
events contain `{row, column, text}` deltas. CP437 control glyphs are translated
to safe Unicode rather than emitted as host terminal controls.

The stream reads host stdin in raw mode. Printable keys, Ctrl combinations,
arrows, navigation keys, and F1-F12 are translated to guest keys. `Ctrl-]` is
reserved for detaching, leaving guest `Ctrl-C` available.

## 86Box GDB-stub behavior

The stub controls the emulated CPU, not the host 86Box process.

Observed constraints:

- The guest starts paused until a client sends `c` or detaches.
- Packets are processed in the interpreter execution loop. Send byte `03h` to
  break before issuing inspection commands.
- The first non-continue request can deadlock unless the client primes the
  stub's response event with an ACK. `rsp.py` sends `+` on connect.
- Hardware breakpoints persist across connections. `rsp.py` removes and retries
  a stale `Z1` entry.
- Software breakpoints cannot modify ROM mappings. Use `Z1` hardware
  breakpoints for firmware.
- Bulk register packets are malformed for current GDB's i386 expectations.
  Indexed fields in `T` packets and selective `p` reads work; `rsp.py` parses
  those leniently.
- Register bytes are hex-encoded little-endian x86 values.
- The stub is single-client per VM.

Supported operations used here include `?`, `c`, `s`, `g`, `G`, `p`, `P`,
`m`, `M`, `Z1`, `z1`, `D`, and `qRcmd`. Monitor commands include port reads
`ib`/`iw`/`il`, port writes `ob`/`ow`/`ol`, and hard reset `r`.

Python API:

```python
from rsp import RSP, read_text_screen, reg16, reg_val, stop_regs

client = RSP(port=12345)
try:
    packet = client.break_cpu()
    registers = stop_regs(packet)
    eip = reg_val(registers[8])
    cs = reg16(registers[10])
    screen = read_text_screen(client)
    client.set_hw_bp(0xF4300)
finally:
    client.detach()
```

## Guest keyboard and command execution

Two input paths cover different machine states:

1. **BIOS ring (`--input-mode bios`, console default):** writes `(ASCII, scan)`
   words to the active circular buffer described by `0041Ah`, `0041Ch`,
   `00480h`, and `00482h`. It validates the head, tail, and bounds, leaves one
   slot empty, and retries pending input after the guest consumes entries.
   This is reliable for DOS and other BIOS-console programs.
2. **8042 hardware (`--input-mode hardware`):** uses the keyboard controller's
   `D2h` output-buffer command through GDB monitor port writes. Set-1 make,
   break, modifier, and extended scan bytes are delivered one per 70 Hz frame.
   Use this for POST prompts that run before the BIOS ring consumer.

One-shot equivalents:

```sh
./rsp.py type "ECHO READY" --enter --bios-buffer
./rsp.py type --key F1
```

`guest_console.py` defaults to the BIOS path because command interaction occurs
after boot. Select hardware input explicitly for early POST. The classic BIOS
ring holds 16 entries, so the implementation sends at most 15 before allowing
the guest to drain it; extended bounds are used when valid.

For automated DOS work, print an unmistakable result from the guest command and
consume `guest_console.py --format jsonl` deltas. This preserves output timing
and avoids treating a host process exit status as the DOS command's status.

## Failure handling

- **Launch exits immediately:** read the tracked log; verify binary, VM path,
  machine ROMs, and relative media.
- **Another 86Box is running:** do nothing to it. Operate only on your record
  and verified PID.
- **PID identity mismatch:** treat the state file as stale or tampered; never
  signal the PID. Use `forget` only after inspecting the mismatch.
- **VM crash:** capture status/log evidence, then `restart` the same record and
  record the new PID.
- **Stub never listens:** confirm unique `gdbstub_port`, the GDB-stub binary,
  and `cpu_use_dynarec = 0`.
- **NVRAM remains blank:** run the guest configuration utility and shut 86Box
  down cleanly before reading `_sec.nvr`.
- **Need an immediate hard stop:** use `--kill-after-timeout` only for the exact
  identity-verified record and accept possible state loss.

## File index

| Path | Purpose |
|---|---|
| `SKILL.md` | Concise agent procedure and safety rules. |
| `86boxctl.py` | VM resolution, correct launch command, process records, identity checks, lifecycle, logs. |
| `86boxctl.sh` | Compatibility wrapper for `86boxctl.py`. |
| `rsp.py` | Guest RSP client, memory/register access, tracing, screen and wait primitives. |
| `guest_console.py` | 70 Hz mode-aware ANSI/JSONL text console with BIOS and hardware keyboard input. |
| `model80_rom_smoke.py` | Real-86Box Model 80 POST text/checksum patch and RSP proof harness. |
| `test_86box_tool.py` | Multi-instance isolation, PID-safety, and screen-rendering tests. |
| `gdbstub_port.patch` | Working-tree patch for `~/src/86Box/src/gdbstub.c`: makes the stub port a per-VM `[General] gdbstub_port` config key (default 12345). |
| `run/` | Generated process records and logs; never source data. |

## Tools

| Script | Purpose | Guest access |
|---|---|---|
| `86boxctl.sh` / `86boxctl.py` | VM lifecycle: launch, stop, status, logs, ROM-set prep | none |
| `rsp.py` | RSP stub client: probe, memory dumps, video-mode-aware screen reads | GDB stub (pauses guest while connected) |
| `guest_console.py` | interactive ANSI console; JSONL delta events; key injection | GDB stub or memdump |
| `screenmon.py` | live delta view of the text framebuffer | memdump server only (never pauses) |
| `diagnostics_driver.py` | state-machine driver for IBM diagnostics disks | memdump + GDB stub for keys |
| `model80_rom_smoke.py` | end-to-end Model 80 BIOS patch smoke test | GDB stub |

### Live screen deltas (screenmon.py)

```sh
uv run screenmon.py --memdump-port 12348            # interactive TTY: ANSI in-place
uv run screenmon.py --memdump-port 12348 --plain   # piped: full frame, then changed rows
uv run screenmon.py --memdump-port 12348 --count 60 --interval 0.2
```

The VM config must set `memdump_port`. The memdump server reads guest memory
from its own thread, so polling never pauses the emulation (tearing is
accepted). The rendering mode follows `stdout.isatty()` unless `--full` or
`--plain` forces it.
