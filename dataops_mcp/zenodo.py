"""Zenodo 레코드 파일 discovery.

근거 (2026-10-30 확인):
- GET https://zenodo.org/api/records/{id} (Accept: application/json, Zenodo legacy JSON)
  files[] = {key, size, checksum: "md5:<hex>", links.self: ".../api/records/{id}/files/{key}/content"}
  출처: zenodo/zenodo-rdm legacy/serializers/schemas/zenodojson.py, zenodo_get 클라이언트
- DOI 10.5281/zenodo.{N}: N은 버전 레코드 또는 concept 레코드. concept id 요청은 최신 버전으로 redirect된다
  (invenio-rdm-records resources.py). httpx 클라이언트는 redirect를 따라간다.
- 게스트 요청 한도는 보수적으로 분당 60회로 본다(developers.zenodo.org rate-limit 문서).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB

ZENODO_API = "https://zenodo.org/api/records"
_PATTERNS = (
    re.compile(r"10\.5281/zenodo\.(\d+)$", re.I),
    re.compile(r"zenodo\.org/(?:api/)?records?/(\d+)(?:[/?#].*)?$", re.I),
    re.compile(r"^(\d+)$"),
)


def record_id(value: str) -> str:
    v = value.strip()
    for pat in _PATTERNS:
        m = pat.search(v)
        if m:
            return m.group(1)
    raise ValueError(f"Zenodo 레코드 번호, 10.5281/zenodo.N DOI 또는 zenodo.org/records/N URL이 아닙니다: {value!r}")


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    record: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    rid = record_id(record)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    # concept id는 최신 버전으로 redirect된다(클라이언트가 redirect를 따라감)
    rec = http.request(client, log, "GET", f"{ZENODO_API}/{rid}", headers={"Accept": "application/json"},
                       retries=retries).json()
    meta = rec.get("metadata") or {}
    rows = []
    for f in rec.get("files") or []:
        key = f.get("key") or f.get("filename")
        url = (f.get("links") or {}).get("self") or f"https://zenodo.org/records/{rec.get('id', rid)}/files/{key}"
        rows.append(urlfiles.make_row(url, filename=key, size=f.get("size", f.get("filesize")),
                                      checksum=f.get("checksum")))
    rows = urlfiles.filter_rows(rows, include, exclude)
    resolved = str(rec.get("id") or rec.get("recid") or rid)
    extra = {
        "record_id": resolved,
        "requested_id": rid,
        "resolved_to_latest_version": resolved != rid,
        "concept_record_id": rec.get("conceptrecid"),
        "title": meta.get("title") or rec.get("title"),
        "doi": rec.get("doi") or meta.get("doi"),
        "version": meta.get("version"),
        "publication_date": meta.get("publication_date"),
        "license": (meta.get("license") or {}).get("id") if isinstance(meta.get("license"), dict) else meta.get("license"),
        "resource_type": (meta.get("resource_type") or {}).get("type") if isinstance(meta.get("resource_type"), dict) else None,
    }
    out = urlfiles.save_url_query(cfg, db, "zenodo", rows, {"record": record, "include": include,
                                                              "exclude": exclude}, log.calls, extra)
    if not rec.get("files"):
        out["hint"] = "파일이 없습니다. 접근 제한(restricted) 레코드이거나 메타데이터만 있는 레코드일 수 있습니다."
    return out
