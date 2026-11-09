"""Short, prefixed, sortable identifiers for every record."""

from __future__ import annotations

import secrets
import time

PREFIXES = {
    "source": "src",
    "unit": "unit",
    "problem": "prob",
    "attack": "atk",
    "question": "q",
    "experiment": "exp",
    "finding": "fnd",
    "run": "run",
    "digest": "dg",
    "reading_note": "rn",
    "weakness_pattern": "wp",
}


def new_id(kind: str) -> str:
    """Return an id like ``q_01jd3k9x_7f3a2c``: prefix, time component, random tail.

    The time component keeps ids roughly sortable by creation; the random tail
    makes collisions negligible even when many workers write concurrently.
    """
    prefix = PREFIXES[kind]
    stamp = _base32(int(time.time() * 1000))
    tail = secrets.token_hex(3)
    return f"{prefix}_{stamp}_{tail}"


_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _base32(n: int) -> str:
    out = []
    while n:
        n, r = divmod(n, 32)
        out.append(_ALPHABET[r])
    return "".join(reversed(out)) or "0"
