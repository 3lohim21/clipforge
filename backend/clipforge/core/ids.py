"""Time-ordered UUIDv7 identifiers (RFC 9562).

Random UUIDv4 keys scatter inserts across the whole B-tree; v7 keys are monotonic by
millisecond so new rows land on the right-most index page, keeping indexes compact and
cache-friendly as tables grow into the millions.
"""

from __future__ import annotations

import os
import time
import uuid


def new_id() -> uuid.UUID:
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = (rand >> 62) & 0xFFF  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (
        ((ts_ms & ((1 << 48) - 1)) << 80)
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122 variant
        | rand_b
    )
    return uuid.UUID(int=value)
