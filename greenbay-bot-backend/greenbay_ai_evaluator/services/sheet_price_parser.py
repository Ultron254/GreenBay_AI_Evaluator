"""
Parse monetary cells from Google Sheets (column J/K and similar).

Human-entered values vary widely; this module is the single source of truth
for turning a cell string into KES as ``float`` or ``None``.
"""

from __future__ import annotations

import re
import unicodedata


_SENTINEL_COMPACT = frozenset(
    {
        "n/a",
        "na",
        "-",
        "--",
        "—",
        "–",
        "nil",
        "none",
        "tbd",
        "pending",
    }
)

_NUM_CAPTURE = r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_K_SUFFIX_RE = re.compile(
    rf"(?<![\d.]){_NUM_CAPTURE}\s*k\b",
    re.IGNORECASE,
)
_PLAIN_NUM_RE = re.compile(
    rf"(?<![\d.]){_NUM_CAPTURE}(?!\s*k\b)",
    re.IGNORECASE,
)


def _float_from_num_group(num_group: str) -> float | None:
    try:
        val = float(num_group.replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None


def parse_sheet_price(text: str | None) -> float | None:
    """Parse a Sheet price string into KES.

    Returns a positive float, or ``None`` when blank / explicit sentinel / unparseable.

    Supported examples:

    - ``35K``, ``35k``, ``35 k`` → ``35000``
    - ``9.5k`` → ``9500``
    - ``35,000``, ``35000`` → ``35000``
    - ``KES 35,000``, ``ksh 35000`` → ``35000``
    - ``25k to 28k`` → average of the two (when both sides parse)
    - ``N/A``, ``na``, ``-``, ``--``, empty → ``None``
    """
    if text is None:
        return None

    raw = unicodedata.normalize("NFKC", str(text))
    s = re.sub(r"\s+", " ", raw.strip())
    if not s:
        return None

    low = s.lower()
    compact = low.replace(" ", "").replace("_", "")
    if compact in _SENTINEL_COMPACT:
        return None

    # Range: average of parsed endpoints (same semantics as legacy learner).
    if " to " in low:
        parts = low.split(" to ")
        prices = [parse_sheet_price(p.strip()) for p in parts]
        valid = [p for p in prices if p is not None and p > 0]
        if not valid:
            return None
        return sum(valid) / len(valid)

    working = low
    working = re.sub(r"\b(kes|ksh)\b\.?", " ", working)
    working = re.sub(r"\s+", " ", working).strip()

    mk = _K_SUFFIX_RE.search(working)
    if mk:
        base = _float_from_num_group(mk.group("num"))
        if base is not None:
            return base * 1000

    mp = _PLAIN_NUM_RE.search(working)
    if mp:
        return _float_from_num_group(mp.group("num"))

    return None
