import hashlib
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from dataops_mcp import config as cfgmod
from dataops_mcp.state import StateDB, new_query_id
from dataops_mcp.util import write_tsv

REFERENCES = {
    "GRCh38_gencode": {
        "assembly": "GRCh38",
        "build": "GRCh38",
        "species": "Homo sapiens",
        "params": {"fasta": "/ref/GRCh38.primary_assembly.genome.fa", "gtf": "/ref/gencode.v44.gtf"},
    },
    "GRCh38_GDC": {
        "assembly": "GRCh38.d1.vd1",
        "build": "GRCh38",
        "species": "Homo sapiens",
        "params": {"fasta": "/ref/GRCh38.d1.vd1.fa"},
    },
}


def make_cfg(tmp_path: Path, monkeypatch, **overrides):
    data = {
        "scheduler": "slurm",
        "partition": "normal",
        "account": "lab",
        "transfer_node_mode": "compute",
        "container": "singularity",
        "data_root": str(tmp_path / "data"),
        "work_dir": str(tmp_path / "work"),
        "state_dir": str(tmp_path / "state"),
        "singularity_cache": str(tmp_path / "sif"),
        "gdc_token_path": str(tmp_path / "secrets" / "gdc-token.txt"),
        "ega_credentials_path": str(tmp_path / "secrets" / "ega.json"),
        "http": {"retries": 0},
        "references": REFERENCES,
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("DATAOPS_CONFIG", str(path))
    cfgmod.clear_cache()
    return cfgmod.load_config(use_cache=False)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch)
    yield c
    cfgmod.clear_cache()


@pytest.fixture
def db(cfg):
    return StateDB(cfg)


@pytest.fixture
def client():
    with httpx.Client(follow_redirects=True) as c:  # 운영 클라이언트(make_client)와 같게
        yield c


class FakeScheduler:
    """subprocess.run 대체: 호출 기록, sbatch는 job id, squeue는 지정 상태를 반환."""

    def __init__(self, job_id="12345", state="RUNNING"):
        self.calls: list[list[str]] = []
        self.job_id = job_id
        self.state = state

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == "sbatch":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.job_id};cluster\n", stderr="")
        if cmd[0] == "squeue":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.state}\n", stderr="")
        if cmd[0] == "sacct":
            return subprocess.CompletedProcess(cmd, 0, stdout="COMPLETED\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    @property
    def submits(self):
        return [c for c in self.calls if c[0] in ("sbatch", "qsub")]


@pytest.fixture
def fake_sched():
    return FakeScheduler()


# ---- 테스트용 discovery 결과 생성 (HTTP 없이) ---------------------------------
GDC_ROWS = [
    ("uuid-1", "a.bam", 10, hashlib.md5(b"a" * 10).hexdigest(), "Primary Tumor", "TCGA-A", "TCGA-A-01A"),
    ("uuid-2", "b.bam", 20, hashlib.md5(b"b" * 20).hexdigest(), "Blood Derived Normal", "TCGA-A", "TCGA-A-10A"),
]


def add_gdc_query(cfg, db, files, access="open"):
    """files: [(file_id, file_name, size, md5, sample_type, case, sample)]"""
    qid = new_query_id("gdc")
    manifest = write_tsv(
        cfg.manifests_dir / f"{qid}.manifest.tsv",
        [{"id": f[0], "filename": f[1], "md5": f[3], "size": f[2], "state": "released"} for f in files],
        ["id", "filename", "md5", "size", "state"],
    )
    write_tsv(
        cfg.manifests_dir / f"{qid}.metadata.tsv",
        [{"file_id": f[0], "file_name": f[1], "file_size": f[2], "md5sum": f[3], "access": access,
          "data_format": "BAM", "case_submitter_id": f[5], "sample_submitter_id": f[6], "sample_type": f[4]}
         for f in files],
        ["file_id", "file_name", "file_size", "md5sum", "access", "data_format", "case_submitter_id",
         "sample_submitter_id", "sample_type"],
    )
    total = sum(f[2] for f in files)
    db.save_query(qid, "gdc", {"criteria": {}}, [], {"n_files": len(files), "total_bytes": total,
                                                     "reference_genome": "GRCh38.d1.vd1"}, str(manifest), access)
    return qid


def add_sra_query(cfg, db, runs):
    """runs: dict(run_accession, sample_accession, library_layout, library_strategy, scientific_name,
    fastq_ftp, fastq_bytes, fastq_md5)"""
    from dataops_mcp.sra import ENA_FIELDS

    qid = new_query_id("sra")
    path = write_tsv(cfg.manifests_dir / f"{qid}.runinfo.tsv", runs, ENA_FIELDS + ["metadata_source"])
    total = sum(sum(int(x) for x in r["fastq_bytes"].split(";") if x) for r in runs)
    db.save_query(qid, "sra", {"accessions": ["PRJNA1"]}, [], {"n_runs": len(runs), "total_bytes": total},
                  str(path), "open")
    return qid


def sra_run(run, sample, layout="PAIRED", strategy="RNA-Seq", species="Homo sapiens", sizes=(10, 12)):
    if layout == "PAIRED":
        ftp = f"ftp.sra.ebi.ac.uk/vol1/fastq/{run}/{run}_1.fastq.gz;ftp.sra.ebi.ac.uk/vol1/fastq/{run}/{run}_2.fastq.gz"
        b = f"{sizes[0]};{sizes[1]}"
        m = "a" * 32 + ";" + "b" * 32
    else:
        ftp = f"ftp.sra.ebi.ac.uk/vol1/fastq/{run}/{run}.fastq.gz"
        b = str(sizes[0])
        m = "a" * 32
    return {
        "run_accession": run, "experiment_accession": "SRX" + run[3:], "sample_accession": sample,
        "study_accession": "PRJNA1", "library_layout": layout, "library_strategy": strategy,
        "scientific_name": species, "fastq_ftp": ftp, "fastq_bytes": b, "fastq_md5": m, "read_count": "100",
        "base_count": "10000",
    }


def write_fastqs(dest: Path, run: str, layout="PAIRED", sizes=(10, 12)):
    fq = dest / "fastq"
    fq.mkdir(parents=True, exist_ok=True)
    srx = "SRX" + run[3:]
    if layout == "PAIRED":
        (fq / f"{srx}_{run}_1.fastq.gz").write_bytes(b"x" * sizes[0])
        (fq / f"{srx}_{run}_2.fastq.gz").write_bytes(b"y" * sizes[1])
    else:
        (fq / f"{srx}_{run}.fastq.gz").write_bytes(b"x" * sizes[0])


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
