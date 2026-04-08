"""In-memory tag value history — ring buffer for trend charts.

Keeps the last *max_samples* readings per tag.  No persistence, no
database — designed for lightweight testing use only.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any


class TagHistory:
    """Thread-safe-ish ring buffer for tag value samples."""

    def __init__(self, max_samples: int = 300) -> None:
        self._buffers: dict[str, deque[dict]] = {}
        self._max = max_samples

    def record(self, tag_values: dict[str, Any]) -> None:
        """Append a timestamped sample for every tag in *tag_values*."""
        now = int(time.time() * 1000)  # epoch ms
        for tag, value in tag_values.items():
            if value is None:
                continue
            try:
                v = float(value)
            except (ValueError, TypeError):
                # Skip non-numeric values (booleans stored as 0/1)
                if isinstance(value, bool) or str(value).lower() in ("true", "false"):
                    v = 1.0 if str(value).lower() == "true" or value is True else 0.0
                else:
                    continue
            buf = self._buffers.get(tag)
            if buf is None:
                buf = deque(maxlen=self._max)
                self._buffers[tag] = buf
            buf.append({"t": now, "v": v})

    def get(self, tag: str, last_n: int = 0) -> list[dict]:
        """Return ``[{t: epoch_ms, v: float}, ...]`` for *tag*.

        If *last_n* > 0, return only the most recent *last_n* samples.
        """
        buf = self._buffers.get(tag)
        if not buf:
            return []
        if last_n > 0:
            return list(buf)[-last_n:]
        return list(buf)

    def clear(self) -> None:
        """Wipe all stored history."""
        self._buffers.clear()


# Module-level singleton
history = TagHistory()
