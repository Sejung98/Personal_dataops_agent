import httpx
import pytest
import respx

from dataops_mcp import ega, http, sra
from dataops_mcp.util import read_tsv

HEADER = "\t".join(sra.ENA_FIELDS)


def ena_row(run, layout="PAIRED", strategy="RNA-Seq", fastq=True, sample="SAMN1"):
    vals = {f: "" for f in sra.ENA_FIELDS}
    vals.update(run_accession=run, experiment_accession="SRX" + run[3:], sample_accession=sample,
                study_accession="PRJNA100", secondary_study_accession="SRP100", library_layout=layout,
                library_strategy=strategy, read_count="1000", base_count="150000", scientific_name="Homo sapiens",
                instrument_model="Illumina NovaSeq 6000")
    if fastq:
        if layout == "PAIRED":
            vals.update(fastq_ftp=f"ftp.sra.ebi.ac.uk/{run}_1.fastq.gz;ftp.sra.ebi.ac.uk/{run}_2.fastq.gz",
                        fastq_bytes="100;150", fastq_md5="a;b")
        else:
            vals.update(fastq_ftp=f"ftp.sra.ebi.ac.uk/{run}.fastq.gz", fastq_bytes="80", fastq_md5="c")
    return "\t".join(vals[f] for f in sra.ENA_FIELDS)


def tsv(*rows):
    return HEADER + "\n" + "\n".join(rows) + "\n"


@respx.mock
def test_sra_search_insdc_summary_and_filter(cfg, db, client):
    route = respx.get(sra.ENA_FILEREPORT).mock(return_value=httpx.Response(200, text=tsv(
        ena_row("SRR1"), ena_row("SRR2", sample="SAMN2"), ena_row("SRR3", layout="SINGLE", strategy="WGS"))))
    out = sra.search(cfg, db, client, accession="PRJNA100")
    s = out["summary"]
    assert s["n_runs"] == 3 and s["n_samples"] == 2
    assert s["by_library_layout"] == {"PAIRED": 2, "SINGLE": 1}
    assert s["total_bytes"] == 250 + 250 + 80
    assert route.calls[0].request.url.params["result"] == "read_run"
    assert "fastq_md5" in route.calls[0].request.url.params["fields"]
    assert len(read_tsv(out["runinfo_path"])) == 3

    only_rna = sra.search(cfg, db, client, accession="PRJNA100", library_strategy="rna-seq")
    assert only_rna["summary"]["n_runs"] == 2


@respx.mock
def test_sra_search_resolves_geo_series(cfg, db, client):
    def esearch(request):
        db_ = request.url.params["db"]
        if db_ == "gds":
            return httpx.Response(200, json={"esearchresult": {"idlist": ["200018729"]}})
        return httpx.Response(200, json={"esearchresult": {"idlist": ["11", "12"]}})

    respx.get(f"{sra.EUTILS}/esearch.fcgi").mock(side_effect=esearch)
    respx.get(f"{sra.EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(200, json={"result": {
        "uids": ["200018729"],
        "200018729": {"accession": "GSE18729", "samples": [{"accession": "GSM1"}, {"accession": "GSM2"}]}}}))
    respx.get(f"{sra.EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=(
        "Run,Experiment,SRAStudy,BioProject\nSRR1,SRX1,SRP100,PRJNA100\nSRR2,SRX2,SRP100,PRJNA100\n")))
    respx.get(sra.ENA_FILEREPORT).mock(return_value=httpx.Response(200, text=tsv(
        ena_row("SRR1"), ena_row("SRR2"), ena_row("SRR9"))))
    out = sra.search(cfg, db, client, accession="GSE18729")
    assert out["summary"]["n_runs"] == 2  # SRR9는 GSE에 속하지 않으므로 제외
    assert any("GSE18729" in n for n in out["notes"])


@respx.mock
def test_sra_search_falls_back_to_ncbi_when_ena_empty(cfg, db, client):
    respx.get(sra.ENA_FILEREPORT).mock(return_value=httpx.Response(200, text=HEADER + "\n"))
    respx.get(f"{sra.EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}}))
    respx.get(f"{sra.EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=(
        "Run,Experiment,LibraryLayout,LibraryStrategy,ScientificName,spots,bases\n"
        "SRR5,SRX5,PAIRED,RNA-Seq,Homo sapiens,10,1000\n")))
    out = sra.search(cfg, db, client, accession="SRP555")
    assert out["summary"]["n_runs"] == 1 and out["summary"]["runs_without_fastq"] == 1
    assert "warning" in out


def test_sra_classify_rejects_unknown():
    with pytest.raises(ValueError):
        sra.classify("ABC123")
    assert sra.classify("srr390278") == "insdc" and sra.classify("GSM465244") == "geo"


@respx.mock
def test_ega_dataset_files(cfg, db, client):
    acc = "EGAD00001000645"
    respx.get(f"{ega.EGA_METADATA_API}/datasets/{acc}").mock(return_value=httpx.Response(200, json={
        "accession_id": acc, "title": "test", "num_samples": 2, "access_type": "controlled"}))
    respx.get(f"{ega.EGA_METADATA_API}/datasets/{acc}/files").mock(return_value=httpx.Response(200, json=[
        {"accession_id": "EGAF00000000001", "filename": "a.bam.cip", "size": 1000, "unencrypted_checksum": "x"},
        {"accession_id": "EGAF00000000002", "file_name": "b.bam", "file_size": 2000},
    ]))
    out = ega.search(cfg, db, client, accession=acc)
    s = out["summary"]
    assert out["access"] == "controlled"
    assert s["n_files"] == 2 and s["total_bytes"] == 3000 and s["datasets"][0]["num_samples"] == 2
    rows = read_tsv(out["manifest_path"])
    assert rows[1]["filename"] == "b.bam"


@respx.mock
def test_ega_pagination_stops_when_api_ignores_offset(cfg, db, client):
    acc = "EGAD00001000001"
    respx.get(f"{ega.EGA_METADATA_API}/datasets/{acc}").mock(return_value=httpx.Response(200, json={}))
    same_page = [{"accession_id": f"EGAF{i:011d}", "filename": f"f{i}"} for i in range(3)]
    route = respx.get(f"{ega.EGA_METADATA_API}/datasets/{acc}/files").mock(
        return_value=httpx.Response(200, json=same_page))
    items = ega._list_paginated(client, http.ApiLog(), f"/datasets/{acc}/files", retries=0, limit=3)
    assert len(items) == 3 and route.call_count == 2


def test_ega_rejects_bad_accession(cfg, db, client):
    with pytest.raises(ValueError):
        ega.search(cfg, db, client, accession="EGAX123")


@respx.mock
def test_sra_search_flags_dbgap_protected_runs(cfg, db, client):
    respx.get(sra.ENA_FILEREPORT).mock(return_value=httpx.Response(204))
    respx.get(f"{sra.EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["1", "2"]}}))
    respx.get(f"{sra.EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=(
        "Run,Experiment,LibraryLayout,LibraryStrategy,ScientificName,spots,bases,dbgap_study_accession,Consent\n"
        "SRR27651451,SRX1,PAIRED,RNA-Seq,Homo sapiens,10,1000,phs002857,GRU\n"
        "SRR2,SRX2,PAIRED,RNA-Seq,Homo sapiens,10,1000,,public\n")))
    out = sra.search(cfg, db, client, accession="SRP999")
    s = out["summary"]
    assert s["dbgap_protected_runs"] == 1 and s["dbgap_studies"] == ["phs002857"]
    assert out["access"] == "controlled" and "use_dbgap=True" in out["dbgap"]
