import os
from pathlib import Path

import pytest
import yaml
from conftest import GDC_ROWS as GDC_FILES
from conftest import add_gdc_query, add_sra_query, make_cfg, secure_dir, sra_run, write_fastqs

from dataops_mcp import transfer
from dataops_mcp.state import StateDB
from dataops_mcp.util import read_tsv



def test_prepare_gdc_open_generates_gdc_client_script(cfg, db):
    qid = add_gdc_query(cfg, db, GDC_FILES)
    out = transfer.prepare_download(cfg, db, qid)
    text = out["script_text"]
    assert "gdc-client download" in text and "--manifest" in text and "--token-file" not in text
    assert "#SBATCH --partition=normal" in text and "#SBATCH --account=lab" in text
    assert out["runner"] == "slurm"
    assert Path(out["script_path"]).is_relative_to(cfg.scripts_dir)
    assert db.get_artifact(out["script_path"])["kind"] == "download"


def test_prepare_gdc_controlled_requires_acknowledgement(cfg, db):
    qid = add_gdc_query(cfg, db, GDC_FILES, access="controlled")
    with pytest.raises(transfer.ControlledAccessNotAcknowledged):
        transfer.prepare_download(cfg, db, qid)


def test_prepare_gdc_controlled_missing_token(cfg, db):
    qid = add_gdc_query(cfg, db, GDC_FILES, access="controlled")
    with pytest.raises(FileNotFoundError):
        transfer.prepare_download(cfg, db, qid, acknowledge_controlled_access=True)


def test_prepare_gdc_controlled_token_path_only_and_perm_warning(cfg, db):
    token = cfg.path("gdc_token_path")
    token.parent.mkdir(parents=True)
    secret = "S3CR3T" * 20
    token.write_text(secret)
    os.chmod(token, 0o644)
    qid = add_gdc_query(cfg, db, GDC_FILES, access="controlled")
    out = transfer.prepare_download(cfg, db, qid, acknowledge_controlled_access=True)
    text = Path(out["script_path"]).read_text()
    assert f"--token-file {token}" in text
    assert secret not in text and secret not in str(out)  # 토큰 내용은 어디에도 노출되지 않음
    assert "umask 077" in text
    assert any("chmod 600" in w for w in out["warnings"])


def test_prepare_sra_fetchngs_compute_mode(cfg, db):
    qid = add_sra_query(cfg, db, [sra_run("SRR1", "SAMN1"), sra_run("SRR2", "SAMN2")])
    out = transfer.prepare_download(cfg, db, qid)
    text = out["script_text"]
    assert "run nf-core/fetchngs" in text and "-r 1.13.0" in text and "-profile singularity" in text
    assert "-resume" in text
    ids = (cfg.manifests_dir / f"{qid}.ids.csv").read_text().split()
    assert ids == ["SRR1", "SRR2"]  # 헤더 없이 한 줄에 하나
    run_dir = Path(out["script_path"]).parents[2] / "runs" / f"{qid}_fetchngs"
    params = yaml.safe_load((run_dir / "params.yaml").read_text())
    assert params["nf_core_pipeline"] == "rnaseq" and params["outdir"] == out["dest"]
    site = (run_dir / "nextflow.dataops.config").read_text()
    assert "executor = 'slurm'" in site and "queue = 'normal'" in site and "--account=lab" in site


def test_login_mode_runs_locally_with_local_executor(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, transfer_node_mode="login")
    db = StateDB(cfg)
    qid = add_sra_query(cfg, db, [sra_run("SRR1", "SAMN1")])
    out = transfer.prepare_download(cfg, db, qid)
    assert out["runner"] == "local" and "#SBATCH" not in out["script_text"]
    site = (cfg.state_dir / "runs" / f"{qid}_fetchngs" / "nextflow.dataops.config").read_text()
    assert "executor = 'local'" in site and "resourceLimits" in site


def test_dtn_mode_uses_transfer_partition(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, transfer_node_mode="dtn", transfer_partition="dtn")
    db = StateDB(cfg)
    qid = add_gdc_query(cfg, db, GDC_FILES)
    out = transfer.prepare_download(cfg, db, qid)
    assert "#SBATCH --partition=dtn" in out["script_text"]


def test_verify_download_gdc(cfg, db):
    qid = add_gdc_query(cfg, db, GDC_FILES)
    dest = transfer.default_dest(cfg, db.get_query(qid))
    (dest / "uuid-1").mkdir(parents=True)
    (dest / "uuid-1" / "a.bam").write_bytes(b"a" * 10)
    rep = transfer.verify_download(cfg, db, qid)
    assert rep["status"] == "FAIL" and rep["missing"] == ["b.bam"]

    (dest / "uuid-2").mkdir()
    (dest / "uuid-2" / "b.bam").write_bytes(b"b" * 20)
    rep = transfer.verify_download(cfg, db, qid, check_md5=True)
    assert rep["status"] == "PASS" and rep["md5_checked_inline"]

    (dest / "uuid-2" / "b.bam").write_bytes(b"c" * 20)  # 같은 크기, 다른 내용
    rep = transfer.verify_download(cfg, db, qid, check_md5=True)
    assert rep["status"] == "FAIL" and rep["md5_mismatch"] == ["b.bam"]

    (dest / "uuid-1" / "x.partial").write_bytes(b"")
    (dest / "uuid-2" / "b.bam").write_bytes(b"b" * 20)
    assert transfer.verify_download(cfg, db, qid)["status"] == "INCOMPLETE"


def test_verify_download_large_md5_becomes_job(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, md5_inline_limit_gb=0)
    db = StateDB(cfg)
    qid = add_gdc_query(cfg, db, GDC_FILES[:1])
    dest = transfer.default_dest(cfg, db.get_query(qid))
    (dest / "uuid-1").mkdir(parents=True)
    (dest / "uuid-1" / "a.bam").write_bytes(b"a" * 10)
    rep = transfer.verify_download(cfg, db, qid, check_md5=True)
    assert rep["md5_job_script"] and "md5sum -c" in Path(rep["md5_job_script"]).read_text()


def test_verify_download_sra_matches_fetchngs_names(cfg, db):
    qid = add_sra_query(cfg, db, [sra_run("SRR1", "SAMN1"), sra_run("SRR2", "SAMN2", layout="SINGLE", sizes=(7,))])
    dest = transfer.default_dest(cfg, db.get_query(qid))
    write_fastqs(dest, "SRR1")
    write_fastqs(dest, "SRR2", layout="SINGLE", sizes=(7,))
    rep = transfer.verify_download(cfg, db, qid)
    assert rep["status"] == "PASS" and rep["expected_files"] == 3


def test_prepare_requires_manifest(cfg, db):
    db.save_query("gdc-x", "gdc", {}, [], {}, None, None)
    with pytest.raises(ValueError):
        transfer.prepare_download(cfg, db, "gdc-x")


def test_controlled_dest_world_readable_warns(cfg, db):
    token = cfg.path("gdc_token_path")
    secure_dir(token.parent)
    token.write_text("t")
    os.chmod(token, 0o600)
    qid = add_gdc_query(cfg, db, GDC_FILES, access="controlled")
    dest = cfg.path("data_root")
    dest.mkdir(parents=True)
    os.chmod(dest, 0o755)
    out = transfer.prepare_download(cfg, db, qid, acknowledge_controlled_access=True)
    assert any("다른 사용자에게 열려" in w for w in out["warnings"])
    assert read_tsv(db.get_query(qid)["manifest_path"])[0]["id"] == "uuid-1"


def test_dbgap_download_requires_ack_and_ngc(tmp_path, monkeypatch):
    ngc = tmp_path / "secrets" / "prj_123.ngc"
    cfg = make_cfg(tmp_path, monkeypatch, dbgap_key_path=str(ngc))
    db = StateDB(cfg)
    qid = add_sra_query(cfg, db, [sra_run("SRR1", "SAMN1")])
    with pytest.raises(transfer.ControlledAccessNotAcknowledged):
        transfer.prepare_download(cfg, db, qid, use_dbgap=True)
    with pytest.raises(FileNotFoundError):
        transfer.prepare_download(cfg, db, qid, use_dbgap=True, acknowledge_controlled_access=True)
    secure_dir(ngc.parent)
    ngc.write_text("NGC-SECRET-CONTENT")
    os.chmod(ngc, 0o600)
    out = transfer.prepare_download(cfg, db, qid, use_dbgap=True, acknowledge_controlled_access=True)
    params = yaml.safe_load((cfg.state_dir / "runs" / f"{qid}_fetchngs" / "params.yaml").read_text())
    assert params["dbgap_key"] == str(ngc) and params["download_method"] == "sratools"
    assert out["access"] == "controlled" and "umask 077" in out["script_text"]
    params_text = (cfg.state_dir / "runs" / f"{qid}_fetchngs" / "params.yaml").read_text()
    assert "NGC-SECRET-CONTENT" not in out["script_text"] + params_text  # 키는 경로로만 전달
    gdc_q = add_gdc_query(cfg, db, GDC_FILES)
    with pytest.raises(ValueError):
        transfer.prepare_download(cfg, db, gdc_q, use_dbgap=True)
