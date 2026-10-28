"""Shared helpers for recognising login pages, risk challenges and signature walls.
Each connector keeps its own markers and check_* functions next to its reader."""

from __future__ import annotations
import re
from typing import Optional
from .errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    RiskBlocked,
    RiskSignatureRequired,
)


def _contains_any(text: str, markers) -> Optional[str]:
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return marker
    return None


def _safe_preview(text: str, limit: int = 200) -> str:
    """Tag-stripped excerpt for error reporting; never echo a cookie."""

    if not text:
        return ""
    cleaned = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if any(token in cleaned for token in ("JSESSIONID=", "tk=", "uamtk=")):
        return ""
    return cleaned[:limit]


def _texts(payload) -> str:
    if isinstance(payload, dict):
        parts = []
        for key in ("errmsg", "errno", "message", "msg", "ret", "error"):
            if key in payload:
                parts.append(str(payload.get(key)))
        return " ".join(parts)
    return str(payload)


def _contains_any_lower(text: str, markers) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in markers)
