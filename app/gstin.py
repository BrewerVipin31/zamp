"""GSTIN (Indian GST registration number) format + checksum validation."""
import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        p = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return _CHARS[(36 - total % 36) % 36]


def validate(gstin: str | None) -> tuple[bool, str]:
    if not gstin:
        return False, "missing"
    g = gstin.strip().upper()
    if not _PATTERN.match(g):
        return False, "bad format"
    if checksum_char(g[:14]) != g[14]:
        return False, "checksum mismatch"
    return True, "valid"


def state_code(gstin: str | None) -> str | None:
    return gstin[:2] if gstin and len(gstin) >= 2 else None


def make(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)
