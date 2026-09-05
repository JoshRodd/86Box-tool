# LPT 8-bit Strobe Mode

## Problem

Some guest operating systems and applications (particularly IBM OS/2 and DOS
print spoolers that capture print jobs for later processing) expect very precise
LPT port timing.  In the default SPP mode (lpt_mode=1), the emulated
8255-style port forwards data to the pipe the instant the guest writes port
0x378 — before STROBE has fired.  A fast driver that writes the data register
and then immediately asserts STROBE can cause the receiving end to see the byte
disappear before it has been acknowledged, clipping the first character of a
print job.  Detecting the end of a job is equally difficult: without a
handshake, the pipe reader has no way to know when the guest has finished
sending.

Mode 3 ("Raw SPP with handshaking") solves both problems by gating every data
byte on the STROBE signal, giving the pipe reader explicit per-byte handshaking
and a reliable way to detect job boundaries.

## How it works

### LPT pipe modes

86Box's UNIX Named Pipe LPT device offers four modes, selectable in the VM
settings under the "Parallel cable" option:

| Value | Name | Behaviour |
|-------|------|-----------|
| 0 | Unidirectional (8-bit) / LapLink (4-bit) | NIBBLE flag set; 4-bit receive via status lines |
| 1 | SPP (8-bit) | No special flags; data written to pipe immediately |
| 2 | DirectParallel FAST | PTI flag set; PTI command/bidirectional protocol |
| 3 | Raw SPP with handshaking | NIBBLE + USESTROBE flags; data latched, sent on STROBE edge |

### What USESTROBE does

When `CHAR_LPT_USESTROBE` is set, the LPT device handles writes from the guest
in two stages:

1. **Data latch** (`lpt_char_write_data`): The byte is stored in
   `dev->char_write` but not forwarded to the pipe.  The guest has driven the
   data bus, but nothing is sent yet.

2. **Strobe trigger** (`lpt_char_strobe`): When the guest writes to the control
   register and bit 0 (STROBE) transitions from 1→0 (trailing edge), the latched
   byte is forwarded to the pipe in a single `write()` call.

This means every byte that reaches the pipe reader has been explicitly strobed.
The reader can rely on the data stream being byte-for-byte what the guest
intended to print, with no clipping.

In mode 1, neither `lpt_char_strobe` nor the latch in `lpt_char_write_data` are
active — the byte is written to the pipe immediately on the port 0x378 write,
with no STROBE gating.

### Direction-switch fix

The emulated parallel port supports switching the data bus direction via bit 5
of the control register (port 0x37A).  A subtle timing issue arises when the
guest switches from input back to output: it writes a data byte (which gets
latched), then clears the direction bit in the same control-register write.
Without the fix, the latched byte would be lost because the control write
happens before the data is driven.

The fix in `lpt_write` (control-register handler, offset 0x0002) checks for this
transition:

```c
/* A bidirectional port still latches DTR writes while its pins are
   inputs. Drive that latched byte before an output-mode strobe. */
if (dev->output_enabled && (dev->ext || dev->epp) &&
    (dev->ctrl & 0x20) && !(val & 0x20) && dev->dt &&
    dev->dt->write_data && dev->dt->priv)
    dev->dt->write_data(dev->dat, dev->dt->priv);
```

This ensures that when direction switches from input (bit 5 set) to output
(bit 5 clear), any previously latched data byte is forwarded before the new
control value takes effect.

### Status/ACK handshake

The `NIBBLE` flag enables the reverse status channel.  The pipe reader (e.g.
`fifo_printer.py`) writes status bytes to the `.in` FIFO, and 86Box maps them to
the PC status register at port 0x379:

    status_register = (pipe_status_byte << 3) ^ 0x80

A typical handshake sequence for a ready printer:

    1. Reader sends READY_STATUS (0x0B) — nAck high, Select, not Busy
    2. Guest writes data byte, asserts STROBE
    3. Reader sends ACK_LOW (0x03) for ~1 µs, then back to READY_STATUS
    4. Guest sees nAck pulse, sends next byte

This is the same handshake a real IBM 4019 or compatible printer would use,
making it transparent to the guest OS.

## 86Box VM configuration

In `86box.cfg`, set the LPT1 device to a named pipe with mode 3:

```ini
[Ports (COM & LPT)]
lpt1_device = pipe

[Named Pipe (LPT) #1]
path = /tmp/my-printer
lpt_mode = 3
```

The pipe reader (`fifo_printer.py`) validates this configuration at startup and
refuses to run if `lpt_mode` is not 3.

## Example: capturing a print job

```bash
# Start 86Box VM, then in another terminal:
python3 fifo_printer.py --vm "My VM" -o ./captured-pages

# Print from inside the guest.  Pages appear as:
#   ./captured-pages/page0001.txt
#   ./captured-pages/page0002.txt
```

## Files

- `src/device/lpt.c` — direction-switch fix in control-register write handler
- `src/char/char_pipe.c` — mode 3 pipe initialisation (NIBBLE | USESTROBE)
- `src/include/86box/char.h` — CHAR_LPT_USESTROBE and CHAR_LPT_NIBBLE flags
- `fifo_printer.py` — reference pipe reader with status handshake
- `test_fifo_printer.py` — unit tests for the reader
