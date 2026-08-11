---
name: 86box-vm-operations
description: Safely launch, track, observe, debug, and analyze one or more 86Box VMs without touching another operator's processes.
---

# 86Box VM operations

Use this skill whenever a task requires starting, configuring, waiting for,
restarting, debugging, or inspecting an 86Box virtual machine.

## Non-negotiable safety rules

1. Resolve the exact VM directory before launch.
2. Launch with `--vmpath <VM directory> --vmname <display name>`.
3. Track the PID returned for that launch and the stable harness process name.
4. Verify PID identity using the executable and exact `--vmpath` before any
   signal.
5. Never use `killall`, broad `pkill`, broad `pgrep`, or an unverified PID.
6. Assume other people are running independent 86Box processes.
7. Prefer clean UI shutdown when NVRAM or configuration changes matter.
8. Never overwrite shared ROMs for a one-VM experiment; clone a complete ROM set and select it with `--rompath`.

## Preflight

1. Resolve a name under the platform VM root. On macOS:

   ```text
   ~/Library/Application Support/86Box/Virtual Machines/<NAME>
   ```

   A direct VM path is also valid.

2. Read `<VM>/86box.cfg`. Record:
   - `machine`;
   - `cpu_use_dynarec`;
   - optional `gdbstub_port`;
   - configured media and adapter devices.

3. Choose the binary:
   - normal interaction: `/Applications/86Box.app/Contents/MacOS/86Box`;
   - guest debugging: the local `-DGDBSTUB=ON` build.

4. Audit the command (run from the 86Box-tool project root, or install it):

   ```sh
   cd ~/src/86Box-tool && uv run ./86boxctl.sh <VM> command
   ```

5. If debugging multiple VMs, require a distinct `gdbstub_port` in every
   config and `cpu_use_dynarec = 0`.

## Launch

Standalone controller:

```sh
./86boxctl.sh <VM> start
./86boxctl.sh <VM> start --gdb-stub
```

Patched IBM PS/2 Model 80 Type 2 BIOS:

```sh
./86boxctl.sh <VM> command \
  --model-80-type-2-rom /path/to/sequential-128k.bin
./86boxctl.sh <VM> start \
  --model-80-type-2-rom /path/to/sequential-128k.bin
```

The VM must use `machine = ibmps2_m80`. The controller discovers the platform
stock ROM root, clones the complete tree under `<VM>/romsets/`, splits the
image into the active even/odd lane files, validates them, and launches with
that generated root through `--rompath`. Use `--rom-source` when discovery is
ambiguous. Use `prepare-roms` to build without launching.

For a long harness-mediated GUI session, start the executable through the
harness process manager with:

```text
name        = unique stable name such as 86box-automatic-configuration
application = selected 86Box binary
args        = [--vmpath, <absolute VM dir>, --vmname, <display name>]
pty         = false
persist     = true when interaction spans turns
```

Record the returned PID. Use only that stable process name for subsequent
`describe`, `logs`, `restart`, and `stop` operations. A restart produces a new
PID; record it.

Do not pass `86box.cfg` as a lone positional argument. That selects a config
but does not set 86Box's VM user-files path, so relative media, NVRAM, and local
ROMs can resolve against the wrong directory.

## Establish readiness

Process creation alone is not guest readiness.

- Normal build: require the exact process to survive startup, then inspect the
  visible VM or a concrete VM artifact.
- GDB build: require that this VM's configured RSP port listens.
- Never interpret another 86Box process or port as evidence for this VM.

Use:

```sh
./86boxctl.sh <VM> status
./86boxctl.sh <VM> logs --lines 100
```

## Wait for a user

When the user says to wait until a harness interruption, do exactly that. Do
not advance when the VM exits unless the user requested exit-triggered work.
The harness job waiter does not treat a managed GUI process as a background
job, so create a long finite asynchronous wait sentinel, wait on that job with
an indefinite harness timeout, and cancel it after the user's interruption.

Keep the VM process separately tracked by its stable process name and PID.

## Observe

### Host lifecycle

```sh
./86boxctl.sh <VM> status
./86boxctl.sh <VM> wait
./86boxctl.sh <VM> logs --lines 100
```

### Guest screen, console, and memory

Two independent paths:

1. **RSP stub** (requires the GDB-stub build; connecting pauses the guest):

   ```sh
   RSP_PORT=<port> ./rsp.py probe
   RSP_PORT=<port> ./rsp.py video-mode
   RSP_PORT=<port> ./rsp.py screen
   RSP_PORT=<port> ./rsp.py wait-screen "expected text" --wait-timeout 600
   RSP_PORT=<port> ./rsp.py dump b8000 fa0
   ```

   In text modes `0`, `1`, `2`, `3`, and `7`, `screen` selects 40x25 or
   80x25, uses `B8000h` for colour or `B0000h` for mono, and follows the
   active video page. In graphics mode `11h`, it reads the packed 640x480
   1bpp framebuffer at `A0000h`, resumes the guest, and asks the harness's
   configured `@vision` model to interpret the generated PNG. `omp` must be
   on `PATH`; use `--vision-question` to customize the prompt. Snapshot
   commands pause only for inspection and detach before model inference.

2. **Memdump server** (no GDB stub, no CPU pause): set `memdump_port` in the
   VM config. Poll the text framebuffer with a live delta view:

   ```sh
   ./screenmon.py --memdump-port 12348            # interactive TTY: ANSI delta
   ./screenmon.py --memdump-port 12348 --plain   # piped: full frame + changed rows
   ./screenmon.py --memdump-port 12348 --count 30 --interval 0.1
   ```

   `screenmon.py` renders only what changed between polls; the mode is chosen
   by `isatty()` unless `--full`/`--plain` override it. The default interval
   is 0.1 s.

For continuous bidirectional operation:

```sh
# Interactive ANSI mirror; Ctrl-] detaches
RSP_PORT=<port> ./guest_console.py

# Processable screen and delta events
RSP_PORT=<port> ./guest_console.py \
  --format jsonl --no-input --duration 10

# Type a DOS command through the BIOS ring
RSP_PORT=<port> ./guest_console.py \
  --send "DIR" --enter --duration 10

# Early POST requires hardware scan-code injection
RSP_PORT=<port> ./guest_console.py \
  --input-mode hardware --key F1 --duration 5
```

The console samples at 70 Hz, retains the complete character/attribute buffer,
and emits only coalesced changed runs. A TTY gets ANSI cursor updates;
redirected output defaults to JSONL. Input defaults to the validated BIOS
keyboard ring after boot. Hardware mode injects paced set-1 scan bytes through
the emulated 8042 for POST. Keep `Ctrl-]` for detach so guest `Ctrl-C` remains
available.

Never infer success merely from an injected key. Observe the resulting screen
delta or explicit guest output.

### Persistent VM configuration

After a clean shutdown, inspect only the resolved VM directory:

```text
<VM>/86box.cfg
<VM>/nvr/<machine>.nvr
<VM>/nvr/<machine>_sec.nvr
```

For this repository:

```sh
uv run --project . refdisk-view-config --vm <NAME> --adf-source rf7080a/disk
```

Adding a card in the 86Box UI does not by itself populate guest MCA NVRAM. The
guest Reference Disk must configure POS state and 86Box must save it cleanly.

## ROM verification

`machine = ...` determines required ROM filenames. Check:

1. the exact `--rompath` recorded by `86boxctl status`, when present;
2. the generated set's `.86boxctl-romset.json`;
3. the exact active filenames from the 86Box machine definition;
4. hashes or byte comparisons of those active files.

For this workflow, an explicit `--rompath` must contain the complete 86Box ROM
set. Although 86Box can fall back to other search roots, completeness makes the
run self-contained and prevents silent mixing. Do not point it at a directory
containing only one machine.

For a raw sequential 128 KiB Model 80 Type 2 image, use
`--model-80-type-2-rom`. The controller writes:

```text
machines/ibmps2_m80/15f6637.bin = input[0::2]  # low/even lane
machines/ibmps2_m80/15f6639.bin = input[1::2]  # high/odd lane
```

Both files are 65,536 bytes; interleaving them must reproduce the input.
`start` and `restart` create or validate the isolated clone automatically:

```sh
./86boxctl.sh <VM> prepare-roms \
  --model-80-type-2-rom /path/to/sequential-128k.bin
./86boxctl.sh <VM> status
```

Defaults are the standard 86Box ROM roots: macOS Application Support,
`XDG_DATA_HOME`/`XDG_DATA_DIRS` on Unix, and Local/Program/AppData on Windows.
Override with `--rom-source PATH` or `86BOX_ROM_ROOT`. `--refresh-rom-set`
rebuilds only a recognized generated directory.

Backup suffixes such as `.orig` and `.altered` are not selected automatically.
Preserve shared originals.

For an end-to-end proof using a real GDB-stub-enabled 86Box process:

```sh
./model80_rom_smoke.py <MODEL80_VM>
```

Require `machine = ibmps2_m80`, `cpu_use_dynarec = 0`, and a unique
`gdbstub_port`. The harness derives a sequential image from the stock lanes,
changes `KB OK` to `OKBMR`, repairs the 64 KiB checksum, launches through the
isolated complete ROM clone, observes `OKBMR` in guest text VRAM, and stops only
its recorded PID. Use `--keep-running` only when manual inspection is required.

## Stop or recover

Prefer a clean UI shutdown, then confirm the tracked process exited. If control
is required:

```sh
./86boxctl.sh <VM> stop
./86boxctl.sh <VM> restart
```

`stop` sends SIGTERM only to the identity-verified PID and does not silently
force-kill it. Use `--kill-after-timeout` only after accepting possible NVRAM
or configuration loss.

On a crash:

1. capture `status` and `logs`;
2. confirm the record belongs to the intended VM;
3. restart that record only;
4. record the new PID;
5. return to the prior wait or observation step.

## Completion evidence

Before reporting success, provide the evidence appropriate to the task:

- exact VM path and machine identifier;
- binary and exact launch command;
- stable process name and current PID;
- readiness evidence;
- clean exit or crash status;
- relevant config/NVRAM paths and sizes;
- guest screen, register, memory, or decoded configuration output.

See `README.md` for implementation details, GDB-stub quirks, ROM lookup, and
failure diagnosis.
