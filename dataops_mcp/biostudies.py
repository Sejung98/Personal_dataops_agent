"""ArrayExpress(EMBL-EBI BioStudies) 처리 데이터 discovery.

근거 (2026-10-30 확인):
- Bioconductor ArrayExpress 패키지(BioStudies 기반으로 재작성) R/getAE.r:
  GET https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}  (section -> subsections -> files 표)
  GET https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}/info  (httpLink/ftpLink, files 개수)
  다운로드 URL = {httpLink}/Files/{path}   ※ httpLink는 샤드 규칙이 달라 직접 만들지 않는다
- 큰 study는 study JSON에 파일이 다 없을 수 있어 페이지 목록 GET /studies/{acc}/files?limit=&offset= 를
  보조로 쓴다(기본 25개만 주므로 limit/offset을 명시). 필드 대소문자(size/Size, path/Name)를 모두 허용.
- 시퀀싱 원시 read는 대부분 ENA에 있다(section.links의 Type=ENA). 그것은 sra_search로 받는다.
- 체크섬 없음(크기로만 검증). 미러가 잘린 파일을 200으로 줄 수 있어 다운로드 스크립트가 크기를 대조한다.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB

BIOSTUDIES_API = "https://www.ebi.ac.uk/biostudies/api/v1"
ACC_RE = re.compile(r"^E-[A-Z]{4}-\d+$")

_SUBDIR = {"processed data": "processed", "raw data": "raw", "mage-tab files": "magetab", "array designs": "adf"}


def _attr(items: list[dict] | None, name: str) -> Any:
    for a in items or []:
        if str(a.get("name", "")).lower() == name.lower():
            return a.get("value")
    return None


def _flatten(x: Any) -> list[dict]:
    if isinstance(x, dict):
        return [x]
    out: list[dict] = []
    for item in x or []:
        out.extend(_flatten(item))
    return out


def walk_files(section: dict, current: str = "") -> list[dict[str, Any]]:
    """study JSON의 section 트리에서 파일 목록을 모은다."""
    stype = str(section.get("type") or "").lower()
    subdir = _SUBDIR.get(stype, current)
    found = [{"path": f.get("path") or f.get("name"), "size": f.get("size", f.get("Size")), "subdir": subdir}
             for f in _flatten(section.get("files")) if f.get("type", "file") == "file"]
    for sub in _flatten(section.get("subsections")):
        found.extend(walk_files(sub, subdir))
    return [f for f in found if f["path"]]


def ena_links(section: dict) -> list[str]:
    out = []
    for link in _flatten(section.get("links")):
        if str(_attr(link.get("attributes"), "Type") or "").upper() == "ENA" and link.get("url"):
            out.append(link["url"])
    for sub in _flatten(section.get("subsections")):
        out.extend(ena_links(sub))
    return sorted(set(out))


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    if not ACC_RE.match(acc):
        raise ValueError(f"ArrayExpress accession 형식이 아닙니다: {accession!r} (예: E-MTAB-9697, E-GEOD-70845)")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    study = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}", retries=retries).json()
    info = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}/info", retries=retries).json()
    if not study or not info:
        raise ValueError(f"BioStudies에서 {acc}를 찾지 못했습니다.")
    http_link = str(info.get("httpLink") or "").rstrip("/")
    if not http_link:
        raise http.ApiError(f"{acc}의 httpLink가 없습니다(비공개 study일 수 있음).")
    section = study.get("section") or {}
    listed = walk_files(section)

    expected = info.get("files")
    if isinstance(expected, int) and expected > len(listed):
        seen = {f["path"] for f in listed}
        offset, limit = 0, 200
        while True:
            page = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}/files",
                                params={"limit": limit, "offset": offset}, retries=retries).json()
            items = page.get("items") or []
            for it in items:
                path = it.get("path") or it.get("Name") or it.get("name")
                if path and path not in seen and str(it.get("type", "file")).lower() == "file":
                    listed.append({"path": path, "size": it.get("size", it.get("Size")), "subdir": ""})
                    seen.add(path)
            total = (page.get("pagination") or {}).get("total", 0)
            offset += limit
            if not items or offset >= total:
                break

    rows = [urlfiles.make_row(f"{http_link}/Files/{f['path']}", filename=f["path"].rsplit("/", 1)[-1],
                              subdir="/".join(x for x in (f["subdir"], f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "") if x),
                              size=f["size"])
            for f in listed]
    rows = urlfiles.filter_rows(rows, include, exclude)
    enas = ena_links(section)
    extra = {
        "accession": acc,
        "title": _attr(study.get("attributes"), "Title") or _attr(section.get("attributes"), "Title"),
        "release_date": _attr(study.get("attributes"), "ReleaseDate"),
        "study_type": _attr(section.get("attributes"), "Study type"),
        "organism": _attr(section.get("attributes"), "Organism"),
        "ena_accessions": enas,
        "checksums": "BioStudies는 체크섬을 제공하지 않아 크기로만 검증합니다.",
    }
    out = urlfiles.save_url_query(cfg, db, "arrayexpress", rows, {"accession": acc, "include": include,
                                                                    "exclude": exclude}, log.calls, extra)
    if enas:
        out["notes"] = [f"원시 read는 ENA {', '.join(enas)}에 있습니다. FASTQ가 필요하면 sra_search(accession='{enas[0]}')."]
    return out
