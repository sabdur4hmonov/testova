from __future__ import annotations

import math
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{size_bytes / p:.1f} {units[i]}"


def truncate(text: str, max_len: int = 50) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def chunk_list(lst: list, size: int) -> list[list]:
    return [lst[i: i + size] for i in range(0, len(lst), size)]
