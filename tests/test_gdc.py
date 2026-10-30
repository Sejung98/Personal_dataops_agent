import json

import httpx
import pytest
import respx

from dataops_mcp import gdc, http
from dataops_mcp.util import read_tsv


def file_hit(fid, size, case, access="open", sample_type="Primary Tumor"):
    return {
        "file_id": fid,
        "file_name": f"{fid}.bam",
        "file_size": size,
        "md5sum": "0" * 32,
        "state": "released",
        "access": access,
        "data_format": "BAM",
        "data_type": "Aligned Reads",
        "data_category": "Sequencing Reads",
        "experimental_strategy": "WGS",
        "analysis": {"workflow_type": "BWA with Mark Duplicates and BQSR"},
        "cases": [{"case_id": case, "submitter_id": f"TCGA-{case}", "project": {"project_id": "TCGA-BRCA"},
                   "samples": [{"submitter_id": f"TCGA-{case}-01A", "sample_type": sample_type}]}],
    }


def test_build_filters_maps_logical_names_per_endpoint():
    f = gdc.build_filters("files", {"project": "TCGA-BRCA", "access": ["open"], "data_type": None})
    assert f == {"op": "and", "content": [
        {"op": "in", "content": {"field": "cases.project.project_id", "value": ["TCGA-BRCA"]}},
        {"op": "in", "content": {"field": "access", "value": ["open"]}},
    ]}
    c = gdc.build_filters("cases", {"project": "TCGA-BRCA", "experimental_strategy": "WGS"})
    fields = [x["content"]["field"] for x in c["content"]]
    assert fields == ["project.project_id", "files.experimental_strategy"]
    assert gdc.build_filters("files", {}) is None
    with pytest.raises(ValueError):
        gdc.build_filters("files", {"bogus": "x"})


@respx.mock
def test_search_files_paginates_summarises_and_writes_manifest(cfg, db, client):
    pages = [
        {"data": {"hits": [file_hit("f1", 100, "c1"), file_hit("f2", 200, "c1", sample_type="Blood Derived Normal")],
                  "pagination": {"total": 3}}},
        {"data": {"hits": [file_hit("f3", 300, "c2")], "pagination": {"total": 3}}},
    ]
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(bodies) - 1])

    respx.post(f"{gdc.GDC_API}/files").mock(side_effect=responder)
    out = gdc.search_files(cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS",
                           data_format="BAM", access="open", page_size=2)

    s = out["summary"]
    assert s["n_files"] == 3 and s["api_total"] == 3 and not s["truncated"]
    assert s["total_bytes"] == 600 and s["n_cases"] == 2
    assert s["by_access"] == {"open": 3}
    assert out["access"] == "open"
    # 요청 본문에 필터와 페이지네이션이 들어갔는지
    assert bodies[0]["from"] == 0 and bodies[1]["from"] == 2
    fields = [c["content"]["field"] for c in bodies[0]["filters"]["content"]]
    assert "cases.project.project_id" in fields and "experimental_strategy" in fields
    # 근거 API 호출 기록
    assert len(out["api_queries"]) == 2 and out["api_queries"][0]["method"] == "POST"
    # gdc-client manifest 형식
    rows = read_tsv(out["manifest_path"])
    assert list(rows[0].keys()) == ["id", "filename", "md5", "size", "state"]
    assert [r["id"] for r in rows] == ["f1", "f2", "f3"]
    saved = db.get_query(out["query_id"])
    assert saved["summary"]["total_bytes"] == 600 and saved["manifest_path"] == out["manifest_path"]


@respx.mock
def test_search_files_controlled_and_zero_hits(cfg, db, client):
    respx.post(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(
        200, json={"data": {"hits": [file_hit("f1", 5, "c1", access="controlled")], "pagination": {"total": 1}}}))
    out = gdc.search_files(cfg, db, client, project="TCGA-BRCA")
    assert out["access"] == "controlled"

    respx.post(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(
        200, json={"data": {"hits": [], "pagination": {"total": 0}}}))
    out = gdc.search_files(cfg, db, client, project="TCGA-XXXX")
    assert out["summary"]["n_files"] == 0 and out["manifest_path"] is None and "hint" in out


@respx.mock
def test_get_field_values_uses_facets(cfg, db, client):
    route = respx.get(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(200, json={
        "data": {"hits": [], "aggregations": {"experimental_strategy": {"buckets": [
            {"key": "WXS", "doc_count": 10}, {"key": "WGS", "doc_count": 50}]}}}}))
    out = gdc.get_field_values(cfg, db, client, field="experimental_strategy",
                               filters_criteria={"project": "TCGA-BRCA"})
    assert out["values"][0] == {"value": "WGS", "count": 50}
    params = route.calls[0].request.url.params
    assert params["facets"] == "experimental_strategy" and params["size"] == "0"
    assert json.loads(params["filters"])["content"][0]["content"]["field"] == "cases.project.project_id"


@respx.mock
def test_get_field_values_mapping_lists_fields(cfg, db, client):
    respx.get(f"{gdc.GDC_API}/files/_mapping").mock(return_value=httpx.Response(200, json={
        "_mapping": {}, "fields": ["access", "data_type", "cases.project.project_id"], "defaults": []}))
    out = gdc.get_field_values(cfg, db, client, search="project")
    assert out["fields"] == ["cases.project.project_id"]


@respx.mock
def test_search_cases(cfg, db, client):
    respx.post(f"{gdc.GDC_API}/cases").mock(return_value=httpx.Response(200, json={"data": {
        "hits": [{"case_id": "c1", "submitter_id": "TCGA-A", "project": {"project_id": "TCGA-BRCA"},
                  "primary_site": "Breast", "disease_type": "Ductal and Lobular Neoplasms"}],
        "pagination": {"total": 1}}}))
    out = gdc.search_cases(cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS")
    assert out["summary"]["n_cases"] == 1 and out["summary"]["by_primary_site"] == {"Breast": 1}


@respx.mock
def test_http_retries_on_503(client):
    route = respx.get("https://example.org/x").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
    log = http.ApiLog()
    resp = http.request(client, log, "GET", "https://example.org/x", retries=2, sleep=lambda s: None)
    assert resp.status_code == 200 and route.call_count == 2 and len(log.calls) == 2


@respx.mock
def test_http_raises_on_client_error(client):
    respx.get("https://example.org/x").mock(return_value=httpx.Response(400, text="bad filter"))
    with pytest.raises(http.ApiError, match="400"):
        http.request(client, http.ApiLog(), "GET", "https://example.org/x", retries=2, sleep=lambda s: None)
