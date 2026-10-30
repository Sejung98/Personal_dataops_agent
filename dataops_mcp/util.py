from __future__ import annotations

import csv
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n:.2f} PB"


def counts(values: Iterable[Any]) -> dict[str, int]:
    c = Counter("NA" if v in (None, "") else str(v) for v in values)
    return dict(c.most_common())


def write_tsv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return path


def read_tsv(path: str | os.PathLike, delimiter: str = "\t") -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter))


def as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def has_accession(filename: str, accession: str) -> bool:
    """파일명에 accession이 '단어'로 들어있는지 (SRR1이 SRR10에 매칭되지 않도록)."""
    return re.search(rf"(^|[^A-Za-z0-9]){re.escape(accession)}([^0-9]|$)", filename) is not None
