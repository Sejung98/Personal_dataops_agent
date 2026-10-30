"""ENCODE, CELLxGENE, cBioPortal. 응답 형태는 각 프로젝트 공식 소스(스키마·OpenAPI)에서 확인한 필드명을 따른다."""

import httpx
import pytest
import respx

from dataops_mcp import cbioportal, cellxgene, encode, transfer
from dataops_mcp.util import read_tsv


# ---------------------------------------------------------------- ENCODE
@respx.mock
def test_encode_search_and_files(cfg, db, client):
    def search(request):
        p = request.url.params
        assert p["format"] == "json"
        if p["type"] == "Experiment":
            assert p["assay_title"] == "ATAC-seq" and "accession" in p.get_list("field")
            return httpx.Response(200, json={"total": 1, "@graph": [{
                "accession": "ENCSR356KRQ", "assay_title": "ATAC-seq", "status": "released",
                "biosample_ontology": {"term_name": "keratinocyte"}, "assembly": ["GRCh38", "hg19"],
                "replicates": [{"library": {"biosample": {"donor": {"organism": {"scientific_name": "Homo sapiens"}}}}}],
            }]})
        assert p["dataset"] == "/experiments/ENCSR356KRQ/" and p["assembly"] == "GRCh38"
        return httpx.Response(200, json={"total": 3, "@graph": [
            {"accession": "ENCFF001AAA", "file_format": "bed", "output_type": "conservative IDR thresholded peaks",
             "assembly": "GRCh38", "file_size": 1234, "md5sum": "a" * 32,
             "href": "/files/ENCFF001AAA/@@download/ENCFF001AAA.bed.gz", "status": "released"},
            {"accession": "ENCFF002BBB", "file_format": "bigWig", "output_type": "fold change over control",
             "assembly": "GRCh38", "file_size": 99, "md5sum": "b" * 32,
             "href": "/files/ENCFF002BBB/@@download/ENCFF002BBB.bigWig", "status": "released"},
            {"accession": "ENCFF003CCC", "file_format": "fastq", "restricted": True, "href": "/x", "status": "released"},
        ]})

    respx.get(f"{encode.ENCODE}/search/").mock(side_effect=search)
    found = encode.search(cfg, db, client, assay_title="ATAC-seq", organism="Homo sapiens")
    assert found["summary"]["n_experiments"] == 1 and found["preview"][0]["biosample"] == "keratinocyte"
    assert found["preview"][0]["organism"] == "Homo sapiens"

    out = encode.files(cfg, db, client, from_query_id=found["query_id"], assembly="GRCh38")
    s = out["summary"]
    assert s["n_files"] == 2 and s["total_bytes"] == 1333 and s["skipped_restricted_or_unavailable"] == 1
    rows = read_tsv(out["manifest_path"])
    assert rows[0]["url"] == "https://www.encodeproject.org/files/ENCFF001AAA/@@download/ENCFF001AAA.bed.gz"
    assert rows[0]["checksum"] == "md5:" + "a" * 32 and rows[0]["subdir"] == "ENCSR356KRQ"
    assert "md5:" in transfer.prepare_download(cfg, db, out["query_id"])["script_text"]


@respx.mock
def test_encode_404_means_no_results(cfg, db, client):
    respx.get(f"{encode.ENCODE}/search/").mock(return_value=httpx.Response(404, json={"notification": "No results found"}))
    out = encode.search(cfg, db, client, assay_title="nonexistent assay")
    assert out["summary"]["n_experiments"] == 0 and out["experiments_path"] is None


def test_encode_files_validates_accessions(cfg, db, client):
    with pytest.raises(ValueError):
        encode.files(cfg, db, client, experiments=["GSE1"])
    with pytest.raises(ValueError):
        encode.files(cfg, db, client)


# ---------------------------------------------------------------- CELLxGENE
def _ds(did, tissue, cells, size=1000, assets=True):
    d = {"dataset_id": did, "dataset_version_id": f"v-{did}", "title": f"{tissue} atlas",
         "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
         "tissue": [{"label": tissue, "ontology_term_id": "UBERON:x"}],
         "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
         "assay": [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}], "cell_count": cells}
    if assets:
        d["assets"] = [{"filetype": "H5AD", "url": f"https://datasets.cellxgene.cziscience.com/v-{did}.h5ad", "filesize": size},
                       {"filetype": "RDS", "url": f"https://datasets.cellxgene.cziscience.com/v-{did}.rds", "filesize": -1}]
    return d


@respx.mock
def test_cellxgene_search_filters_and_manifest(cfg, db, client):
    respx.get(f"{cellxgene.CXG_API}/collections").mock(return_value=httpx.Response(200, json=[
        {"collection_id": "c1", "name": "Lung atlas", "datasets": [_ds("d1", "upper lobe of left lung", 0, assets=False)]},
        {"collection_id": "c2", "name": "Liver", "datasets": [_ds("d2", "liver", 0, assets=False)]},
    ]))
    respx.get(f"{cellxgene.CXG_API}/collections/c1").mock(return_value=httpx.Response(200, json={
        "collection_id": "c1", "name": "Lung atlas", "datasets": [_ds("d1", "upper lobe of left lung", 5000, 2048)]}))
    out = cellxgene.search(cfg, db, client, tissue="lung")
    s = out["summary"]
    assert s["n_datasets"] == 1 and s["total_cells"] == 5000 and s["total_bytes"] == 2048
    rows = read_tsv(out["manifest_path"])
    assert len(rows) == 1 and rows[0]["filename"] == "v-d1.h5ad" and rows[0]["subdir"] == "c1"
    meta = read_tsv(out["datasets_path"])
    assert meta[0]["files"] == "v-d1.h5ad"

    with_rds = cellxgene.search(cfg, db, client, collection_id="c1", asset_types=["H5AD", "RDS"])
    assert with_rds["summary"]["n_files"] == 2 and with_rds["summary"]["n_files_with_size"] == 1  # -1 = 미상
    with pytest.raises(ValueError):
        cellxgene.search(cfg, db, client, asset_types=["LOOM"])


@respx.mock
def test_cellxgene_tombstoned_collection_is_skipped(cfg, db, client):
    respx.get(f"{cellxgene.CXG_API}/collections/gone").mock(return_value=httpx.Response(410))
    out = cellxgene.search(cfg, db, client, collection_id="gone")
    assert out["summary"]["n_datasets"] == 0 and any("철회" in n for n in out["notes"])


# ---------------------------------------------------------------- cBioPortal
@respx.mock
def test_cbioportal_search_builds_datahub_manifest(cfg, db, client):
    route = respx.get(f"{cbioportal.CBIO_API}/studies").mock(return_value=httpx.Response(200, json=[
        {"studyId": "brca_tcga_pan_can_atlas_2018", "name": "Breast Invasive Carcinoma (TCGA, PanCancer Atlas)",
         "cancerType": {"name": "Invasive Breast Carcinoma"}, "allSampleCount": 1084, "referenceGenome": "hg19",
         "pmid": "29625048", "readPermission": True},
        {"studyId": "brca_new", "name": "BRCA new", "cancerTypeId": "brca", "allSampleCount": 10,
         "referenceGenome": "hg38", "readPermission": True},
        {"studyId": "locked", "name": "x", "readPermission": False},
    ]))
    respx.get(f"{cbioportal.DATAHUB}/study_list.json").mock(
        return_value=httpx.Response(200, json=["brca_tcga_pan_can_atlas_2018"]))
    respx.head(f"{cbioportal.DATAHUB}/brca_tcga_pan_can_atlas_2018.tar.gz").mock(
        return_value=httpx.Response(200, headers={"content-length": "123456"}))
    out = cbioportal.search(cfg, db, client, keyword="breast")
    s = out["summary"]
    assert route.calls[0].request.url.params["projection"] == "DETAILED"
    assert s["n_studies"] == 2 and s["total_samples"] == 1094 and s["n_files"] == 1 and s["total_bytes"] == 123456
    assert s["not_on_datahub"] == ["brca_new"] and "hg19" in out["warning"]
    assert read_tsv(out["manifest_path"])[0]["filename"] == "brca_tcga_pan_can_atlas_2018.tar.gz"
    with pytest.raises(ValueError):
        cbioportal.search(cfg, db, client)
