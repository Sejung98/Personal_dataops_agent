"""실제 API 스모크 테스트. 기본 실행에서는 제외되며 `pytest -m live`로 실행한다.

테스트 accession은 nf-core/fetchngs 테스트 목록(SRR390278, GSE18729)과
EGA 공식 문서 예시(EGAD00001000645)에서 가져왔다.
"""

import pytest

from dataops_mcp import ega, gdc, sra
from dataops_mcp.http import make_client

pytestmark = pytest.mark.live


@pytest.fixture
def live_client(cfg):
    with make_client(cfg) as c:
        yield c


def test_gdc_status(live_client):
    r = live_client.get(f"{gdc.GDC_API}/status")
    assert r.status_code == 200


def test_gdc_open_wgs_bam_tcga_brca(cfg, db, live_client):
    out = gdc.search_files(cfg, db, live_client, project="TCGA-BRCA", experimental_strategy="WGS",
                           data_format="BAM", access="open")
    print(out["summary"])
    assert out["summary"]["api_total"] == out["summary"]["n_files"]


def test_gdc_field_values(cfg, db, live_client):
    out = gdc.get_field_values(cfg, db, live_client, field="experimental_strategy",
                               filters_criteria={"project": "TCGA-BRCA"})
    assert any(v["value"] == "RNA-Seq" for v in out["values"])


def test_ena_single_run(cfg, db, live_client):
    out = sra.search(cfg, db, live_client, accession="SRR390278")
    assert out["summary"]["n_runs"] == 1


def test_geo_series(cfg, db, live_client):
    out = sra.search(cfg, db, live_client, accession="GSE18729")
    assert out["summary"]["n_runs"] > 0


def test_ega_dataset(cfg, db, live_client):
    out = ega.search(cfg, db, live_client, accession="EGAD00001000645")
    print(out["summary"], out["preview"][:2])
    assert out["summary"]["n_datasets"] == 1


# ---- 추가 저장소 (accession은 각 공식 클라이언트/문서의 예시) -------------------------------
def test_geo_supplementary_files(cfg, db, live_client):
    from dataops_mcp import geo

    out = geo.files(cfg, db, live_client, accession="GSE137303")  # GEOquery·fetch-series 예시
    print(out["summary"], out["preview"])
    assert out["summary"]["n_files"] >= 1


def test_arrayexpress_files(cfg, db, live_client):
    from dataops_mcp import biostudies

    out = biostudies.files(cfg, db, live_client, accession="E-MTAB-9697")
    print(out["summary"])
    assert out["summary"]["n_files"] >= 2 and "ERP124622" in out["summary"]["ena_accessions"]


def test_pride_files(cfg, db, live_client):
    from dataops_mcp import pride

    out = pride.files(cfg, db, live_client, accession="PXD008644")  # pridepy 테스트 프로젝트
    print(out["summary"])
    assert out["summary"]["n_files"] >= 1


def test_zenodo_files(cfg, db, live_client):
    from dataops_mcp import zenodo

    out = zenodo.files(cfg, db, live_client, record="3490058")
    print(out["summary"], out["preview"])
    assert out["summary"]["n_files_with_checksum"] >= 1


def test_encode_experiment_files(cfg, db, live_client):
    from dataops_mcp import encode

    out = encode.files(cfg, db, live_client, experiments=["ENCSR356KRQ"], assembly="GRCh38")  # ENCODE ATAC 파이프라인 테스트셋
    print(out["summary"])
    assert out["summary"]["n_files"] >= 1


def test_cellxgene_collection(cfg, db, live_client):
    from dataops_mcp import cellxgene

    out = cellxgene.search(cfg, db, live_client, collection_id="e5f58829-1a66-40b5-a624-9046778e74f5")  # Tabula Sapiens
    print(out["summary"])
    assert out["summary"]["n_datasets"] >= 1


def test_cbioportal_study(cfg, db, live_client):
    from dataops_mcp import cbioportal

    out = cbioportal.search(cfg, db, live_client, study_ids=["acbc_mskcc_2015"])
    print(out["summary"])
    assert out["summary"]["n_studies"] == 1


def test_sra_dbgap_flag(cfg, db, live_client):
    out = sra.search(cfg, db, live_client, accession="SRR27651451")  # phs002857 (metadata는 공개)
    print(out["summary"])
    assert out["summary"]["n_runs"] == 1
