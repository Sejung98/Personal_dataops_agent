"""NCBI GEO 처리 데이터(supplementary, series matrix) discovery.

원시 read(FASTQ)는 sra.py(GSE -> SRA run)가 담당하고, 여기서는 GEO에 올라온 파일 자체를 다룬다.

근거 (2026-10-30 확인):
- E-utilities esummary db=gds 레코드 필드(accession, title, gdstype, taxon, n_samples, suppfile, ftplink,
  bioproject, extrelations, pubmedids): R 패키지 GEOquery R/searchGEO.R 및 실제 응답 캡처
- 디렉토리 규칙: https://ftp.ncbi.nlm.nih.gov/geo/series/{stub}/{GSE}/suppl/ , .../matrix/
  stub = 마지막 1~3자리 숫자를 'nnn'으로 (GSE1234 -> GSE1nnn, GSE123 -> GSEnnn)
  GEOquery R/getGEOSuppFiles.R (gsub("\\d{1,3}$", "nnn", acc)), getDirListing()
- 디렉토리 목록은 Apache HTML index(크기는 '17M' 같은 근사치)이므로 정확한 크기는 HEAD Content-Length로 얻는다.
- GEO는 체크섬을 제공하지 않는다(크기로만 검증).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .sra import EUTILS, _eutils_params
from .state import StateDB

GEO_HTTPS = "https://ftp.ncbi.nlm.nih.gov/geo"
ACC_RE = re.compile(r"^(GSE|GSM)\d+$")
_HREF = re.compile(r'<a href="([^"?/][^"]*)">', re.I)


def stub(accession: str) -> str:
    return re.sub(r"\d{1,3}$", "nnn", accession)


def series_dir(accession: str) -> str:
    kind = "series" if accession.startswith("GSE") else "samples"
    return f"{GEO_HTTPS}/{kind}/{stub(accession)}/{accession}"


def parse_index(html: str) -> list[str]:
    """Apache 디렉토리 index에서 파일 이름 목록. 상위 디렉토리·정렬 링크·하위 디렉토리·filelist.txt 제외."""
    names = []
    for href in _HREF.findall(html):
        if href.startswith(("http", "/")) or href.endswith("/"):
            continue
        if href == "filelist.txt":
            continue
        names.append(href)
    return names


def _esummary(cfg: Config, client: httpx.Client, log: http.ApiLog, acc: str, retries: int) -> dict[str, Any]:
    es = http.request(client, log, "GET", f"{EUTILS}/esearch.fcgi",
                      params=_eutils_params(cfg, db="gds", term=f"{acc}[ACCN]", retmode="json"), retries=retries).json()
    uids = es.get("esearchresult", {}).get("idlist", [])
    if not uids:
        raise ValueError(f"GEO에서 {acc}를 찾지 못했습니다.")
    summ = http.request(client, log, "GET", f"{EUTILS}/esummary.fcgi",
                        params=_eutils_params(cfg, db="gds", id=",".join(uids[:500]), retmode="json"),
                        retries=retries).json()
    result = summ.get("result")
    if not result:
        raise http.ApiError(f"esummary 응답에 result가 없습니다: {str(summ)[:200]}")
    for uid in result.get("uids", uids):
        rec = result.get(uid, {})
        if str(rec.get("accession", "")).upper() == acc:
            return rec
    raise ValueError(f"esummary 결과에 {acc} 레코드가 없습니다.")


def _list_dir(client: httpx.Client, log: http.ApiLog, url: str, retries: int) -> list[str] | None:
    try:
        resp = http.request(client, log, "GET", url + "/", retries=retries)
    except http.ApiError as exc:
        if "404" in str(exc):
            return None
        raise
    if re.search(r"recaptcha|Checking your browser", resp.text, re.I):
        raise http.ApiError(f"{url}: NCBI가 봇 확인 페이지를 돌려줬습니다. 잠시 후 다시 시도하세요.")
    return parse_index(resp.text)


def _head_size(client: httpx.Client, log: http.ApiLog, url: str, retries: int) -> int | str:
    try:
        resp = http.request(client, log, "HEAD", url, retries=retries)
    except http.ApiError:
        return ""
    return int(resp.headers.get("content-length") or 0) or ""


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include_matrix: bool = True,
    include_samples: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_samples: int = 200,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    if not ACC_RE.match(acc):
        raise ValueError(f"GSE 또는 GSM accession이어야 합니다: {accession!r}")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    rec = _esummary(cfg, client, log, acc, retries)
    base = series_dir(acc)

    candidates: list[tuple[str, str]] = []  # (url, subdir)
    for name in _list_dir(client, log, f"{base}/suppl", retries) or []:
        candidates.append((f"{base}/suppl/{name}", "suppl"))
    if include_matrix and acc.startswith("GSE"):
        for name in _list_dir(client, log, f"{base}/matrix", retries) or []:
            candidates.append((f"{base}/matrix/{name}", "matrix"))
    samples = [s.get("accession") for s in rec.get("samples", []) if s.get("accession")]
    notes: list[str] = []
    if include_samples and acc.startswith("GSE"):
        for gsm in samples[:max_samples]:
            sdir = series_dir(gsm)
            for name in _list_dir(client, log, f"{sdir}/suppl", retries) or []:
                candidates.append((f"{sdir}/suppl/{name}", f"samples/{gsm}"))
        if len(samples) > max_samples:
            notes.append(f"GSM {len(samples)}개 중 {max_samples}개만 조회했습니다(max_samples).")

    rows = urlfiles.filter_rows([urlfiles.make_row(u, subdir=d) for u, d in candidates], include, exclude)
    for row in rows:
        row["size"] = _head_size(client, log, row["url"], retries)

    sra_studies = [x.get("targetobject") for x in rec.get("extrelations", []) if x.get("relationtype") == "SRA"]
    if sra_studies or rec.get("bioproject"):
        notes.append("원시 read(FASTQ)가 필요하면 sra_search(accession='%s')를 쓰세요." % (rec.get("bioproject") or acc))
    if any(r["filename"].endswith("_RAW.tar") for r in rows) and not include_samples:
        notes.append("_RAW.tar에 샘플별 파일이 묶여 있습니다. 샘플별로 받으려면 include_samples=True.")
    extra = {
        "accession": acc,
        "title": rec.get("title"),
        "gdstype": rec.get("gdstype"),
        "taxon": rec.get("taxon"),
        "n_samples": rec.get("n_samples"),
        "suppfile_types": rec.get("suppfile"),
        "bioproject": rec.get("bioproject"),
        "sra_studies": sra_studies,
        "pubmed_ids": rec.get("pubmedids", []),
        "release_date": rec.get("pdat"),
        "checksums": "GEO는 체크섬을 제공하지 않아 크기로만 검증합니다.",
    }
    params = {"accession": acc, "include_matrix": include_matrix, "include_samples": include_samples,
              "include": include, "exclude": exclude}
    out = urlfiles.save_url_query(cfg, db, "geo", rows, params, log.calls, extra)
    out["notes"] = notes
    if not rows:
        out["hint"] = "공개된 supplementary/matrix 파일이 없습니다. 원시 read만 있는 시리즈일 수 있습니다(sra_search)."
    return out
