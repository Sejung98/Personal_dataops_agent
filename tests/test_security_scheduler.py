import os
import subprocess
from pathlib import Path

import pytest

from dataops_mcp import scheduler, security
from dataops_mcp.config import ConfigError
from conftest import make_cfg


def test_secret_file_permissions(tmp_path):
    p = tmp_path / "token.txt"
    assert security.check_secret_file(p, "GDC token")["exists"] is False
    p.write_text("abc")
    os.chmod(p, 0o644)
    r = security.check_secret_file(p, "GDC token")
    assert r["ok"] is False and "chmod 600" in r["warning"]
    os.chmod(p, 0o600)
    assert security.check_secret_file(p, "GDC token")["ok"] is True


def test_redact_hides_token_like_strings():
    token = "eyJ" + "a" * 120
    out = security.redact(f"using token={token} and X {token}")
    assert token not in out and "token=***" in out


def test_inline_secret_detection(tmp_path):
    path = tmp_path / "tok"
    assert security.script_has_inline_secret(f"gdc-client download --token-file {path}", [path]) is False
    assert security.script_has_inline_secret("export TOKEN=abcdef", [path]) is True
    assert security.script_has_inline_secret("curl -H 'X-Auth-Token: x'", [path]) is True


def test_slurm_and_pbs_directives(tmp_path):
    res = scheduler.Resources(cpus=4, mem="8G", time="02:00:00", partition="short", account="lab")
    s = scheduler.directives("slurm", "job name!", res, tmp_path / "x.log")
    assert "#SBATCH --job-name=job_name_" in s and "#SBATCH --mem=8G" in s and "#SBATCH --account=lab" in s
    p = scheduler.directives("pbs", "j", res, tmp_path / "x.log")
    assert "#PBS -l select=1:ncpus=4:mem=8gb" in p and "#PBS -q short" in p
    assert scheduler.nf_memory("16G") == "16 GB"
    with pytest.raises(ValueError):
        scheduler.directives("slurm", "j", scheduler.Resources(mem="lots"), tmp_path / "x.log")


def test_slurm_submit_parses_parsable_output(tmp_path):
    script = tmp_path / "a.sh"
    script.write_text("#!/bin/bash\n")

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="987;clusterA\n", stderr="")

    assert scheduler.submit("slurm", script, tmp_path / "a.log", run=run) == "987"


def test_submit_reports_missing_scheduler(tmp_path):
    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(RuntimeError, match="sbatch"):
        scheduler.submit("slurm", tmp_path / "a.sh", tmp_path / "a.log", run=run)


def test_pbs_status_parsing(tmp_path):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="Job Id: 1.srv\n    job_state = F\n    Exit_status = 0\n", stderr="")

    assert scheduler.query_status("pbs", "1.srv", run=run) == "COMPLETED"


def test_local_runner_end_to_end(tmp_path):
    script = tmp_path / "local.sh"
    exit_file = scheduler.exit_file_for(script)
    script.write_text(f"#!/bin/bash\ntrap 'echo $? > {exit_file}' EXIT\necho hello\n")
    pid = scheduler.submit("local", script, tmp_path / "local.log")
    for _ in range(100):
        if exit_file.exists():
            break
        import time
        time.sleep(0.05)
    assert scheduler.query_status("local", pid, script=script) == "COMPLETED"
    assert "hello" in (tmp_path / "local.log").read_text()


def test_config_validation(tmp_path, monkeypatch):
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, monkeypatch, scheduler="lsf")
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, monkeypatch, transfer_node_mode="dtn", transfer_partition="")
    cfg = make_cfg(tmp_path, monkeypatch, gdc_token_path="~/x/token.txt")
    assert cfg.path("gdc_token_path") == Path("~/x/token.txt").expanduser().resolve()
    assert "token" not in str(cfg.public_summary().get("gdc_token_contents", ""))
