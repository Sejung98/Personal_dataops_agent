"""NCI Genomic Data Commons (GDC) discovery.

근거 문서 (2026-10-30 확인, NCI-GDC/gdc-docs 저장소의 develop 브랜치 원문):
- Search & Retrieval: https://docs.gdc.cancer.gov/API/Users_Guide/Search_and_Retrieval/
  * 엔드포인트 /files, /cases, /{endpoint}/_mapping
  * 파라미터 filters, fields, size, from, facets / 대규모 쿼리는 POST 권장
  * facets 응답: data.aggregations.<field>.buckets[{key, doc_count}]
- Downloading Files / Manifest: https://docs.gdc.cancer.gov/API/Users_Guide/Downloading_Files/
  * gdc-client용 manifest 컬럼: id, filename, md5, size, state

manifest는 return_type=manifest를 따로 호출하지 않고, 같은 검색 응답에서 직접 만든다.
요약 숫자와 manifest가 반드시 같은 API 응답에서 나오도록 하기 위함이다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import http
from .config import Config
from .state import StateDB, new_query_id
from .util import as_list, counts, fmt_bytes, write_tsv

GDC_API = "https://api.gdc.cancer.gov"

# 논리적 필터 이름 -> (files 엔드포인트 필드, cases 엔드포인트 필드)
FILTER_FIELDS: dict[str, tuple[str, str]] = {
    "project": ("cases.project.project_id", "project.project_id"),
    "primary_site": ("cases.primary_site", "primary_site"),
    "disease_type": ("cases.disease_type", "disease_type"),
    "sample_type": ("cases.samples.sample_type", "samples.sample_type"),
    "data_category": ("data_category", "files.data_category"),
    "data_type": ("data_type", "files.data_type"),
    "data_format": ("data_format", "files.data_format"),
    "experimental_strategy": ("experimental_strategy", "files.experimental_strategy"),
    "workflow_type": ("analysis.workflow_type", "files.analysis.workflow_type"),
    "access": ("access", "files.access"),
}

FILE_FIELDS = [
    "file_id",
    "file_name",
    "file_size",
    "md5sum",
    "state",
    "access",
    "data_category",
    "data_type",
    "data_format",
    "experimental_strategy",
    "analysis.workflow_type",
    "cases.case_id",
    "cases.submitter_id",
    "cases.project.project_id",
    "cases.samples.submitter_id",
    "cases.samples.sample_type",
]

CASE_FIELDS = ["case_id", "submitter_id", "project.project_id", "primary_site", "disease_type"]

MANIFEST_COLUMNS = ["id", "filename", "md5", "size", "state"]
METADATA_COLUMNS = [
    "file_id",
    "file_name",
    "file_size",
    "md5sum",
    "access",
    "data_category",
    "data_type",
    "data_format",
    "experimental_strategy",
    "workflow_type",
    "project_id",
    "case_id",
    "case_submitter_id",
    "sample_submitter_id",
    "sample_type",
]

# GDC 하모나이즈 데이터의 레퍼런스 (Zhang et al. 2021, Nat Commun 12:1226)
GDC_REFERENCE = "GRCh38.d1.vd1"


def build_filters(endpoint: str, criteria: dict[str, Any]) -> dict | None:
    idx = 0 if endpoint == "files" else 1
    content = []
    for name, value in criteria.items():
        values = as_list(value)
        if not values:
            continue
        if name not in FILTER_FIELDS:
            raise ValueError(f"지원하지 않는 필터: {name}. 사용 가능: {sorted(FILTER_FIELDS)}")
        content.append({"op": "in", "content": {"field": FILTER_FIELDS[name][idx], "value": values}})
    if not content:
        return None
    return {"op": "and", "content": content}


def _flatten_file_hit(hit: dict) -> dict[str, Any]:
    cases = hit.get("cases") or []
    samples = [s for c in cases for s in (c.get("samples") or [])]
    return {
        "file_id": hit.get("file_id") or hit.get("id"),
        "file_name": hit.get("file_name"),
        "file_size": int(hit.get("file_size") or 0),
        "md5sum": hit.get("md5sum"),
        "state": hit.get("state"),
        "access": hit.get("access"),
        "data_category": hit.get("data_category"),
        "data_type": hit.get("data_type"),
        "data_format": hit.get("data_format"),
        "experimental_strategy": hit.get("experimental_strategy"),
        "workflow_type": (hit.get("analysis") or {}).get("workflow_type"),
        "project_id": ",".join(sorted({(c.get("project") or {}).get("project_id", "") for c in cases})),
        "case_id": ",".join(c.get("case_id", "") for c in cases),
        "case_submitter_id": ",".join(c.get("submitter_id", "") for c in cases),
        "sample_submitter_id": ",".join(s.get("submitter_id", "") for s in samples),
        "sample_type": ",".join(sorted({s.get("sample_type", "") for s in samples})),
        "_case_ids": [c.get("case_id") for c in cases if c.get("case_id")],
    }


def _paginate(
    client: httpx.Client,
    log: http.ApiLog,
    endpoint: str,
    filters: dict | None,
    fields: list[str],
    max_records: int,
    page_size: int,
    retries: int,
) -> tuple[list[dict], int]:
    hits: list[dict] = []
    total = 0
    offset = 0
    while True:
        body: dict[str, Any] = {
            "fields": ",".join(fields),
            "format": "JSON",
            "size": min(page_size, max_records - len(hits)),
            "from": offset,
        }
        if filters:
            body["filters"] = filters
        resp = http.request(client, log, "POST", f"{GDC_API}/{endpoint}", json=body, retries=retries)
        data = resp.json().get("data", {})
        page = data.get("hits", [])
        total = int(data.get("pagination", {}).get("total", len(page)))
        hits.extend(page)
        offset += len(page)
        if not page or offset >= total or len(hits) >= max_records:
            break
    return hits, total


def search_files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    project: Any = None,
    primary_site: Any = None,
    disease_type: Any = None,
    sample_type: Any = None,
    data_category: Any = None,
    data_type: Any = None,
    data_format: Any = None,
    experimental_strategy: Any = None,
    workflow_type: Any = None,
    access: Any = None,
    max_files: int = 50000,
    page_size: int = 1000,
) -> dict[str, Any]:
    criteria = {
        "project": project,
        "primary_site": primary_site,
        "disease_type": disease_type,
        "sample_type": sample_type,
        "data_category": data_category,
        "data_type": data_type,
        "data_format": data_format,
        "experimental_strategy": experimental_strategy,
        "workflow_type": workflow_type,
        "access": access,
    }
    criteria = {k: v for k, v in criteria.items() if as_list(v)}
    filters = build_filters("files", criteria)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    hits, total = _paginate(client, log, "files", filters, FILE_FIELDS, max_files, page_size, retries)
    rows = [_flatten_file_hit(h) for h in hits]

    query_id = new_query_id("gdc")
    case_ids = {cid for r in rows for cid in r["_case_ids"]}
    total_bytes = sum(r["file_size"] for r in rows)
    access_values = {r["access"] for r in rows}
    access_level = "controlled" if "controlled" in access_values else ("open" if rows else None)

    manifest_path = None
    if rows:
        manifest_rows = [
            {"id": r["file_id"], "filename": r["file_name"], "md5": r["md5sum"], "size": r["file_size"],
             "state": r["state"]}
            for r in rows
        ]
        manifest_path = write_tsv(cfg.manifests_dir / f"{query_id}.manifest.tsv", manifest_rows, MANIFEST_COLUMNS)
        write_tsv(cfg.manifests_dir / f"{query_id}.metadata.tsv", rows, METADATA_COLUMNS)

    summary = {
        "n_files": len(rows),
        "api_total": total,
        "truncated": total > len(rows),
        "n_cases": len(case_ids),
        "total_bytes": total_bytes,
        "total_human": fmt_bytes(total_bytes),
        "by_access": counts(r["access"] for r in rows),
        "by_project": counts(r["project_id"] for r in rows),
        "by_data_format": counts(r["data_format"] for r in rows),
        "by_experimental_strategy": counts(r["experimental_strategy"] for r in rows),
        "by_workflow_type": counts(r["workflow_type"] for r in rows),
        "by_sample_type": counts(r["sample_type"] for r in rows),
        "reference_genome": GDC_REFERENCE,
    }
    params = {"endpoint": "files", "criteria": criteria, "filters": filters}
    db.save_query(query_id, "gdc", params, log.calls, summary, str(manifest_path) if manifest_path else None,
                  access_level)
    result = {
        "query_id": query_id,
        "summary": summary,
        "access": access_level,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "metadata_path": str(cfg.manifests_dir / f"{query_id}.metadata.tsv") if rows else None,
        "preview": [{k: r[k] for k in METADATA_COLUMNS[:6] + ["case_submitter_id", "sample_type"]} for r in rows[:5]],
        "api_queries": log.calls,
        "filters_json": json.dumps(filters, ensure_ascii=False) if filters else None,
    }
    if not rows:
        result["hint"] = ("결과가 0건입니다. gdc_get_field_values로 필드 값 철자(대소문자 포함)를 확인하세요. "
                          "예: experimental_strategy='WGS', data_format='BAM', access='open'.")
    if summary["truncated"]:
        result["warning"] = f"max_files={max_files} 제한으로 {total}건 중 {len(rows)}건만 가져왔습니다."
    return result


def search_cases(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    project: Any = None,
    primary_site: Any = None,
    disease_type: Any = None,
    sample_type: Any = None,
    data_category: Any = None,
    data_type: Any = None,
    data_format: Any = None,
    experimental_strategy: Any = None,
    workflow_type: Any = None,
    access: Any = None,
    max_cases: int = 20000,
    page_size: int = 1000,
) -> dict[str, Any]:
    criteria = {
        "project": project,
        "primary_site": primary_site,
        "disease_type": disease_type,
        "sample_type": sample_type,
        "data_category": data_category,
        "data_type": data_type,
        "data_format": data_format,
        "experimental_strategy": experimental_strategy,
        "workflow_type": workflow_type,
        "access": access,
    }
    criteria = {k: v for k, v in criteria.items() if as_list(v)}
    filters = build_filters("cases", criteria)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    hits, total = _paginate(client, log, "cases", filters, CASE_FIELDS, max_cases, page_size, retries)
    rows = [
        {
            "case_id": h.get("case_id") or h.get("id"),
            "submitter_id": h.get("submitter_id"),
            "project_id": (h.get("project") or {}).get("project_id"),
            "primary_site": h.get("primary_site"),
            "disease_type": h.get("disease_type"),
        }
        for h in hits
    ]
    query_id = new_query_id("gdc")
    cases_path = None
    if rows:
        cases_path = write_tsv(cfg.manifests_dir / f"{query_id}.cases.tsv", rows,
                               ["case_id", "submitter_id", "project_id", "primary_site", "disease_type"])
    summary = {
        "n_cases": total,
        "n_retrieved": len(rows),
        "truncated": total > len(rows),
        "by_project": counts(r["project_id"] for r in rows),
        "by_primary_site": counts(r["primary_site"] for r in rows),
        "by_disease_type": counts(r["disease_type"] for r in rows),
    }
    params = {"endpoint": "cases", "criteria": criteria, "filters": filters}
    db.save_query(query_id, "gdc", params, log.calls, summary, None, None)
    return {
        "query_id": query_id,
        "summary": summary,
        "cases_path": str(cases_path) if cases_path else None,
        "preview": rows[:10],
        "api_queries": log.calls,
        "note": "케이스 검색은 manifest를 만들지 않습니다. 다운로드하려면 gdc_search_files를 사용하세요.",
    }


def get_field_values(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    field: str | None = None,
    endpoint: str = "files",
    search: str | None = None,
    filters_criteria: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if endpoint not in ("files", "cases", "projects"):
        raise ValueError("endpoint는 files, cases, projects 중 하나여야 합니다.")
    if endpoint == "projects" and filters_criteria:
        raise ValueError("projects 엔드포인트에는 filters를 지원하지 않습니다. files 또는 cases를 사용하세요.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    query_id = new_query_id("gdc")
    if field:
        idx = 0 if endpoint == "files" else 1
        real_field = FILTER_FIELDS[field][idx] if field in FILTER_FIELDS and endpoint != "projects" else field
        params: dict[str, Any] = {"facets": real_field, "size": 0}
        filters = build_filters(endpoint, filters_criteria or {}) if filters_criteria else None
        if filters:
            params["filters"] = json.dumps(filters)
        resp = http.request(client, log, "GET", f"{GDC_API}/{endpoint}", params=params, retries=retries)
        aggs = resp.json().get("data", {}).get("aggregations", {})
        buckets = (aggs.get(real_field) or {}).get("buckets", [])
        values = sorted(({"value": b.get("key"), "count": b.get("doc_count")} for b in buckets),
                        key=lambda x: -(x["count"] or 0))
        summary = {"field": real_field, "endpoint": endpoint, "n_values": len(values)}
        db.save_query(query_id, "gdc", {"field": real_field, "endpoint": endpoint, "filters": filters},
                      log.calls, summary, None, None)
        return {"query_id": query_id, "field": real_field, "values": values[:limit], "api_queries": log.calls}

    resp = http.request(client, log, "GET", f"{GDC_API}/{endpoint}/_mapping", retries=retries)
    fields = resp.json().get("fields", [])
    if search:
        fields = [f for f in fields if search.lower() in f.lower()]
    summary = {"endpoint": endpoint, "n_fields": len(fields), "search": search}
    db.save_query(query_id, "gdc", {"mapping": endpoint, "search": search}, log.calls, summary, None, None)
    return {
        "query_id": query_id,
        "endpoint": endpoint,
        "fields": fields[:limit],
        "n_fields": len(fields),
        "logical_filters": {k: v[0 if endpoint == "files" else 1] for k, v in FILTER_FIELDS.items()},
        "api_queries": log.calls,
    }
