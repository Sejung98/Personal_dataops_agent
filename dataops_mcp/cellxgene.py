"""CZ CELLxGENE Discover 단일세포 데이터셋 discovery.

근거 (2026-10-30 확인, chanzuckerberg/single-cell-data-portal backend/curation/api/curation-api.yml):
- base: https://api.cellxgene.cziscience.com/curation/v1  (공개 GET은 인증 없음, 페이지네이션 없음)
- GET /collections               : 공개 collection 목록. datasets는 미리보기(assets 없음):
                                   dataset_id, assay, disease, organism, tissue (각각 [{label, ontology_term_id}])
- GET /collections/{collection_id}: datasets 전체 메타데이터 + assets[{filetype, url, filesize}]
  filetype: H5AD, RDS(2024년 말 이후 신규 생성 중단), ATAC_FRAGMENT, ATAC_INDEX. filesize -1 = 미상. 체크섬 없음.
- 410 = 철회(tombstoned)된 collection
- asset URL은 dataset_version_id 기준(불변 버전) -> 재현성을 위해 그 URL을 그대로 쓴다.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB
from .util import counts, write_tsv

CXG_API = "https://api.cellxgene.cziscience.com/curation/v1"
ASSET_TYPES = ("H5AD", "RDS", "ATAC_FRAGMENT", "ATAC_INDEX")
DATASET_COLUMNS = ["dataset_id", "dataset_version_id", "title", "collection_id", "collection_name", "organism",
                   "tissue", "disease", "assay", "cell_count", "files"]


def _labels(items: Any) -> list[str]:
    return [str(i.get("label")) for i in items or [] if isinstance(i, dict) and i.get("label")]


def _match(values: list[str], wanted: list[str]) -> bool:
    if not wanted:
        return True
    low = [v.lower() for v in values]
    return any(w.lower() in v for w in wanted for v in low)


def _as_list(v: Any) -> list[str]:
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    organism: Any = None,
    tissue: Any = None,
    disease: Any = None,
    assay: Any = None,
    text: str | None = None,
    collection_id: str | None = None,
    asset_types: list[str] | None = None,
    max_collections: int = 30,
) -> dict[str, Any]:
    types = [t.upper() for t in (asset_types or ["H5AD"])]
    if set(types) - set(ASSET_TYPES):
        raise ValueError(f"asset_types는 {ASSET_TYPES} 중에서 고르세요.")
    crit = {"organism": _as_list(organism), "tissue": _as_list(tissue), "disease": _as_list(disease),
            "assay": _as_list(assay)}
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    notes: list[str] = []

    if collection_id:
        coll_ids = [collection_id]
    else:
        listing = http.request(client, log, "GET", f"{CXG_API}/collections", retries=retries).json()
        coll_ids = []
        for c in listing:
            if text and text.lower() not in f"{c.get('name', '')} {c.get('description', '')}".lower():
                continue
            if any(all(_match(_labels(d.get(k)), v) for k, v in crit.items()) for d in c.get("datasets", [])):
                coll_ids.append(c["collection_id"])
        if len(coll_ids) > max_collections:
            notes.append(f"조건에 맞는 collection {len(coll_ids)}개 중 {max_collections}개만 상세 조회했습니다. 조건을 좁히세요.")
            coll_ids = coll_ids[:max_collections]

    rows, datasets = [], []
    for cid in coll_ids:
        try:
            coll = http.request(client, log, "GET", f"{CXG_API}/collections/{cid}", retries=retries).json()
        except http.ApiError as exc:
            if "HTTP 410" in str(exc) or "HTTP 404" in str(exc):
                notes.append(f"collection {cid}: 철회되었거나 없음")
                continue
            raise
        for d in coll.get("datasets", []):
            if not all(_match(_labels(d.get(k)), v) for k, v in crit.items()):
                continue
            files = []
            for a in d.get("assets", []):
                if str(a.get("filetype", "")).upper() not in types or not a.get("url"):
                    continue
                size = a.get("filesize")
                row = urlfiles.make_row(a["url"], subdir=cid, size=size if isinstance(size, int) and size > 0 else None)
                rows.append(row)
                files.append(row["filename"])
            datasets.append({
                "dataset_id": d.get("dataset_id"),
                "dataset_version_id": d.get("dataset_version_id"),
                "title": d.get("title"),
                "collection_id": cid,
                "collection_name": coll.get("name"),
                "organism": ",".join(_labels(d.get("organism"))),
                "tissue": ",".join(_labels(d.get("tissue"))),
                "disease": ",".join(_labels(d.get("disease"))),
                "assay": ",".join(_labels(d.get("assay"))),
                "cell_count": d.get("cell_count"),
                "files": ",".join(files),
            })
    extra = {
        "n_datasets": len(datasets),
        "n_collections": len({d["collection_id"] for d in datasets}),
        "total_cells": sum(int(d["cell_count"] or 0) for d in datasets),
        "by_organism": counts(d["organism"] for d in datasets),
        "by_assay": counts(d["assay"] for d in datasets),
        "by_disease": dict(list(counts(d["disease"] for d in datasets).items())[:15]),
        "asset_types": types,
        "checksums": "CELLxGENE API는 체크섬을 제공하지 않아 크기로만 검증합니다.",
    }
    params = {"criteria": crit, "text": text, "collection_id": collection_id, "asset_types": types}
    out = urlfiles.save_url_query(cfg, db, "cellxgene", rows, params, log.calls, extra)
    if datasets:
        meta_path = write_tsv(cfg.manifests_dir / f"{out['query_id']}.datasets.tsv", datasets, DATASET_COLUMNS)
        out["datasets_path"] = str(meta_path)
        out["datasets_preview"] = datasets[:8]
    out["notes"] = notes + ["파일 이름은 dataset_version_id(불변 버전)입니다. datasets_path 표로 데이터셋 제목과 연결하세요."]
    if "RDS" in types:
        out["notes"].append("RDS(Seurat)는 2024년 말 이후 새 버전에서 생성되지 않아 없는 데이터셋이 많습니다.")
    return out
