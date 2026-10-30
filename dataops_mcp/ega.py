"""European Genome-phenome Archive (EGA) 공개 메타데이터 discovery.

근거 (2026-10-30 확인):
- 공식 문서 "Metadata Public API": https://ega-archive.org/discovery/metadata/public-metadata-api/
  * base URL: https://metadata.ega-archive.org
  * 객체 조회 패턴 /{object}/{accession} (studies, datasets, files, samples, runs, analyses, dacs)
  * 교차 조회 예: /datasets/EGAD00001000645/files, /files/EGAF00000056146/dacs
  * 목록 조회는 limit / offset 페이지네이션
- 구 API(https://ega-archive.org/metadata/v2/...)는 사용하지 않는다.

주의: 이 세션의 네트워크 정책 때문에 구현 시점에 위 엔드포인트를 직접 호출해 보지 못했다.
문서 원문과 공개 클라이언트(ToolUniverse ega_tool: accession_id, title, num_samples,
access_type 등 필드 사용)를 근거로 작성했다. 파일 객체의 필드명은 문서에 응답 예시가 없어
여러 후보 키를 허용하도록 작성했으니, 서버에서 `pytest -m live`로 실제 응답을 확인할 것.

EGA 데이터 파일은 전부 controlled access이며 다운로드에는 DAC 승인이 필요하다.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http
from .config import Config
from .state import StateDB, new_query_id
from .util import fmt_bytes, write_tsv

EGA_METADATA_API = "https://metadata.ega-archive.org"

ACC_RE = re.compile(r"^EGA([SDF])\d{11}$")

_NAME_KEYS = ("filename", "file_name", "relative_path", "name", "display_file_name")
_SIZE_KEYS = ("size", "file_size", "unencrypted_size", "bytes")
_MD5_KEYS = ("unencrypted_checksum", "unencrypted_md5", "checksum", "md5", "md5_checksum")

FILE_COLUMNS = ["accession_id", "dataset_accession", "filename", "size", "checksum", "file_type"]


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _get(client: httpx.Client, log: http.ApiLog, path: str, retries: int, params: dict | None = None) -> Any:
    resp = http.request(client, log, "GET", f"{EGA_METADATA_API}{path}", params=params, retries=retries)
    return resp.json()


def _as_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "response"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _list_paginated(client: httpx.Client, log: http.ApiLog, path: str, retries: int, limit: int = 1000,
                    max_items: int = 100000) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while len(items) < max_items:
        page = _as_items(_get(client, log, path, retries, params={"limit": limit, "offset": offset}))
        new = [p for p in page if str(p.get("accession_id", id(p))) not in seen]
        if not new:
            break
        for p in new:
            seen.add(str(p.get("accession_id", id(p))))
        items.extend(new)
        if len(page) < limit:
            break
        offset += limit
    return items


def _file_row(f: dict, dataset: str | None) -> dict[str, Any]:
    size = _first(f, _SIZE_KEYS)
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "accession_id": f.get("accession_id"),
        "dataset_accession": dataset,
        "filename": _first(f, _NAME_KEYS),
        "size": size,
        "checksum": _first(f, _MD5_KEYS),
        "file_type": f.get("file_type") or f.get("type"),
    }


def _dataset_brief(d: dict) -> dict[str, Any]:
    return {
        "accession_id": d.get("accession_id"),
        "title": d.get("title"),
        "num_samples": d.get("num_samples"),
        "access_type": d.get("access_type"),
        "technologies": d.get("technologies"),
        "dataset_types": d.get("dataset_types"),
        "policy_accession_id": d.get("policy_accession_id"),
    }


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include_files: bool = True,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    m = ACC_RE.match(acc)
    if not m:
        raise ValueError(f"EGA accession 형식이 아닙니다: {accession!r} (EGAS/EGAD/EGAF + 11자리 숫자)")
    kind = m.group(1)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    out: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = []

    if kind == "S":
        study = _get(client, log, f"/studies/{acc}", retries)
        datasets = [_dataset_brief(d) for d in _list_paginated(client, log, f"/studies/{acc}/datasets", retries)]
        out["study"] = {k: study.get(k) for k in ("accession_id", "title", "study_type", "pubmed_ids",
                                                   "released_date", "description")}
        if include_files:
            for d in datasets:
                for f in _list_paginated(client, log, f"/datasets/{d['accession_id']}/files", retries):
                    files.append(_file_row(f, d["accession_id"]))
    elif kind == "D":
        ds = _get(client, log, f"/datasets/{acc}", retries)
        datasets = [_dataset_brief(ds)]
        if include_files:
            files = [_file_row(f, acc) for f in _list_paginated(client, log, f"/datasets/{acc}/files", retries)]
    else:
        f = _get(client, log, f"/files/{acc}", retries)
        files = [_file_row(f, None)]

    query_id = new_query_id("ega")
    manifest_path = None
    if files:
        manifest_path = write_tsv(cfg.manifests_dir / f"{query_id}.files.tsv", files, FILE_COLUMNS)
    sizes = [f["size"] for f in files if f["size"] is not None]
    total_bytes = sum(sizes) if sizes else None
    summary = {
        "accession": acc,
        "object_type": {"S": "study", "D": "dataset", "F": "file"}[kind],
        "n_datasets": len(datasets),
        "n_files": len(files),
        "n_files_with_size": len(sizes),
        "total_bytes": total_bytes,
        "total_human": fmt_bytes(total_bytes),
        "datasets": datasets,
    }
    params = {"accession": acc, "include_files": include_files}
    db.save_query(query_id, "ega", params, log.calls, summary, str(manifest_path) if manifest_path else None,
                  "controlled")
    result = {
        "query_id": query_id,
        "summary": summary,
        "access": "controlled",
        "manifest_path": str(manifest_path) if manifest_path else None,
        "preview": files[:5],
        "api_queries": log.calls,
        "note": "EGA 파일은 모두 controlled access입니다. 다운로드 전에 DAC 승인과 기관 규정을 확인하세요.",
        **out,
    }
    if files and len(sizes) < len(files):
        result["warning"] = "일부 파일의 크기 필드를 찾지 못했습니다. 총 용량은 하한값입니다."
    return result
