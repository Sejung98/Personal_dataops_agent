"""GEO 보조파일, ArrayExpress, PRIDE, Zenodo. 응답 형태는 실제 공개 API 응답 캡처(tests/fixtures)를 쓴다."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from dataops_mcp import biostudies, geo, pride, transfer, zenodo
from dataops_mcp.util import read_tsv

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- GEO
def test_geo_stub_and_index_parsing():
    assert geo.stub("GSE1234") == "GSE1nnn" and geo.stub("GSE123") == "GSEnnn" and geo.stub("GSE12") == "GSEnnn"
    assert geo.series_dir("GSM1551552").endswith("/samples/GSM1551nnn/GSM1551552")
    names = geo.parse_index(fixture("gse5350_suppl.html"))
    assert names[0] == "GSE5350_MAQC_ABI_123_60TXTs.zip" and all(n.startswith("GSE5350") for n in names)


@respx.mock
def test_geo_files_lists_suppl_and_matrix(cfg, db, client):
    summ = json.loads(fixture("gse305638.json"))
    respx.get(f"{geo.EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["200305638"]}}))
    respx.get(f"{geo.EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(200, json=summ))
    base = geo.series_dir("GSE305638")
    respx.get(f"{base}/suppl/").mock(return_value=httpx.Response(200, text=(
        '<a href="/geo/series/GSE305nnn/GSE305638/">Parent Directory</a>'
        '<a href="GSE305638_counts.tsv.gz">x</a><a href="GSE305638_RAW.tar">x</a><a href="filelist.txt">x</a>')))
    respx.get(f"{base}/matrix/").mock(return_value=httpx.Response(200, text=(
        '<a href="GSE305638_series_matrix.txt.gz">x</a>')))
    respx.head(url__startswith=base).mock(return_value=httpx.Response(200, headers={"content-length": "1000"}))

    out = geo.files(cfg, db, client, accession="GSE305638", exclude=["*_RAW.tar"])
    s = out["summary"]
    assert s["n_files"] == 2 and s["total_bytes"] == 2000 and s["n_files_with_checksum"] == 0
    assert s["taxon"] == "Homo sapiens" and s["bioproject"] == "PRJNA1307399"
    rows = read_tsv(out["manifest_path"])
    assert {r["subdir"] for r in rows} == {"suppl", "matrix"}
    assert any("sra_search" in n for n in out["notes"])


@respx.mock
def test_geo_missing_matrix_dir_is_tolerated(cfg, db, client):
    summ = json.loads(fixture("gse305638.json"))
    respx.get(f"{geo.EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["200305638"]}}))
    respx.get(f"{geo.EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(200, json=summ))
    base = geo.series_dir("GSE305638")
    respx.get(f"{base}/suppl/").mock(return_value=httpx.Response(404))
    respx.get(f"{base}/matrix/").mock(return_value=httpx.Response(404))
    out = geo.files(cfg, db, client, accession="GSE305638")
    assert out["summary"]["n_files"] == 0 and "hint" in out


# ---------------------------------------------------------------- ArrayExpress / BioStudies
@respx.mock
def test_arrayexpress_files_from_study_json(cfg, db, client):
    acc = "E-MTAB-9697"
    link = "https://ftp.ebi.ac.uk/biostudies/fire/E-MTAB-/697/E-MTAB-9697"
    respx.get(f"{biostudies.BIOSTUDIES_API}/studies/{acc}").mock(
        return_value=httpx.Response(200, text=fixture("emtab9697.json")))
    respx.get(f"{biostudies.BIOSTUDIES_API}/studies/{acc}/info").mock(
        return_value=httpx.Response(200, json={"httpLink": link, "files": 2}))
    out = biostudies.files(cfg, db, client, accession=acc)
    rows = read_tsv(out["manifest_path"])
    sizes = {r["filename"]: int(r["size"]) for r in rows}
    assert sizes == {"E-MTAB-9697.idf.txt": 4329, "E-MTAB-9697.sdrf.txt": 11869}
    assert rows[0]["url"].startswith(link + "/Files/") and rows[0]["subdir"] == "magetab"
    assert out["summary"]["ena_accessions"] == ["ERP124622"]


@respx.mock
def test_arrayexpress_uses_paged_listing_when_json_incomplete(cfg, db, client):
    acc = "E-MTAB-1"
    respx.get(f"{biostudies.BIOSTUDIES_API}/studies/{acc}").mock(return_value=httpx.Response(200, json={
        "accno": acc, "attributes": [{"name": "Title", "value": "t"}], "section": {"type": "Study"}}))
    respx.get(f"{biostudies.BIOSTUDIES_API}/studies/{acc}/info").mock(
        return_value=httpx.Response(200, json={"httpLink": "https://ftp.ebi.ac.uk/x/E-MTAB-1", "files": 3}))
    pages = [
        {"items": [{"path": "a.txt", "Size": 1, "type": "file"}, {"path": "b.txt", "size": 2, "type": "file"}],
         "pagination": {"offset": 0, "limit": 200, "total": 3}},
    ]
    route = respx.get(f"{biostudies.BIOSTUDIES_API}/studies/{acc}/files").mock(side_effect=[
        httpx.Response(200, json={"items": pages[0]["items"] + [{"path": "c.txt", "Size": 3, "type": "file"}],
                                  "pagination": {"offset": 0, "limit": 200, "total": 3}})])
    out = biostudies.files(cfg, db, client, accession=acc)
    assert out["summary"]["n_files"] == 3 and out["summary"]["total_bytes"] == 6
    assert route.calls[0].request.url.params["limit"] == "200"  # 기본 25개 제한을 피한다


def test_arrayexpress_rejects_bad_accession(cfg, db, client):
    with pytest.raises(ValueError):
        biostudies.files(cfg, db, client, accession="GSE1")


# ---------------------------------------------------------------- PRIDE
@respx.mock
def test_pride_files_with_checksums_and_category(cfg, db, client):
    acc = "PXD001584"
    listing = json.loads(fixture("pride_sage.json"))
    respx.get(f"{pride.PRIDE_API}/projects/{acc}").mock(return_value=httpx.Response(200, text=fixture("pride_project.json")))
    respx.get(f"{pride.PRIDE_API}/projects/{acc}/files/all").mock(return_value=httpx.Response(200, json=listing))
    first = listing[0]["fileName"]
    respx.get(f"{pride.PRIDE_API}/files/checksum/{acc}").mock(return_value=httpx.Response(
        200, text=f"File-Name\tFile-MD5Checksum\tFile-Size\n{first}\t{'a' * 32}\t1\n"))
    out = pride.files(cfg, db, client, accession=acc)
    rows = read_tsv(out["manifest_path"])
    assert len(rows) == len(listing)
    assert all(r["url"].startswith("https://ftp.pride.ebi.ac.uk/") for r in rows)
    assert next(r for r in rows if r["filename"] == first)["checksum"] == "md5:" + "a" * 32
    raw_only = pride.files(cfg, db, client, accession=acc, categories=["RAW"])
    assert all(r["subdir"] == "raw" for r in read_tsv(raw_only["manifest_path"]))
    with pytest.raises(ValueError):
        pride.files(cfg, db, client, accession=acc, categories=["BOGUS"])


# ---------------------------------------------------------------- Zenodo
def test_zenodo_record_id_parsing():
    assert zenodo.record_id("10.5281/zenodo.1215979") == "1215979"
    assert zenodo.record_id("https://zenodo.org/records/3490058") == "3490058"
    assert zenodo.record_id("3490058") == "3490058"
    with pytest.raises(ValueError):
        zenodo.record_id("10.1038/nbt.3820")


@respx.mock
def test_zenodo_files_md5_and_concept_redirect(cfg, db, client):
    respx.get(f"{zenodo.ZENODO_API}/3490057").mock(return_value=httpx.Response(
        302, headers={"location": f"{zenodo.ZENODO_API}/3490058"}))
    respx.get(f"{zenodo.ZENODO_API}/3490058").mock(return_value=httpx.Response(200, json={
        "id": 3490058, "conceptrecid": "3490057", "doi": "10.5281/zenodo.3490058",
        "metadata": {"title": "paper data", "version": "1.0", "license": {"id": "cc-by-4.0"}},
        "files": [{"key": "paper.pdf", "size": 433244, "checksum": "md5:a1399249c4b1368107959c5cc897ae2d",
                   "links": {"self": "https://zenodo.org/api/records/3490058/files/paper.pdf/content"}}]}))
    out = zenodo.files(cfg, db, client, record="10.5281/zenodo.3490057")
    s = out["summary"]
    assert s["resolved_to_latest_version"] and s["record_id"] == "3490058"
    assert s["total_bytes"] == 433244 and s["n_files_with_checksum"] == 1
    row = read_tsv(out["manifest_path"])[0]
    assert row["checksum"] == "md5:a1399249c4b1368107959c5cc897ae2d" and row["url"].endswith("/content")
    script = transfer.prepare_download(cfg, db, out["query_id"])["script_text"]
    assert "md5:a1399249c4b1368107959c5cc897ae2d" in script
