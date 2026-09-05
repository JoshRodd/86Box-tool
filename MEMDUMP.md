# MEMDUMP — the 86Box memory-dump server

*Working notes for the pull request. Not checked into git.*

## Summary

`memdump` is a tiny TCP server inside the emulator that answers
"dump guest physical memory" and "assert value onto the I/O bus"
requests. It exists because the GDB stub, the only previous external
read path, **suspends the CPU** on every connect — useless for watching
a live screen, catastrophic for anything timing-sensitive. The memdump
server never touches CPU state: reads go straight to the emulated
memory arrays from a dedicated thread.

Primary consumer today: polling the guest text framebuffer
(`86Box-tool`'s `screenmon.py` / `guest_console.py`), i.e. observing the
guest without a debugger and without pausing it.

## Enabling

Per-VM setting, `[General]` section of `86box.cfg`:

```ini
[General]
memdump_port = 12348
```

`0` (the default) disables the server entirely — no socket is created.
The setting is read at start-up and written back only when non-zero.
When active, the log line `MemDump: Listening on port 12348` appears at
machine start.

## Wire protocol

Line-based commands, DEBUG-style. The server emits a `- ` prompt when
the connection opens and after every reply; a connection accepts any
number of commands until `q` or EOF. Replies keep request/reply
symmetry so scripting is easy.

```
d <address> [<length>]     dump memory, DEBUG-style lines
dr <address> [<length>]    dump raw                 -> "<length*2 lowercase hex>\n"
o <port> <value>           I/O output, sensed width  -> "ok\n"
ow <port> <value>          I/O output, forced word   -> "ok\n"
od <port> <value>          I/O output, forced dword  -> "ok\n"
q                          close the connection      (no reply)
?                          help, the command table
```

Anything unparseable is answered `" ^ Error\n"` — DEBUG's error, with
the prompt column in mind.

The `?` reply:

```
dump                   D address [length]
dump raw               DR address [length]
output                 O[W|D] port value
quit                   Q
help                   ?
```

**Numbers** are hexadecimal, C/assembler style: an optional leading
`0x`/`0X` and an optional trailing `h`/`H` are ignored — on plain
numbers and on both halves of a `segment:offset` pair.

**Addresses** accept every spelling a debugged-era programmer uses:

```
F000:FFFF      8086 segment:offset  -> (segment << 4) + offset
0xF000:0xFFFF  ditto, with 0x prefixes
FFFFF          absolute 20-bit
FFFFFF         absolute 24-bit
FFFFFFFF       absolute 32-bit
FFFF6h         absolute with assembler suffix
```

**Dump length** is the optional second parameter, defaulting to 128
bytes (like DEBUG) and clamped to 0x1000 per request.

**DEBUG-style dump format** (`d`), exactly like the DOS DEBUG look:

```
F000:FFF0  EA 5B E0 00 F0 31 30 2F-31 39 2F 38 31 FF FF 9B   ..[...10/19/81..
F000:0000  FF FF FF FF FF FF FF FF-FF FF FF FF FF FF FF FF   ................
```

Segmented requests label each line `SEG:OFF` — segment fixed, offset
incrementing and wrapping mod 64 K, just like DEBUG; absolute requests
label lines with the 8-digit linear address.  Reads always advance
linearly from the base either way (the wrap is display-only).  Hex
bytes are uppercase with a dash between bytes 8 and 9; the ASCII
column shows `.` for non-printables; partial last lines are padded so
the columns stay aligned.  The reply is one `\n`-terminated line per
16 bytes, followed by an empty line as the end-of-reply marker.

**Raw dump** (`dr`) is the machine-readable path — exactly the old
hex-only reply — and what `86Box-tool`'s `MemDump` client uses.

**I/O output width** follows the written digits, not just the value:
8 digits (or anything wider than 16 bits) is a dword; 3 or more digits
(or anything wider than 8 bits) is a word; otherwise a byte.  So `EF`
is a byte, while `1EF`, `00EF` and `00FF` are words and `000000FF` is
a dword.  `OW` and `OD` override the sensing and force a word or dword
access respectively (there is deliberately no `OB` — plain `O` with a
byte-sized spelling is the byte path, just like DEBUG).  The value is
asserted onto the emulated bus through the same `outb()`/`outw()`/
`outl()` dispatch the CPU uses, from the server thread — i.e.
immediately but asynchronously with respect to the guest, exactly like
poking a port from a debug monitor.  Ports above 16 bits are masked.
Practical use: making something happen with an I/O device from outside
the guest, e.g. `o 7F 02` requests a power off through the (now
upstream) PC Convertible soft power card.

Details, straight from `src/memdump.c`:

* Command lines are read up to 127 bytes, `\r` is tolerated, one
  reply per line.  Every reply ends with an empty line.
* Requests are served serially by a single thread (listen backlog 1).
* Binds `0.0.0.0:<port>` (IPv4) with `SO_REUSEADDR`. Caveat for the
  PR: binding is not restricted to loopback; on a multi-user host pick
  a port accordingly (or propose a 127.0.0.1 default as a follow-up).

## Read semantics

1. **Fast path** — `readlookup2[addr >> 12]` yields a direct pointer for
   RAM/ROM-backed pages; the byte is read from the emulated array.
2. **Mapping path** — otherwise the page's registered memory mapping
   handler `read_b` is invoked (device-mapped pages: video windows,
   memory-mapped I/O).
3. **Unmapped** — returns `0xFF`.

There is **no lock against the emulation thread** — a dump can observe
torn or mid-update state. That is by design and is what makes it safe:
the server never stops the CPU, never takes emulator locks, and cannot
deadlock the guest. Consumers (screen pollers) treat transient
inconsistency as noise.

## Lifecycle / implementation notes

* `memdump_init()` is called from `machine_init()` immediately after
  `gdbstub_init()`, guarded by a once-per-process static so the port is
  only occupied when a machine is actually started (not for
  `--settings`-only runs).
* `memdump_close()` runs from `pc_close()` at shutdown
  (`shutdown()` + `close()`, which also wakes the blocked `accept()`).
* Win32 path uses Winsock2 (`WSAStartup`/`WSACleanup`).
* The gdbstub port is deliberately untouched; memdump is the
  CPU-suspension-free alternative, not a replacement.

## Files changed (the PR diff)

```
src/memdump.c                  (new, ~415 lines: server thread, command parser, DEBUG formatter, read path)
src/include/86box/memdump.h    (new, 7 lines: memdump_init/close)
src/CMakeLists.txt             (+2: target_sources memdump.c)
src/machine/machine.c          (+2: include, init call)
src/86box.c                    (+3: memdump_port global, close call)
src/config.c                   (+6: load/save of memdump_port)
src/include/86box/86box.h      (+1: extern memdump_port)
```

## Trying it

With `memdump_port = 12348` and a running VM:

```
$ nc 127.0.0.1 12348
- d ffff0 20
000FFFF0  EA 5B E0 00 F0 31 30 2F-31 39 2F 38 31 FF FF 9B   ..[...10/19/81..
00100000  FF FF FF FF FF FF FF FF-FF FF FF FF FF FF FF FF   ................

- dr ffff0 8
ea5be000f031302f
- xyz
 ^ Error
- o 7F 02
ok

- q
```

Tooling in this repository:

* `memdump/example.py` — minimal protocol example (`--raw` for `dr`,
  `--out`/`--outW`/`--outD` for I/O).
* `memdump/tests/test_protocol.py` — protocol conformance suite
  (requires a running VM; an IBM PC 5150 with the 19OCT81 BIOS is
  expected by the content-specific checks).  Run:
  `python3 memdump/tests/test_protocol.py 127.0.0.1 12349`.
* `screenmon.py` / `guest_console.py` / `diagnostics_driver.py` — all
  read guest memory through `rsp.MemDump`, which speaks the `dr`
  command.
