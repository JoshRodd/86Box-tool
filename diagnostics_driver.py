#!/usr/bin/env python3
"""Drive the IBM PS/2 Model 25/30 Advanced Diagnostics to the fixed-disk
test through the RSP gdb-stub, for one or more VMs at once.

State-driven: no fixed-duration waits beyond short post-injection pauses.
Every transition is detected by polling the guest screen through the 86Box
memdump server (src/memdump.c), which reads the emulated memory from a
dedicated thread and never touches the CPU.  Keys are re-injected when the
emulated keyboard drops them.

Crucial: the GDB stub pauses the CPU for the whole time a client is
connected, so this driver NEVER holds an RSP connection while polling.  A
short-lived connection is opened only to inject a key sequence and is
closed immediately afterwards; the guest then resumes and processes the
keys.  The fixed-disk option number is read from the on-screen test list
rather than hardcoded, so the same driver works on the 8525 and 8530
diagnostics images.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from typing import Callable

from rsp import RSP, MemDump, TEXT_VIDEO_ADDRESS, inject_keyboard_scan_byte, render_text_screen

SCAN_ENTER = 0x1C
_SCANS = {
    "0": 0x0B, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05,
    "5": 0x06, "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A,
    "Y": 0x15, "N": 0x31,
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21,
    "g": 0x22, "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26,
    "m": 0x32, "n": 0x31, "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13,
    "s": 0x1F, "t": 0x14, "u": 0x16, "v": 0x2F, "w": 0x11, "x": 0x2D,
    "y": 0x15, "z": 0x2C,
    ",": 0x33,
}

Predicate = Callable[[str], bool]


class DiagnosticsDriver:
    """One driver per VM.  Screen polls go through the memdump server; key
    presses use a short-lived gdb-stub connection so the CPU is never
    paused for more than the injection itself."""

    def __init__(
        self, port: int, *, memdump_port: int | None = None,
        test_option: int = 6, drive_id: str = "C",
        poll: float = 1.0, settle: float = 4.0,
    ):
        self.port = port
        self.test_option = test_option
        self.drive_id = drive_id
        self.poll = poll
        self.settle = settle
        self.memdump = MemDump(port=memdump_port if memdump_port is not None else port + 3)

    def screen(self) -> str:
        mode = self.memdump.read(0x449, 1)[0]
        if mode not in (2, 3, 7):
            return ""
        cells = self.memdump.read(TEXT_VIDEO_ADDRESS, 80 * 25 * 2)
        return render_text_screen(cells, columns=80, rows=25)

    def wait_for(self, predicate: Predicate, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.screen()
            if predicate(text):
                return text
            time.sleep(self.poll)
        return None

    def _inject_scans(self, scans: list[int]) -> None:
        """Inject make/break scan bytes through a short-lived connection.

        Connecting pauses the guest CPU; disconnecting resumes it, so the
        connection is opened, the scans are sent (the keyboard fast path),
        and it is closed again.  The guest then processes the keys."""
        client = RSP(host="127.0.0.1", port=self.port, timeout=5.0)
        try:
            for scan in scans:
                inject_keyboard_scan_byte(client, scan & 0x7F, make=not (scan & 0x80))
        finally:
            client.detach()

    def _key_scans(self, key: str | int) -> list[int]:
        """Encode one key as XT set-1 make/break bytes, holding the shift
        across the letter for uppercase letters and shifted symbols."""
        if isinstance(key, int):
            scan, shifted = key, False
        else:
            scan = _SCANS.get(key.lower(), _SCANS.get(key))
            if scan is None:
                raise ValueError(f"no scan code for {key!r}")
            shifted = key.isupper() or key in '~!@#$%^&*()_+{}|:"<>?'
        scans: list[int] = []
        if shifted:
            scans.append(0x2A)  # left shift make
        scans.append(scan)
        scans.append(scan | 0x80)
        if shifted:
            scans.append(0xAA)  # left shift break
        return scans

    def _inject_until(self, scans: list[int], expect: Predicate, *, attempts: int = 10) -> bool:
        """Inject a scan sequence until `expect` matches the screen."""
        for _ in range(attempts):
            if expect(self.screen()):
                return True
            self._inject_scans(scans)
            time.sleep(self.settle)
            if expect(self.screen()):
                return True
        return expect(self.screen())

    def press_until(self, key: str, expect: Predicate, *, attempts: int = 10) -> bool:
        """Inject a key (short-lived connection) until `expect` matches."""
        return self._inject_until(self._key_scans(key), expect, attempts=attempts)

    def type_text_enter(self, text: str, expect: Predicate, *, attempts: int = 12) -> bool:
        """Type arbitrary text (digits, letters, punctuation) and ENTER."""
        scans: list[int] = []
        for character in text:
            if character.isalpha():
                if character.lower() not in _SCANS:
                    raise ValueError(f"no scan code for {character!r}")
            elif character not in _SCANS:
                raise ValueError(f"no scan code for {character!r}")
            scans.extend(self._key_scans(character))
        scans.extend(self._key_scans(SCAN_ENTER))
        return self._inject_until(scans, expect, attempts=attempts)

    def type_number_enter(self, number: int, expect: Predicate, *, attempts: int = 8) -> bool:
        """Type a menu number and ENTER, re-trying until `expect` matches."""
        return self.type_text_enter(str(number), expect, attempts=attempts)

    def wait_for_menu(self, timeout: float = 600.0) -> str | None:
        """Wait for any known diagnostics screen (menu, list prompt, checkout
        menu or test list), so the driver can continue from the VM's current
        state instead of assuming a fresh boot."""
        def known(text: str) -> bool:
            return any(
                marker in text
                for marker in ("SELECT AN OPTION", "IS THE LIST CORRECT",
                               "RUN TESTS ONE TIME", "SELECT OPTION NUMBER",
                               "FIXED DISK DIAGNOSTIC MENU", "HARD COPY")
            )
        return self.wait_for(known, timeout)

    def fixed_disk_option(self, test_list: str) -> int | None:
        """Extract the FIXED DISK option number from the test-list screen."""
        for line in test_list.splitlines():
            match = re.match(r"\s*(\d+)\s*-\s*.*FIXED DISK", line)
            if match:
                return int(match.group(1))
        return None

    def run_fixed_disk_test(self, capture: float = 30.0) -> dict[str, object]:
        """Drive the diagnostics from wherever it currently is through the
        fixed-disk sub-test and return the observed screens and the verdict."""
        log: dict[str, object] = {"port": self.port}

        def at_menu(t: str) -> bool:
            return "SELECT AN OPTION" in t

        def at_list_ok(t: str) -> bool:
            return "IS THE LIST CORRECT" in t

        def at_checkout(t: str) -> bool:
            return "RUN TESTS ONE TIME" in t

        def at_test_list(t: str) -> bool:
            return "SELECT OPTION NUMBER" in t

        def at_fd_menu(t: str) -> bool:
            return "FIXED DISK DIAGNOSTIC MENU" in t

        def at_hard_copy(t: str) -> bool:
            return "HARD COPY" in t

        state = self.wait_for_menu()
        if state is None:
            log["error"] = "no diagnostics screen appeared; is the VM booted?"
            return log

        # Walk forward through the states; each step runs only when the VM
        # is at or before that state, so a mid-flow start continues cleanly.
        if at_menu(state):
            # SELECT AN OPTION -> 0 (SYSTEM CHECKOUT)
            if not self.type_number_enter(
                0, lambda t: at_list_ok(t) or "PROGRAMS LOADING" in t
            ):
                log["error"] = "SYSTEM CHECKOUT did not start"
                return log
            state = self.wait_for(at_list_ok, timeout=90.0)
            if state is None:
                log["error"] = "device list prompt never appeared"
                return log

        if at_list_ok(state):
            # Y<ENTER> -> the SYSTEM CHECKOUT menu
            yes_scans = self._key_scans("Y") + self._key_scans(SCAN_ENTER)
            if not self._inject_until(yes_scans, at_checkout):
                log["error"] = "device list was not accepted"
                return log
            state = self.screen()

        if at_checkout(state):
            # 0 -> RUN TESTS ONE TIME -> the test list
            if not self.type_number_enter(0, at_test_list):
                log["error"] = "test list never appeared"
                return log
            state = self.screen()

        if at_test_list(state):
            option = self.fixed_disk_option(state)
            if option is None:
                log["error"] = "no FIXED DISK entry in the test list"
                return log
            log["fixed_disk_option"] = option
            log["test_list"] = state
            if not self.type_number_enter(option, at_fd_menu, attempts=12):
                log["error"] = "fixed-disk test selection did not register"
                return log
            state = self.screen()

        if not at_fd_menu(state) and not at_hard_copy(state):
            log["error"] = "fixed-disk diagnostic menu was not reached"
            return log
        log["fixed_disk_menu"] = True

        # Run the requested sub-test: the prompt wants
        # "OPTION NUMBER, DRIVE ID (1,C)", e.g. "6,C".
        sub_test = self.test_option
        text = f"{sub_test},{self.drive_id}"
        if not self.type_text_enter(text, lambda t: not at_fd_menu(t)):
            log["error"] = "fixed-disk sub-test %r did not start" % text
            return log
        log["sub_test"] = sub_test
        log["drive_id"] = self.drive_id

        # The verify suite asks whether a hard copy is wanted; decline it.
        if not self.wait_for(lambda t: "HARD COPY" in t, timeout=30.0):
            log["error"] = "hard-copy prompt never appeared"
            return log
        no_scans = self._key_scans("N") + self._key_scans(SCAN_ENTER)
        if not self._inject_until(no_scans, lambda t: "HARD COPY" not in t):
            log["error"] = "hard-copy prompt was not dismissed"
            return log

        # Capture the test output as it runs.
        samples: list[str] = []
        deadline = time.monotonic() + capture
        last = ""
        while time.monotonic() < deadline:
            text = self.screen()
            if text != last:
                samples.append(text)
                last = text
            time.sleep(self.poll)
        log["screens"] = samples

        verdict = self._verdict(samples)
        log["verdict"] = verdict
        log["screen"] = last
        return log

    def _verdict(self, screens: list[str]) -> str:
        joined = "\n".join(screens).upper()
        if "COULD NOT BE READ" in joined:
            return "FAIL: read errors"
        if "ERROR" in joined or "FAILED" in joined:
            return "FAIL"
        if "PASS" in joined:
            return "PASS"
        if not screens:
            return "NO OUTPUT"
        return "COMPLETED (no explicit verdict captured)"


def drive_fixed_disk(port: int, options: argparse.Namespace, results: dict) -> None:
    driver = DiagnosticsDriver(port, memdump_port=options.memdump_port,
                               test_option=options.test, drive_id=options.drive)
    try:
        results[port] = driver.run_fixed_disk_test(capture=options.capture)
    except Exception as error:  # noqa: BLE001 - report per-VM failures
        results[port] = {"port": port, "error": f"{type(error).__name__}: {error}"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, action="append", required=True, metavar="PORT",
        help="gdb-stub port of a VM to drive (repeat for simultaneous VMs)",
    )
    parser.add_argument("--memdump-port", type=int, action="append", default=None,
                        help="memdump server port per VM (default: gdb-stub port + 3)")
    parser.add_argument("--capture", type=float, default=30.0,
                        help="seconds of test output to capture (default: 30)")
    parser.add_argument("--test", type=int, default=6,
                        help="fixed-disk sub-test (default: 6 = READ VERIFY)")
    parser.add_argument("--drive", default="C",
                        help="fixed-disk drive ID for the sub-test (default: C)")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    memdump_ports = options.memdump_port or [None] * len(options.port)
    if len(memdump_ports) != len(options.port):
        raise SystemExit("--memdump-port count must match --port count")
    results: dict[int, dict[str, object]] = {}
    threads = [
        threading.Thread(
            target=drive_fixed_disk, args=(port, options, results),
            name=f"drive-{port}",
        )
        for port in options.port
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for port in options.port:
        result = results.get(port, {"error": "no result"})
        print(f"=== VM on port {port} ===")
        print(f"  boot:      {result.get('boot', '?')}")
        if "fixed_disk_option" in result:
            print(f"  fixed-disk option: {result['fixed_disk_option']}")
        if "test_list" in result:
            fixed = [l.strip() for l in result["test_list"].splitlines()
                     if "FIXED DISK" in l.upper()]
            print(f"  test list: {fixed[0] if fixed else '?'}")
        print(f"  verdict:   {result.get('verdict', '?')}")
        if "error" in result:
            print(f"  error:     {result['error']}")
        if "screens" in result:
            print("  --- captured test output ---")
            for text in result["screens"]:
                lines = [l for l in text.splitlines() if l.strip()]
                if lines:
                    print("    " + " | ".join(lines[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
