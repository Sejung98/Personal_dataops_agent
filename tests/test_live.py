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
