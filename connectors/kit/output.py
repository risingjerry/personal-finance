"""Output file handling (0600) and human summaries."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple


def ensure_out_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_path(
    out_dir: Path, prefix: str, now: Optional[datetime], suffix: str = ".json"
) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = out_dir / f"{prefix}-{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = out_dir / f"{prefix}-{stamp}-{counter}{suffix}"
        counter += 1
    return candidate


def write_json(
    out_dir: Path,
    prefix: str,
    payload: Dict,
    *,
    now: Optional[datetime] = None,
) -> Path:
    """Atomically write one JSON file with mode 0600."""

    out_dir = ensure_out_dir(out_dir)
    final_path = _unique_path(out_dir, prefix, now)
    fd, temp_name = tempfile.mkstemp(
        prefix=".pfin-", suffix=".tmp", dir=str(out_dir)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return final_path


def write_bytes(
    out_dir: Path,
    prefix: str,
    body: bytes,
    *,
    suffix: str = ".body",
    now: Optional[datetime] = None,
) -> Path:
    """Atomically write one raw body with mode 0600."""

    out_dir = ensure_out_dir(out_dir)
    final_path = _unique_path(out_dir, prefix, now, suffix=suffix)
    fd, temp_name = tempfile.mkstemp(
        prefix=".pfin-", suffix=".tmp", dir=str(out_dir)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return final_path


def file_mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def format_date_range(span: Optional[Tuple[str, str]]) -> str:
    if not span:
        return "none"
    return f"{span[0]} .. {span[1]}"


def format_summary(platform: str, summary: Dict, path: Optional[Path] = None) -> str:
    line = (
        f"{platform}: pages={summary.get('pages', 0)} "
        f"orders={summary.get('orders', 0)}"
    )
    if summary.get("months") is not None:
        line += f" months={summary.get('months')}"
    line += f" date_range={format_date_range(summary.get('date_range'))}"
    lines = [line]
    if path is not None:
        lines.append(f"written: {path}")
    return "\n".join(lines)


def format_baitiao_summary(summary: Dict, path: Optional[Path] = None) -> str:
    """Summary line for the Baitiao bill/transaction export."""

    lines = [
        f"jd-baitiao: bills={summary.get('bills', 0)} "
        f"transactions={summary.get('transactions', 0)} "
        f"bill_range={format_date_range(summary.get('bill_range'))} "
        f"transaction_range="
        f"{format_date_range(summary.get('transaction_range'))}"
    ]
    pages = summary.get("pages") or {}
    if pages:
        lines.append(
            "pages: "
            f"bills={pages.get('bills', 0)} "
            f"transactions={pages.get('transactions', 0)}"
        )
    if path is not None:
        lines.append(f"written: {path}")
    return "\n".join(lines)
