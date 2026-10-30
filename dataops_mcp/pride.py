"""PRIDE Archive(질량분석 프로테오믹스) discovery.

근거 (2026-10-30 확인, 공식 클라이언트 PRIDE-Archive/pridepy 소스):
- 공개 데이터 API: https://www.ebi.ac.uk/pride/ws/archive/v3/
  GET projects/{PXD}             : 프로젝트 메타데이터
  GET projects/{PXD}/files/all   : 전체 파일 배열 (fileName, fileSizeBytes, fileCategory.value,
                                   publicFileLocations[{name:'FTP Protocol', value:'ftp://ftp.pride.ebi.ac.uk/...'}])
  GET files/checksum/{PXD}       : 'File-Name  File-MD5Checksum  File-Size' TSV (파일 레코드의 checksum은 비어 있음)
- HTTPS 다운로드: ftp://ftp.pride.ebi.ac.uk/ -> https://ftp.pride.ebi.ac.uk/ (pridepy ARCHIVE_HTTPS_URL_PREFIX)
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB

PRIDE_API = "https://www.ebi.ac.uk/pride/ws/archive/v3"
ACC_RE = re.compile(r"^PXD\d{6}$")
CATEGORIES = ("RAW", "SEARCH", "RESULT", "PEAK", "OTHER", "FASTA", "GEL", "SPECTRUM_LIBRARY")


def _https(location: str) -> str:
    return re.sub(r"^ftp://ftp\.pride\.ebi\.ac\.uk/", "https://ftp.pride.ebi.ac.uk/", location)


def _checksums(client: httpx.Client, log: http.ApiLog, acc: str, retries: int) -> dict[str, str]:
    try:
        resp = http.request(client, log, "GET", f"{PRIDE_API}/files/checksum/{acc}", retries=retries)
    except http.ApiError:
        return {}
    out = {}
    for row in csv.DictReader(io.StringIO(resp.text.strip()), delimiter="\t"):
        name = row.get("File-Name") or row.get("fileName")
        md5 = row.get("File-MD5Checksum") or row.get("checksum")
        if name and md5:
            out[name] = md5
    return out


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    categories: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    if not ACC_RE.match(acc):
        raise ValueError(f"PRIDE accession 형식이 아닙니다: {accession!r} (예: PXD008644)")
    wanted = {c.upper() for c in (categories or [])}
    if wanted - set(CATEGORIES):
        raise ValueError(f"categories는 {CATEGORIES} 중에서 고르세요.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    project = http.request(client, log, "GET", f"{PRIDE_API}/projects/{acc}", retries=retries).json()
    listing = http.request(client, log, "GET", f"{PRIDE_API}/projects/{acc}/files/all", retries=retries).json()
    md5 = _checksums(client, log, acc, retries)

    rows, by_cat = [], {}
    for f in listing if isinstance(listing, list) else []:
        cat = str(((f.get("fileCategory") or {}).get("value")) or "OTHER").upper()
        if wanted and cat not in wanted:
            continue
        ftp = next((loc.get("value") for loc in f.get("publicFileLocations", [])
                    if "FTP" in str(loc.get("name", "")).upper()), None)
        if not ftp:
            continue
        rows.append(urlfiles.make_row(_https(ftp), filename=f.get("fileName"), subdir=cat.lower(),
                                      size=f.get("fileSizeBytes"), checksum=md5.get(f.get("fileName"))))
        by_cat[cat] = by_cat.get(cat, 0) + 1
    rows = urlfiles.filter_rows(rows, include, exclude)
    names = lambda key: [x.get("name") for x in project.get(key) or [] if isinstance(x, dict)]  # noqa: E731
    extra = {
        "accession": acc,
        "title": project.get("title"),
        "organisms": names("organisms"),
        "instruments": names("instruments"),
        "experiment_types": names("experimentTypes"),
        "submission_type": project.get("submissionType"),
        "publication_date": project.get("publicationDate"),
        "doi": project.get("doi"),
        "by_category": by_cat,
    }
    params = {"accession": acc, "categories": sorted(wanted), "include": include, "exclude": exclude}
    return urlfiles.save_url_query(cfg, db, "pride", rows, params, log.calls, extra)
