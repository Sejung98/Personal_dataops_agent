"""ENCODE 포털 discovery (실험 검색, 실험별 파일 목록).

근거 (2026-10-30 확인, ENCODE-DCC/encoded·snovault-search 소스):
- JSON은 format=json을 붙여야 한다(없으면 HTML). 공개(released) 데이터는 인증 없음.
- GET /search/?type=Experiment&<필드>=<값>&field=...&limit=all&format=json
  응답: @graph, total. 결과 0건이면 HTTP 404(notification "No results found") -> 빈 결과로 처리.
  필터 필드: assay_title, biosample_ontology.term_name, target.label, assembly, status,
  replicates.library.biosample.donor.organism.scientific_name, searchTerm(자유 검색)
- 실험의 파일: GET /search/?type=File&dataset=/experiments/{ENCSR}/&limit=all&format=json
  (포털 UI filegallery.js와 같은 방식). 파일 필드: accession, file_format, file_type, output_type, assembly,
  file_size, md5sum(받는 파일 그대로의 md5), href, status, biological_replicates, restricted, no_file_available
- 다운로드 URL = https://www.encodeproject.org + href (307 redirect -> 서명된 S3 URL, curl -L로 따라감)
- 공식 안내: 초당 GET 10회 이하
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB, new_query_id
from .util import counts, write_tsv

ENCODE = "https://www.encodeproject.org"
EXP_RE = re.compile(r"^ENCSR\d{3}[A-Z]{3}$")

ORGANISM_FIELD = "replicates.library.biosample.donor.organism.scientific_name"
EXPERIMENT_FIELDS = ["accession", "assay_title", "biosample_ontology.term_name", "target.label", "assembly",
                     "status", "description", ORGANISM_FIELD]
FILE_FIELDS = ["accession", "file_format", "file_type", "output_type", "assembly", "file_size", "md5sum", "href",
               "status", "biological_replicates", "restricted", "no_file_available", "dataset", "preferred_default"]
EXPERIMENT_COLUMNS = ["accession", "assay_title", "biosample", "target", "organism", "assembly", "status"]


def _search(client: httpx.Client, log: http.ApiLog, params: dict[str, Any], retries: int) -> dict[str, Any]:
    try:
        resp = http.request(client, log, "GET", f"{ENCODE}/search/", params=params, retries=retries)
    except http.ApiError as exc:
        if "HTTP 404" in str(exc):
            return {"@graph": [], "total": 0}
        raise
    return resp.json()


def _get(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = [c.get(part) if isinstance(c, dict) else None for c in cur]
            cur = [c for c in cur if c is not None]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _flat(v: Any) -> str:
    if isinstance(v, list):
        vals = []
        for x in v:
            vals.extend(x if isinstance(x, list) else [x])
        return ",".join(sorted({str(x) for x in vals if x not in (None, "")}))
    return "" if v is None else str(v)


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    assay_title: Any = None,
    biosample: Any = None,
    target: Any = None,
    organism: Any = None,
    assembly: Any = None,
    search_term: str | None = None,
    status: str = "released",
    limit: int = 500,
) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "Experiment", "format": "json", "limit": limit, "field": EXPERIMENT_FIELDS,
                              "status": status}
    for key, value in (("assay_title", assay_title), ("biosample_ontology.term_name", biosample),
                       ("target.label", target), (ORGANISM_FIELD, organism), ("assembly", assembly)):
        if value:
            params[key] = value
    if search_term:
        params["searchTerm"] = search_term
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    data = _search(client, log, params, retries)
    rows = [{
        "accession": e.get("accession"),
        "assay_title": e.get("assay_title"),
        "biosample": _flat(_get(e, "biosample_ontology.term_name")),
        "target": _flat(_get(e, "target.label")),
        "organism": _flat(_get(e, ORGANISM_FIELD)),
        "assembly": _flat(e.get("assembly")),
        "status": e.get("status"),
    } for e in data.get("@graph", [])]
    query_id = new_query_id("encode")
    exp_path = write_tsv(cfg.manifests_dir / f"{query_id}.experiments.tsv", rows, EXPERIMENT_COLUMNS) if rows else None
    summary = {
        "n_experiments": int(data.get("total", len(rows))),
        "n_retrieved": len(rows),
        "truncated": int(data.get("total", len(rows))) > len(rows),
        "by_assay": counts(r["assay_title"] for r in rows),
        "by_biosample": dict(list(counts(r["biosample"] for r in rows).items())[:20]),
        "by_target": dict(list(counts(r["target"] for r in rows).items())[:20]),
        "by_organism": counts(r["organism"] for r in rows),
        "by_assembly": counts(r["assembly"] for r in rows),
    }
    db.save_query(query_id, "encode", {k: v for k, v in params.items() if k != "field"}, log.calls, summary, None,
                  "open")
    return {
        "query_id": query_id,
        "summary": summary,
        "experiments_path": str(exp_path) if exp_path else None,
        "preview": rows[:10],
        "api_queries": log.calls,
        "next_step": "받을 실험을 정하면 encode_files(experiments=[...] 또는 from_query_id=이 query_id)로 파일을 고르세요.",
    }


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    experiments: list[str] | None = None,
    from_query_id: str | None = None,
    assembly: str | None = None,
    output_type: Any = None,
    file_format: Any = None,
    preferred_default_only: bool = False,
    max_experiments: int = 200,
) -> dict[str, Any]:
    accs = [a.strip().upper() for a in (experiments or []) if a.strip()]
    if from_query_id:
        q = db.get_query(from_query_id)
        if q is None or q["source"] != "encode":
            raise KeyError(f"ENCODE 검색 query_id가 아닙니다: {from_query_id}")
        path = cfg.manifests_dir / f"{from_query_id}.experiments.tsv"
        from .util import read_tsv

        accs += [r["accession"] for r in read_tsv(path)] if path.exists() else []
    accs = list(dict.fromkeys(accs))
    bad = [a for a in accs if not EXP_RE.match(a)]
    if bad:
        raise ValueError(f"ENCODE 실험 accession(ENCSR...) 형식이 아닙니다: {bad[:5]}")
    if not accs:
        raise ValueError("experiments 또는 from_query_id가 필요합니다.")
    if len(accs) > max_experiments:
        raise ValueError(f"실험 {len(accs)}개는 너무 많습니다(max_experiments={max_experiments}). 조건을 좁히세요.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    rows, skipped, per = [], 0, []
    for acc in accs:
        params: dict[str, Any] = {"type": "File", "dataset": f"/experiments/{acc}/", "limit": "all",
                                  "format": "json", "field": FILE_FIELDS, "status": "released"}
        if assembly:
            params["assembly"] = assembly
        if output_type:
            params["output_type"] = output_type
        if file_format:
            params["file_format"] = file_format
        for f in _search(client, log, params, retries).get("@graph", []):
            if f.get("restricted") or f.get("no_file_available") or not f.get("href"):
                skipped += 1
                continue
            if preferred_default_only and not f.get("preferred_default"):
                continue
            row = urlfiles.make_row(ENCODE + f["href"], subdir=acc, size=f.get("file_size"),
                                    checksum=f.get("md5sum"))
            rows.append(row)
            per.append({"file": f.get("accession"), "output_type": f.get("output_type"),
                        "file_format": f.get("file_format"), "assembly": f.get("assembly")})
    extra = {
        "n_experiments": len(accs),
        "by_output_type": counts(p["output_type"] for p in per),
        "by_file_format": counts(p["file_format"] for p in per),
        "by_assembly": counts(p["assembly"] for p in per),
        "skipped_restricted_or_unavailable": skipped,
    }
    out = urlfiles.save_url_query(cfg, db, "encode", rows, {"experiments": accs, "assembly": assembly,
                                                              "output_type": output_type, "file_format": file_format,
                                                              "preferred_default_only": preferred_default_only},
                                  log.calls, extra)
    if len([a for a in extra["by_assembly"] if a != "NA"]) > 1:
        out["warning"] = f"여러 assembly가 섞여 있습니다: {extra['by_assembly']}. assembly='GRCh38'처럼 하나로 좁히세요."
    return out
