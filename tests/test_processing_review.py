import os
import shutil
from collections import namedtuple
from pathlib import Path

import pytest
import yaml
from conftest import GDC_ROWS, add_gdc_query, add_sra_query, make_cfg, secure_dir, sra_run, write_fastqs

from dataops_mcp import processing, review, transfer
from dataops_mcp.state import StateDB
from dataops_mcp.util import read_tsv


def _sra_with_files(cfg, db, runs, layouts=None):
    qid = add_sra_query(cfg, db, runs)
    dest = transfer.default_dest(cfg, db.get_query(qid))
    for r in runs:
        lay = (layouts or {}).get(r["run_accession"], r["library_layout"])
        write_fastqs(dest, r["run_accession"], layout=lay)
    return qid


def _gdc_with_bams(cfg, db, access="open"):
    qid = add_gdc_query(cfg, db, GDC_ROWS, access=access)
    dest = transfer.default_dest(cfg, db.get_query(qid))
    for fid, name, size, *_ in GDC_ROWS:
        (dest / fid).mkdir(parents=True, exist_ok=True)
        (dest / fid / name).write_bytes(b"z" * size)
        (dest / fid / name.replace(".bam", ".bai")).write_bytes(b"i")
    return qid


def _check(result, prefix):
    return [c for c in result["checks"] if c["name"].startswith(prefix)]


# ---------------------------------------------------------------- samplesheets
def test_rnaseq_samplesheet_from_sra(cfg, db):
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1"), sra_run("SRR2", "SAMN1"), sra_run("SRR3", "SAMN2")])
    out = processing.build_samplesheet(cfg, db, qid, "rnaseq", strandedness="reverse")
    assert out["columns"] == ["sample", "fastq_1", "fastq_2", "strandedness"]
    rows = read_tsv(out["samplesheet_path"], delimiter=",")
    assert len(rows) == 3 and out["n_samples"] == 2  # SAMN1의 두 run은 technical replicate로 합쳐짐
    assert all(r["fastq_1"].endswith("_1.fastq.gz") and r["fastq_2"].endswith("_2.fastq.gz") for r in rows)
    assert {r["strandedness"] for r in rows} == {"reverse"}
    with pytest.raises(ValueError):
        processing.build_samplesheet(cfg, db, qid, "rnaseq", strandedness="yes")


def test_rnaseq_rejects_mixed_layout_within_sample(cfg, db):
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1"), sra_run("SRR2", "SAMN1", layout="SINGLE", sizes=(5,))])
    with pytest.raises(ValueError, match="paired와 single"):
        processing.build_samplesheet(cfg, db, qid, "rnaseq")


def test_sarek_fastq_requires_paired(cfg, db):
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1", layout="SINGLE", strategy="WGS", sizes=(5,))])
    with pytest.raises(ValueError, match="paired-end"):
        processing.build_samplesheet(cfg, db, qid, "sarek")


def test_sarek_fastq_samplesheet(cfg, db):
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1", strategy="WGS"), sra_run("SRR2", "SAMN2", strategy="WGS")])
    out = processing.build_samplesheet(cfg, db, qid, "sarek", status_map={"SAMN2": 1}, sex_map={"SAMN1": "XX"})
    rows = read_tsv(out["samplesheet_path"], delimiter=",")
    assert list(rows[0]) == ["patient", "sex", "status", "sample", "lane", "fastq_1", "fastq_2"]
    assert [(r["sample"], r["status"], r["sex"], r["lane"]) for r in rows] == [
        ("SAMN1", "0", "XX", "SRR1"), ("SAMN2", "1", "NA", "SRR2")]


def test_sarek_bam_samplesheet_from_gdc(cfg, db):
    qid = _gdc_with_bams(cfg, db)
    out = processing.build_samplesheet(cfg, db, qid, "sarek")
    rows = read_tsv(out["samplesheet_path"], delimiter=",")
    assert list(rows[0]) == ["patient", "sex", "status", "sample", "bam", "bai"]
    status = {r["sample"]: r["status"] for r in rows}
    assert status == {"TCGA-A-01A": "1", "TCGA-A-10A": "0"}  # tumor / normal
    assert all(r["bai"].endswith(".bai") for r in rows)


def test_gdc_rnaseq_is_rejected_with_guidance(cfg, db):
    qid = _gdc_with_bams(cfg, db)
    with pytest.raises(ValueError, match="FASTQ"):
        processing.build_samplesheet(cfg, db, qid, "rnaseq")


# ---------------------------------------------------------------- prepare_pipeline
def _sheet(cfg, db, pipeline="rnaseq", species="Homo sapiens"):
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1", species=species, strategy="RNA-Seq")])
    return processing.build_samplesheet(cfg, db, qid, pipeline)["samplesheet_path"]


@pytest.mark.parametrize("bad", ["dev", "master", "latest", "3.27", "v3.27.0", ""])
def test_prepare_pipeline_requires_pinned_version(cfg, db, bad, monkeypatch):
    sheet = _sheet(cfg, db)
    if bad == "":
        monkeypatch.setitem(cfg.data["pipelines"]["rnaseq"], "version", "")
    with pytest.raises(ValueError, match="고정"):
        processing.prepare_pipeline(cfg, db, "rnaseq", sheet, version=bad or None)


def test_prepare_pipeline_script_and_params(cfg, db):
    sheet = _sheet(cfg, db)
    out = processing.prepare_pipeline(cfg, db, "rnaseq", sheet, params={"reference": "GRCh38_gencode"})
    text = out["script_text"]
    assert "run nf-core/rnaseq" in text and "-r 3.27.0" in text and "-profile singularity" in text
    assert "-params-file" in text and "-resume" in text and "NXF_OFFLINE" not in text
    params = yaml.safe_load(Path(out["params_file"]).read_text())
    assert params["input"] == sheet and params["fasta"].endswith(".fa") and params["gtf"].endswith(".gtf")
    assert params["igenomes_ignore"] is True and params["genome"] is None
    assert out["reference"]["species"] == "Homo sapiens"


def test_offline_compute_adds_prefetch(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, transfer_node_mode="login")
    db = StateDB(cfg)
    sheet = _sheet(cfg, db)
    out = processing.prepare_pipeline(cfg, db, "rnaseq", sheet, params={"genome": "GRCh38"})
    assert "export NXF_OFFLINE=true" in out["script_text"]
    cmds = "\n".join(out["prefetch"]["commands"])
    assert "nextflow pull nf-core/rnaseq -r 3.27.0" in cmds and "plugin install nf-schema@2.7.2" in cmds
    assert "nf-core pipelines download" in cmds
    assert any("iGenomes" in w for w in out["warnings"])
    res = review.preflight_check(cfg, db, out["script_path"])
    assert res["verdict"] == "FAIL" and _check(res, "offline_ready:reference")[0]["status"] == "FAIL"


# ---------------------------------------------------------------- review checks
def test_review_passes_good_rnaseq(cfg, db):
    sheet = _sheet(cfg, db)
    out = processing.prepare_pipeline(cfg, db, "rnaseq", sheet, params={"reference": "GRCh38_gencode"})
    res = review.preflight_check(cfg, db, out["script_path"])
    assert res["verdict"] == "PASS", res["defects"]
    assert _check(res, "version_pinned")[0]["status"] == "PASS"
    assert _check(res, "reference_match:species")[0]["status"] == "PASS"


def test_review_flags_species_mismatch(cfg, db):
    sheet = _sheet(cfg, db, species="Mus musculus")
    out = processing.prepare_pipeline(cfg, db, "rnaseq", sheet, params={"reference": "GRCh38_gencode"})
    res = review.preflight_check(cfg, db, out["script_path"])
    assert res["verdict"] == "FAIL"
    assert _check(res, "reference_match:species")[0]["status"] == "FAIL"


def test_review_flags_missing_reference_for_rnaseq(cfg, db):
    out = processing.prepare_pipeline(cfg, db, "rnaseq", _sheet(cfg, db))
    res = review.preflight_check(cfg, db, out["script_path"])
    assert _check(res, "reference_match")[0]["status"] == "FAIL"


def test_review_flags_gdc_bam_against_default_sarek_genome(cfg, db):
    sheet = processing.build_samplesheet(cfg, db, _gdc_with_bams(cfg, db), "sarek")["samplesheet_path"]
    out = processing.prepare_pipeline(cfg, db, "sarek", sheet, params={"tools": "mutect2"})
    assert out["params"]["step"] == "variant_calling"
    res = review.preflight_check(cfg, db, out["script_path"])
    asm = _check(res, "reference_match:assembly")[0]
    assert asm["status"] == "FAIL" and "GRCh38.d1.vd1" in asm["detail"]

    ok = processing.prepare_pipeline(cfg, db, "sarek", sheet, params={"tools": "mutect2", "reference": "GRCh38_GDC"})
    res = review.preflight_check(cfg, db, ok["script_path"])
    assert _check(res, "reference_match:assembly")[0]["status"] == "PASS"


def test_review_flags_lost_mates(cfg, db):
    # 메타데이터는 PAIRED인데 디스크에는 _1만 있음 -> samplesheet가 SINGLE이 됨
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1")], layouts={"SRR1": "SINGLE"})
    dest = transfer.default_dest(cfg, db.get_query(qid))
    for p in dest.rglob("*.fastq.gz"):
        p.rename(p.with_name(p.name.replace(".fastq.gz", "_1.fastq.gz")))
    sheet = processing.build_samplesheet(cfg, db, qid, "rnaseq")["samplesheet_path"]
    out = processing.prepare_pipeline(cfg, db, "rnaseq", sheet, params={"reference": "GRCh38_gencode"})
    res = review.preflight_check(cfg, db, out["script_path"])
    assert _check(res, "paired_single")[0]["status"] == "FAIL"


def test_review_flags_sarek_sex_required_tools(cfg, db):
    sheet = processing.build_samplesheet(cfg, db, _gdc_with_bams(cfg, db), "sarek")["samplesheet_path"]
    out = processing.prepare_pipeline(cfg, db, "sarek", sheet,
                                      params={"tools": "ascat,mutect2", "reference": "GRCh38_GDC"})
    res = review.preflight_check(cfg, db, out["script_path"])
    assert _check(res, "sarek:sex")[0]["status"] == "FAIL"


def test_review_disk_space(cfg, db, monkeypatch):
    out = processing.prepare_pipeline(cfg, db, "rnaseq", _sheet(cfg, db), params={"reference": "GRCh38_gencode"})
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10, 10, 1))
    res = review.preflight_check(cfg, db, out["script_path"])
    assert _check(res, "disk_space")[0]["status"] == "FAIL"


def test_review_controlled_access_locations(cfg, db):
    token = cfg.path("gdc_token_path")
    secure_dir(token.parent)
    token.write_text("t")
    os.chmod(token, 0o600)
    data = cfg.path("data_root")
    data.mkdir(parents=True)
    os.chmod(data, 0o755)
    qid = add_gdc_query(cfg, db, GDC_ROWS, access="controlled")
    script = transfer.prepare_download(cfg, db, qid, acknowledge_controlled_access=True)["script_path"]
    res = review.preflight_check(cfg, db, script)
    assert _check(res, "controlled_access:dest")[0]["status"] == "FAIL"
    assert _check(res, "controlled_access:umask")  == []  # umask 077은 들어 있음

    os.chmod(data, 0o700)
    res = review.preflight_check(cfg, db, script)
    assert _check(res, "controlled_access:dest")[0]["status"] == "PASS"
    assert _check(res, "controlled_access:credential")[0]["status"] == "PASS"
    assert res["verdict"] == "PASS", res["defects"]


def test_review_unregistered_script(cfg, db, tmp_path):
    p = tmp_path / "x.sh"
    p.write_text("echo")
    res = review.preflight_check(cfg, db, str(p))
    assert res["verdict"] == "FAIL"
    with pytest.raises(ValueError):
        review.record_review(cfg, db, str(p), "PASS")


def test_review_fetchngs_for_sarek_with_single_end(cfg, db):
    qid = add_sra_query(cfg, db, [sra_run("SRR1", "SAMN1", layout="SINGLE", strategy="WGS", sizes=(5,))])
    script = transfer.prepare_download(cfg, db, qid, nf_core_pipeline="sarek")["script_path"]
    res = review.preflight_check(cfg, db, script)
    assert _check(res, "paired_single")[0]["status"] == "FAIL"
    assert _check(res, "version_pinned")[0]["status"] == "PASS"


def test_run_accession_matching_is_exact(cfg, db):
    # SRR1의 FASTQ를 찾을 때 SRR10 파일이 섞이면 안 된다
    qid = _sra_with_files(cfg, db, [sra_run("SRR1", "SAMN1"), sra_run("SRR10", "SAMN10")])
    rows = read_tsv(processing.build_samplesheet(cfg, db, qid, "rnaseq")["samplesheet_path"], delimiter=",")
    by_sample = {r["sample"]: r["fastq_1"] for r in rows}
    assert by_sample["SAMN1"].endswith("SRX1_SRR1_1.fastq.gz")
    assert by_sample["SAMN10"].endswith("SRX10_SRR10_1.fastq.gz")
    assert transfer.verify_download(cfg, db, qid)["status"] == "PASS"
