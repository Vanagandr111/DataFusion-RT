from __future__ import annotations

import logging
import re

MASS_PATTERNS = (
    re.compile(
        r"(?P<value>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>g|kg|mg|lb|lbs|oz)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:gross|nett?|net)\s*[:=]?\s*(?P<value>[+-]?\d+(?:[.,]\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<value>[+-]?\d+(?:[.,]\d+)?)"),
)


def parse_mass_line(raw_line: str, logger: logging.Logger | None = None) -> float | None:
    cleaned = sanitize_ascii_line(raw_line)
    if not cleaned:
        return None

    for pattern in MASS_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue

        token = match.group("value").replace(",", ".")
        try:
            return float(token)
        except ValueError:
            continue

    if logger is not None:
        logger.debug("Scale line did not match known formats: %r", cleaned)
    return None


def sanitize_ascii_line(raw_line: str) -> str:
    cleaned = raw_line.replace("\x00", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned
