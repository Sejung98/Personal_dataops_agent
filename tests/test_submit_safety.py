"""제출 안전장치: dry_run 기본값, confirm 없는 제출 거부, 검수·무결성·동시성 제한."""

import inspect
import shutil
from collections import namedtuple
from pathlib import Path

import pytest
from conftest import GDC_ROWS, FakeScheduler, add_gdc_query, make_cfg

from dataops_mcp import review, server, transfer
from dataops_mcp.state import StateDB


@pytest.fixture
def prepared(cfg, db):
    qid = add_gdc_query(cfg, db, GDC_ROWS)
    out = transfer.prepare_download(cfg, db, qid)
    return out["script_path"], qid


def test_server_tool_defaults_are_safe():
    sig = inspect.signature(server.submit_job)
    assert sig.parameters["dry_run"].default is True
    assert sig.parameters["confirm"].default is False


def test_default_call_is_dry_run_and_never_submits(cfg, db, prepared, fake_sched):
    script, _ = prepared
    out = transfer.submit_job(cfg, db, script, run=fake_sched)
    assert out["submitted"] is False and out["dry_run"] is True
    assert out["would_run"].startswith("sbatch --parsable")
    assert fake_sched.submits == []
    assert db.list_jobs() == []  # DRY_RUN은 기본 목록에서 숨김
    assert len(db.list_jobs(include_dry_run=True)) == 1


def test_real_submit_without_confirm_is_refused(cfg, db, prepared, fake_sched):
    script, _ = prepared
    with pytest.raises(transfer.SubmissionRefused, match="confirm=True"):
        transfer.submit_job(cfg, db, script, confirm=False, dry_run=False, run=fake_sched)
    assert fake_sched.submits == []


def test_confirm_with_dry_run_still_does_not_submit(cfg, db, prepared, fake_sched):
    script, _ = prepared
    out = transfer.submit_job(cfg, db, script, confirm=True, dry_run=True, run=fake_sched)
    assert out["submitted"] is False and fake_sched.submits == []


def test_confirm_without_review_is_refused(cfg, db, prepared, fake_sched):
    script, _ = prepared
    with pytest.raises(transfer.SubmissionRefused, match="peer-reviewer"):
        transfer.submit_job(cfg, db, script, confirm=True, dry_run=False, run=fake_sched)
    assert fake_sched.submits == []


def test_full_path_review_then_confirmed_submit(cfg, db, prepared, fake_sched):
    script, qid = prepared
    rec = review.record_review(cfg, db, script, "PASS")
    assert rec["final_verdict"] == "PASS", rec["defects"]
    out = transfer.submit_job(cfg, db, script, confirm=True, dry_run=False, run=fake_sched)
    assert out["submitted"] is True and out["scheduler_job_id"] == "12345"
    assert fake_sched.submits == [["sbatch", "--parsable", script]]
    job = db.get_job(out["job_id"])
    assert job["status"] == "SUBMITTED" and job["query_id"] == qid

    st = transfer.job_status(cfg, db, "12345", run=fake_sched)
    assert st["status"] == "RUNNING"
    listed = transfer.list_jobs(cfg, db, since_hours=24, run=fake_sched)
    assert listed["n"] == 1 and listed["jobs"][0]["status"] == "RUNNING"


def test_review_fail_cannot_be_overridden(cfg, db, prepared, fake_sched, monkeypatch):
    script, _ = prepared
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100, 100, 0))
    rec = review.record_review(cfg, db, script, "PASS")
    assert rec["final_verdict"] == "FAIL" and rec["overridden_to_fail"]
    assert any("disk_space" in d for d in rec["defects"])
    with pytest.raises(transfer.SubmissionRefused):
        transfer.submit_job(cfg, db, script, confirm=True, dry_run=False, run=fake_sched)


def test_script_modified_after_review_is_refused(cfg, db, prepared, fake_sched):
    script, _ = prepared
    review.record_review(cfg, db, script, "PASS")
    with open(script, "a") as fh:
        fh.write("rm -rf /tmp/whatever\n")
    with pytest.raises(transfer.SubmissionRefused, match="변경"):
        transfer.submit_job(cfg, db, script, confirm=True, dry_run=False, run=fake_sched)


def test_manifest_modified_after_review_is_refused(cfg, db, prepared, fake_sched):
    script, qid = prepared
    review.record_review(cfg, db, script, "PASS")
    manifest = Path(db.get_query(qid)["manifest_path"])
    manifest.write_text(manifest.read_text() + "uuid-9\tevil.bam\t0\t1\treleased\n")
    with pytest.raises(transfer.SubmissionRefused):
        transfer.submit_job(cfg, db, script, confirm=True, dry_run=False, run=fake_sched)


def test_scripts_outside_state_dir_are_refused(cfg, db, tmp_path, fake_sched):
    rogue = tmp_path / "rogue.sh"
    rogue.write_text("#!/bin/bash\necho hi\n")
    with pytest.raises(transfer.SubmissionRefused):
        transfer.submit_job(cfg, db, str(rogue), confirm=True, dry_run=False, run=fake_sched)


def test_unregistered_script_inside_state_dir_is_refused(cfg, db, fake_sched):
    p = cfg.scripts_dir / "x" / "handmade.sh"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/bash\n")
    with pytest.raises(transfer.SubmissionRefused):
        transfer.submit_job(cfg, db, str(p), confirm=True, dry_run=False, run=fake_sched)


def test_max_concurrent_jobs(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, max_concurrent_jobs=1)
    db = StateDB(cfg)
    sched = FakeScheduler(state="RUNNING")
    scripts = []
    for _ in range(2):
        qid = add_gdc_query(cfg, db, GDC_ROWS)
        s = transfer.prepare_download(cfg, db, qid)["script_path"]
        review.record_review(cfg, db, s, "PASS")
        scripts.append(s)
    transfer.submit_job(cfg, db, scripts[0], confirm=True, dry_run=False, run=sched)
    with pytest.raises(transfer.SubmissionRefused, match="max_concurrent_jobs"):
        transfer.submit_job(cfg, db, scripts[1], confirm=True, dry_run=False, run=sched)
    sched.state = ""  # squeue에서 사라지면 sacct(COMPLETED)로 갱신되어 슬롯이 빈다
    out = transfer.submit_job(cfg, db, scripts[1], confirm=True, dry_run=False, run=sched)
    assert out["submitted"]


def test_server_wraps_refusal_as_structured_error(cfg, db, prepared):
    script, _ = prepared
    out = server.submit_job(script_path=script, confirm=False, dry_run=False)
    assert out["ok"] is False and out["refused"] is True
