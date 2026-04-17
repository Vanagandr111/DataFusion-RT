from __future__ import annotations

import logging
import re

MASS_PATTERNS = (
    re.compile(
        r"(?P<sign>[+-])?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?P<suffix>[+-])?\s*(?P<unit>g|kg|mg|lb|lbs|oz)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:gross|nett?|net)\s*[:=]?\s*(?P<sign>[+-])?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?P<suffix>[+-])?",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<sign>[+-])?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?P<suffix>[+-])?"),
)


def parse_mass_line(
    raw_line: str, logger: logging.Logger | None = None
) -> float | None:
    cleaned = sanitize_ascii_line(raw_line)
    if not cleaned:
        return None

    cleaned_lower = cleaned.lower()
    if "err" in cleaned_lower or "error" in cleaned_lower:
        if logger is not None:
            logger.warning("Строка весов содержит маркер ошибки и пропущена: %r", cleaned)
        return None

    for pattern in MASS_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue

        token = match.group("value").replace(",", ".")
        sign = match.groupdict().get("sign") or match.groupdict().get("suffix") or ""
        try:
            return float(f"{sign}{token}")
        except ValueError:
            continue

    if logger is not None:
        logger.debug("Scale line did not match known formats: %r", cleaned)
    return None


def sanitize_ascii_line(raw_line: str) -> str:
    cleaned = raw_line.replace("\x00", " ").strip()
    cleaned = cleaned.replace("−", "-").replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned
