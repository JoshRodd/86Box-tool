# Installing 86Box-tool

86Box-tool is a collection of scripts for launching, observing, driving, and
diagnosing [86Box](https://86box.net) virtual machines. It is dependency-free
(standard library only) and managed with [uv](https://docs.astral.sh/uv/).

## Prerequisites

- **uv** (0.5+): `brew install uv`, or the [standalone installer](https://docs.astral.sh/uv/getting-started/installation/)
- **Python 3.11+** (uv installs it automatically if missing)
- **86Box** — see below for which build each tool needs

## Install

```sh
git clone <repository-url> ~/src/86Box-tool
cd ~/src/86Box-tool
uv sync
```

`uv sync` creates `.venv` and resolves the (empty) dependency set. All
commands run through `uv run`:

```sh
uv run screenmon.py --memdump-port 12348
uv run ./86boxctl.sh "8530" status
```

## Running the test suite

```sh
uv run python -m unittest test_86box_tool -v
```

## Which 86Box build each tool needs

| Tool | Requirement |
|---|---|
| `86boxctl.sh` / `86boxctl.py` | any 86Box build |
| `rsp.py` | a `-DGDBSTUB=ON` build (RSP stub; connecting pauses the guest) |
| `guest_console.py` | a `-DGDBSTUB=ON` build for the interactive console; `--no-input` mode works with the memdump server |
| `screenmon.py` | the **memdump server** (`memdump_port` in the VM config) — no GDB stub, no CPU pause |
| `diagnostics_driver.py` | the memdump server + a GDB-stub build for key injection |
| `model80_rom_smoke.py` | a `-DGDBSTUB=ON` build |

### Enabling the memdump server

You will need a build of 86Box with the memdump patch applied. Apply
`0001-Add-memdump-server-for-external-guest-memory-observa.patch`
to your current source tree and build.

Add to the VM's `86box.cfg`:

```ini
[General]
memdump_port = 12348
```

The server reads guest memory from a dedicated thread; it never suspends
the CPU. `screenmon.py` polls the text framebuffer through it.

### GDB-stub builds

```sh
cmake -B build -DGDBSTUB=ON -DDEV_BRANCH=ON -DUSE_QT6=ON <86Box source>
cmake --build build -j
```

Each VM that needs RSP access must have a distinct `gdbstub_port` in its
config. Connecting a client pauses the guest; close the connection to
resume.

## Layout

```
86Box-tool/
  .agents/skills/SKILL.md   skill definition for harness agents
  86boxctl.sh                thin wrapper over 86boxctl.py
  86boxctl.py                VM lifecycle controller (launch/stop/status)
  rsp.py                     RSP stub client + memdump client + text renderers
  guest_console.py           interactive ANSI console with delta events
  screenmon.py               live delta view of the text screen (memdump)
  diagnostics_driver.py      state-machine driver for IBM diagnostics disks
  model80_rom_smoke.py       end-to-end Model 80 BIOS patch smoke test
  test_86box_tool.py         unittest suite
```

See `README.md` for usage of each tool.
