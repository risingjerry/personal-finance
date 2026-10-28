"""Shared row format for every connector: make_record / stable_id / normalize_* / write_rows.

Normalized, de-duplicable rows written next to every raw export.

Every fetch writes the original JSON and, alongside it,
``<site>-rows-<ts>.jsonl`` (mode 0600): one JSON object per record::

    {source, record_type, id, occurred, amount, direction,
     merchant, title, status, extra}

``occurred`` is Beijing time ``YYYY-MM-DD HH:MM:SS`` (or null), ``amount`` is a
positive yuan string, and ``direction`` is ``out`` / ``in`` / null.  ``id`` is
stable across runs so a consumer can de-duplicate by ``(source, id)``.
"""

from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from .output import ensure_out_dir


"""Normalized, de-duplicable rows written next to every raw export.

Every fetch writes the original JSON and, alongside it,
``<site>-rows-<ts>.jsonl`` (mode 0600): one JSON object per record::

    {source, record_type, id, occurred, amount, direction,
     merchant, title, status, extra}

``occurred`` is Beijing time ``YYYY-MM-DD HH:MM:SS`` (or null), ``amount`` is a
positive yuan string, and ``direction`` is ``out`` / ``in`` / null.  ``id`` is
stable across runs so a consumer can de-duplicate by ``(source, id)``.
"""

BEIJING = datetime.timezone(datetime.timedelta(hours=8))

RECORD_TYPES = ("order", "bill", "transaction", "trip")

DIRECTIONS = ("out", "in", None)

_AMOUNT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

_DATETIME_RE = re.compile(
    r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

def _unwrap(value):
    if isinstance(value, dict):
        for key in ("value", "text", "amount", "price", "name"):
            if key in value:
                return _unwrap(value[key])
    return value

def _text(value) -> Optional[str]:
    if value is None:
        return None
    value = _unwrap(value)
    text = str(value).strip()
    return text or None

def normalize_amount(value) -> Optional[str]:
    """Return a positive plain yuan string with two decimals, or None.

    ``¥1,234``, ``1234.5元``, ``+12.30`` and ``-8`` all become ``1234.00`` /
    ``1234.50`` / ``12.30`` / ``8.00``; callers fill ``direction`` from each
    site's transaction semantics.
    """

    value = _unwrap(value)
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    try:
        number = Decimal(match.group(0))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return format(abs(number).quantize(Decimal("0.01")), "f")

def normalize_occurred(value) -> Optional[str]:
    """Normalize a date/date-time/epoch to Beijing ``YYYY-MM-DD HH:MM:SS``."""

    value = _unwrap(value)
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=BEIJING)
        return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return datetime.datetime(
            value.year, value.month, value.day, tzinfo=BEIJING
        ).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        if number < 10_000_000_000:  # seconds, not milliseconds
            number *= 1000
        try:
            return datetime.datetime.fromtimestamp(
                number / 1000, BEIJING
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    match = _DATETIME_RE.search(str(value))
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        moment = datetime.datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=BEIJING,
        )
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S")

def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )

def stable_id(
    source: str, record_type: str, native_id=None, *fields
) -> str:
    """Build a stable id: native ids are used verbatim, otherwise a hash.

    The ``source:record_type:`` prefix keeps ids unique across sites while
    remaining identical on every re-run of the same record.
    """

    native = _text(native_id)
    if native is not None:
        return f"{source}:{record_type}:{native}"
    digest = hashlib.sha1(canonical(fields).encode("utf-8")).hexdigest()[:20]
    return f"{source}:{record_type}:h:{digest}"

def make_record(
    *,
    source: str,
    record_type: str,
    id: str,
    occurred,
    amount,
    direction: Optional[str],
    merchant,
    title,
    status,
    extra: Optional[Dict] = None,
) -> Dict:
    if record_type not in RECORD_TYPES:
        raise ValueError(f"unsupported record_type: {record_type}")
    if direction not in DIRECTIONS:
        raise ValueError(f"unsupported direction: {direction}")
    return {
        "source": source,
        "record_type": record_type,
        "id": id,
        "occurred": normalize_occurred(occurred),
        "amount": normalize_amount(amount),
        "direction": direction,
        "merchant": _text(merchant),
        "title": _text(title),
        "status": _text(status),
        "extra": extra or {},
    }

def _first_item_title(items) -> Optional[str]:
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("title"):
                return _text(item.get("title"))
    return None

def _decimal_or_none(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("¥", "").replace("￥", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None

def stamp_from_raw(raw_path: Path, prefix: str) -> str:
    """Derive the timestamp used by a raw export filename."""

    name = Path(raw_path).name
    stem = name[:-5] if name.endswith(".json") else name
    marker = prefix + "-"
    if stem.startswith(marker):
        return stem[len(marker):] or "unknown"
    return "unknown"

def write_rows(
    out_dir: Path,
    site: str,
    records: Sequence[Dict],
    *,
    stamp: str,
) -> Path:
    """Atomically write ``<site>-rows-<stamp>.jsonl`` with mode 0600."""

    out_dir = ensure_out_dir(out_dir)
    base = f"{site}-rows-{stamp}"
    candidate = out_dir / f"{base}.jsonl"
    counter = 1
    while candidate.exists():
        candidate = out_dir / f"{base}-{counter}.jsonl"
        counter += 1

    fd, temp_name = tempfile.mkstemp(
        prefix=".pfin-rows-", suffix=".tmp", dir=str(out_dir)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, candidate)
        os.chmod(candidate, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return candidate
