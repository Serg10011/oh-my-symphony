#!/usr/bin/env python3
"""Acquire flock on an FD kept open by the calling shell (macOS / POSIX).

The caller opens a persistent file with ``exec 9>>path`` and passes ``--fd 9``.
Do not unlock here: the parent and its test children retain the same open file
description. The kernel releases the lock when its last reference is closed.
The path must never be unlinked or renamed while any participant may use it.
"""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import stat
import sys
import time


def finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--deadline", type=finite_number, required=True, help="Unix time")
    parser.add_argument("--poll", type=finite_number, default=5.0)
    args = parser.parse_args()
    if args.poll <= 0:
        parser.error("--poll must be positive")
    try:
        if not stat.S_ISREG(os.fstat(args.fd).st_mode):
            raise ValueError("lock FD must refer to a regular file")
        started = time.monotonic()
        # Freeze the remaining budget against later wall-clock adjustments.
        until = started + max(0.0, args.deadline - time.time())
        last_note = None
        while True:
            try:
                fcntl.flock(args.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if last_note is not None:
                    print(f"test.sh: test.lock acquired after {time.monotonic() - started:.1f}s", flush=True)
                return 0
            except BlockingIOError:
                pass
            now = time.monotonic()
            left = max(0.0, until - now)
            if last_note is None or now - last_note >= 30:
                print(f"test.sh: waiting for test.lock; {left:.1f}s left in wait budget", flush=True)
                last_note = now
            if left <= 0:
                print("ENV: test lock busy (wait budget exhausted)", file=sys.stderr)
                return 75
            time.sleep(min(args.poll, left))
    except (OSError, ValueError) as exc:
        print(f"ENV: cannot acquire test lock: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    sys.exit(main())
