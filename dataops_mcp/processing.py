"""처리 준비: nf-core samplesheet 생성과 nextflow 실행 스크립트 생성.

samplesheet 스키마(각 파이프라인 assets/schema_input.json 기준):
- nf-core/rnaseq: sample, fastq_1, fastq_2, strandedness(auto|forward|reverse|unstranded)
  같은 sample 이름의 여러 행은 technical replicate로 합쳐진다.
- nf-core/sarek (FASTQ): patient, sex, status, sample, lane, fastq_1, fastq_2
  (BAM 입력): patient, sex, status, sample, bam, bai
  status: 0=normal, 1=tumor

파이프라인 버전은 반드시 X.Y.Z 형식의 릴리스 태그로 고정한다(dev/master/latest 금지).
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

from . import render
from .config import Config
from .state import StateDB, now_iso
from .transfer import _load_query, default_dest, disk_check, nextflow_site_context
from .util import fmt_bytes, has_accession, read_tsv

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SUPPORTED = ("rnaseq", "sarek")

# nf-core iGenomes 키 -> (build, species)
IGENOMES: dict[str, tuple[str, str]] = {
    "GRCh38": ("GRCh38", "Homo sapiens"),
    "hg38": ("GRCh38", "Homo sapiens"),
    "GATK.GRCh38": ("GRCh38", "Homo sapiens"),
    "GRCh37": ("GRCh37", "Homo sapiens"),
    "hg19": ("GRCh37", "Homo sapiens"),
    "GATK.GRCh37": ("GRCh37", "Homo sapiens"),
    "GRCm38": ("GRCm38", "Mus musculus"),
    "mm10": ("GRCm38", "Mus musculus"),
    "GRCm39": ("GRCm39", "Mus musculus"),
    "Rnor_6.0": ("Rnor_6.0", "Rattus norvegicus"),
    "GRCz10": ("GRCz10", "Danio rerio"),
    "BDGP6": ("BDGP6", "Drosophila melanogaster"),
    "WBcel235": ("WBcel235", "Caenorhabditis elegans"),
    "R64-1-1": ("R64-1-1", "Saccharomyces cerevisiae"),
    "TAIR10": ("TAIR10", "Arabidopsis thaliana"),
}

STRANDEDNESS = ("auto", "forward", "reverse", "unstranded")
_NORMAL_WORDS = ("normal",)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip()) or "sample"


def _find_fastqs(dest: Path, run: str) -> dict[str, Path | None]:
    files = sorted(p for p in dest.rglob("*.fastq.gz") if has_accession(p.name, run))
    r1 = next((p for p in files if p.name.endswith("_1.fastq.gz")), None)
    r2 = next((p for p in files if p.name.endswith("_2.fastq.gz")), None)
    if r1 is None and files:
        single = [p for p in files if not p.name.endswith("_2.fastq.gz")]
        r1 = single[0] if single else None
    return {"r1": r1, "r2": r2}


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") if r.get(c) is not None else "" for c in columns})


def _sra_rows(dest: Path, runinfo: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows, missing = [], []
    for r in runinfo:
        run = r["run_accession"]
        fq = _find_fastqs(dest, run)
        if fq["r1"] is None:
            missing.append(run)
            continue
        rows.append({"run": run, "info": r, "r1": fq["r1"], "r2": fq["r2"]})
    return rows, missing


def build_samplesheet(
    cfg: Config,
    db: StateDB,
    query_id: str,
    pipeline: str,
    dest: str | None = None,
    strandedness: str = "auto",
    sample_column: str = "sample_accession",
    status_map: dict[str, int] | None = None,
    sex_map: dict[str, str] | None = None,
    patient_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    if pipeline not in SUPPORTED:
        raise ValueError(f"지원 파이프라인: {SUPPORTED}")
    query = _load_query(db, query_id)
    source = query["source"]
    dest_path = Path(dest).expanduser().resolve() if dest else default_dest(cfg, query)
    if not dest_path.exists():
        raise FileNotFoundError(f"다운로드 위치가 없습니다: {dest_path}. 전송과 verify_download를 먼저 완료하세요.")
    status_map = status_map or {}
    sex_map = sex_map or {}
    patient_map = patient_map or {}
    warnings: list[str] = []
    meta: dict[str, Any] = {"pipeline": pipeline, "source": source, "query_id": query_id, "dest": str(dest_path)}

    if source == "sra":
        runinfo = read_tsv(query["manifest_path"])
        found, missing = _sra_rows(dest_path, runinfo)
        if missing:
            raise FileNotFoundError(
                f"{len(missing)}개 run의 FASTQ를 {dest_path}에서 찾지 못했습니다(예: {', '.join(missing[:5])}). "
                "verify_download로 전송 상태를 확인하세요."
            )
        layouts = {r["run"]: ("PAIRED" if r["r2"] else "SINGLE") for r in found}
        declared = {r["run_accession"]: (r.get("library_layout") or "").upper() for r in runinfo}
        mismatch = [run for run, lay in layouts.items() if declared.get(run) and declared[run] != lay]
        if mismatch:
            warnings.append(f"메타데이터 layout과 실제 파일이 다른 run: {', '.join(mismatch[:10])}")
        species = sorted({r["info"].get("scientific_name") or "NA" for r in found})
        total = sum(r["r1"].stat().st_size + (r["r2"].stat().st_size if r["r2"] else 0) for r in found)
        if pipeline == "rnaseq":
            if strandedness not in STRANDEDNESS:
                raise ValueError(f"strandedness는 {STRANDEDNESS} 중 하나")
            columns = ["sample", "fastq_1", "fastq_2", "strandedness"]
            rows = []
            for r in found:
                sample = _safe(r["info"].get(sample_column) or r["run"])
                rows.append({"sample": sample, "fastq_1": str(r["r1"]), "fastq_2": str(r["r2"] or ""),
                             "strandedness": strandedness})
            per_sample: dict[str, set[str]] = {}
            for row in rows:
                per_sample.setdefault(row["sample"], set()).add("PAIRED" if row["fastq_2"] else "SINGLE")
            mixed = [s for s, v in per_sample.items() if len(v) > 1]
            if mixed:
                raise ValueError(f"같은 sample에 paired와 single run이 섞여 있습니다: {mixed[:5]}")
            strategies = {r["info"].get("library_strategy") for r in found}
            if strategies - {"RNA-Seq"}:
                warnings.append(f"RNA-Seq 이외 library_strategy 포함: {sorted(s for s in strategies if s)}")
        else:  # sarek
            single = [r["run"] for r in found if r["r2"] is None]
            if single:
                raise ValueError(f"sarek FASTQ 입력은 paired-end만 지원합니다. single-end run: {single[:5]}")
            columns = ["patient", "sex", "status", "sample", "lane", "fastq_1", "fastq_2"]
            rows = []
            for r in found:
                sample = _safe(r["info"].get(sample_column) or r["run"])
                rows.append({
                    "patient": _safe(patient_map.get(sample, sample)),
                    "sex": sex_map.get(sample, "NA"),
                    "status": int(status_map.get(sample, 0)),
                    "sample": sample,
                    "lane": r["run"],
                    "fastq_1": str(r["r1"]),
                    "fastq_2": str(r["r2"]),
                })
            if not status_map:
                warnings.append("status_map이 없어 모든 샘플을 normal(status=0, germline)로 지정했습니다.")
        meta.update({
            "input_type": "fastq",
            "input_reference": None,
            "species": species,
            "layouts": sorted(set(layouts.values())),
            "declared_layouts": sorted({v for v in declared.values() if v}),
            "total_input_bytes": total,
        })

    elif source == "gdc":
        if pipeline == "rnaseq":
            raise ValueError(
                "GDC RNA-seq는 정렬된 BAM과 STAR counts로 제공됩니다. nf-core/rnaseq는 FASTQ 입력이 필요하므로 "
                "BAM→FASTQ 변환이 필요합니다. 발현량만 필요하면 GDC의 'Gene Expression Quantification'(STAR - Counts) "
                "파일을 바로 쓰는 것이 일반적입니다."
            )
        md = read_tsv(Path(query["manifest_path"]).with_name(f"{query_id}.metadata.tsv"))
        rows, missing_idx = [], []
        total = 0
        for r in md:
            if (r.get("data_format") or "").upper() != "BAM":
                continue
            bam = dest_path / r["file_id"] / r["file_name"]
            if not bam.exists():
                raise FileNotFoundError(f"BAM이 없습니다: {bam}. verify_download를 먼저 실행하세요.")
            bai = next(iter(sorted(bam.parent.glob("*.bai"))), None)
            if bai is None:
                missing_idx.append(r["file_name"])
            total += bam.stat().st_size
            sample = _safe(r.get("sample_submitter_id") or r["file_id"])
            patient = _safe(patient_map.get(sample, r.get("case_submitter_id") or sample))
            stype = (r.get("sample_type") or "").lower()
            status = status_map.get(sample, 0 if any(w in stype for w in _NORMAL_WORDS) else 1)
            rows.append({"patient": patient, "sex": sex_map.get(sample, "NA"), "status": int(status),
                         "sample": sample, "bam": str(bam), "bai": str(bai or "")})
        if not rows:
            raise ValueError("이 query에 BAM 파일이 없습니다.")
        if missing_idx:
            warnings.append(f"BAI 인덱스가 없는 BAM {len(missing_idx)}개 (gdc-client는 기본적으로 index를 함께 받습니다)")
        columns = ["patient", "sex", "status", "sample", "bam", "bai"]
        meta.update({
            "input_type": "bam",
            "input_reference": query["summary"].get("reference_genome", "GRCh38.d1.vd1"),
            "species": ["Homo sapiens"],
            "layouts": ["PAIRED"],
            "declared_layouts": ["PAIRED"],
            "total_input_bytes": total,
            "suggested_step": "variant_calling",
        })
    else:
        raise ValueError(
            "EGA 파일은 파일명과 샘플의 매핑이 데이터셋마다 달라 자동 samplesheet를 만들지 않습니다. "
            "직접 만든 samplesheet 경로를 prepare_pipeline에 전달하세요."
        )

    path = (cfg.samplesheets_dir / f"{query_id}.{pipeline}.csv").resolve()
    _write_csv(path, rows, columns)
    meta.update({
        "columns": columns,
        "n_rows": len(rows),
        "n_samples": len({r["sample"] for r in rows}),
        "access": query.get("access"),
    })
    db.register_artifact(path, "samplesheet", query_id, meta)
    return {
        "samplesheet_path": str(path),
        "pipeline": pipeline,
        "n_rows": len(rows),
        "n_samples": meta["n_samples"],
        "columns": columns,
        "preview": rows[:5],
        "input_type": meta["input_type"],
        "total_input_human": fmt_bytes(meta["total_input_bytes"]),
        "warnings": warnings,
    }


# 파라미터를 주지 않았을 때 각 파이프라인이 쓰는 genome 기본값(nextflow_schema.json 기준)
PIPELINE_DEFAULT_GENOME = {"rnaseq": None, "sarek": "GATK.GRCh38"}


def _resolve_reference(cfg: Config, params: dict[str, Any], pipeline: str | None = None) -> dict[str, Any]:
    """params의 reference(config.references 이름) 또는 genome(iGenomes 키)을 해석한다."""
    info: dict[str, Any] = {"label": None, "build": None, "species": None, "assembly": None, "source": None}
    ref_name = params.pop("reference", None)
    if ref_name:
        refs = cfg.get("references", {}) or {}
        if ref_name not in refs:
            raise KeyError(f"config.references에 {ref_name!r}가 없습니다. 등록된 이름: {sorted(refs)}")
        entry = refs[ref_name]
        for k, v in (entry.get("params") or {}).items():
            params.setdefault(k, v)
        params.setdefault("igenomes_ignore", True)
        # sarek은 genome 기본값이 GATK.GRCh38이므로 명시적으로 null을 넣어야 커스텀 fasta가 쓰인다.
        params["genome"] = None
        info.update(label=ref_name, build=entry.get("build"), species=entry.get("species"),
                    assembly=entry.get("assembly") or entry.get("build"), source="config")
    elif params.get("genome"):
        g = params["genome"]
        build, species = IGENOMES.get(g, (None, None))
        info.update(label=g, build=build, species=species, assembly=g, source="igenomes")
    elif params.get("fasta"):
        info.update(label=Path(str(params["fasta"])).name, source="fasta")
    elif pipeline and PIPELINE_DEFAULT_GENOME.get(pipeline) and "genome" not in params:
        g = PIPELINE_DEFAULT_GENOME[pipeline]
        build, species = IGENOMES.get(g, (None, None))
        info.update(label=g, build=build, species=species, assembly=g, source="igenomes-default")
    return info


def prepare_pipeline(
    cfg: Config,
    db: StateDB,
    pipeline: str,
    samplesheet: str,
    version: str | None = None,
    params: dict[str, Any] | None = None,
    outdir: str | None = None,
    run_name: str | None = None,
) -> dict[str, Any]:
    if pipeline not in SUPPORTED:
        raise ValueError(f"지원 파이프라인: {SUPPORTED}")
    version = str(version or cfg.pipeline(pipeline).get("version") or "")
    if not SEMVER.match(version):
        raise ValueError(
            f"파이프라인 버전은 X.Y.Z 릴리스 태그로 고정해야 합니다(받은 값: {version!r}). dev/master/latest는 금지."
        )
    sheet = Path(samplesheet).expanduser().resolve()
    if not sheet.exists():
        raise FileNotFoundError(str(sheet))
    sheet_art = db.get_artifact(sheet)
    sheet_meta = (sheet_art or {}).get("meta", {})
    query_id = (sheet_art or {}).get("query_id")
    if sheet_meta.get("pipeline") and sheet_meta["pipeline"] != pipeline:
        raise ValueError(f"이 samplesheet는 {sheet_meta['pipeline']}용입니다.")

    merged: dict[str, Any] = dict(cfg.pipeline(pipeline).get("default_params") or {})
    merged.update(params or {})
    ref = _resolve_reference(cfg, merged, pipeline)
    if pipeline == "sarek" and sheet_meta.get("input_type") == "bam":
        merged.setdefault("step", sheet_meta.get("suggested_step", "variant_calling"))

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = re.sub(r"[^A-Za-z0-9_.-]", "_", run_name or f"{pipeline}_{query_id or 'manual'}_{stamp}")
    run_dir = (cfg.state_dir / "runs" / run_name).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    out = Path(outdir).expanduser().resolve() if outdir else (cfg.path("data_root") / "results" / run_name).resolve()  # type: ignore[operator]
    work = (cfg.path("work_dir") / run_name).resolve()  # type: ignore[operator]
    merged["input"] = str(sheet)
    merged["outdir"] = str(out)

    params_file = run_dir / "params.yaml"
    params_file.write_text(yaml.safe_dump(merged, sort_keys=True, allow_unicode=True), encoding="utf-8")
    head = render.resources_from(cfg, "nextflow_head_job")
    site = nextflow_site_context(cfg, "scheduler", head)
    site_config = run_dir / "nextflow.dataops.config"
    site_config.write_text(render.render("nextflow_site.config.j2", generated_at=now_iso(), **site), encoding="utf-8")

    offline = not cfg.compute_has_internet()
    runner = cfg["scheduler"]
    tools = cfg.get("tools", {})
    expansion = float(cfg.pipeline(pipeline).get("work_expansion", 3.0))
    est = int((sheet_meta.get("total_input_bytes") or 0) * expansion)
    disk = disk_check(work, est, 1.0)
    context = {
        "pipeline": pipeline,
        "version": version,
        "profile": cfg["container"],
        "site_config": str(site_config),
        "params_file": str(params_file),
        "work_dir": str(work),
        "outdir": str(out),
        "launch_dir": str(run_dir),
        "nextflow": tools.get("nextflow", "nextflow"),
        "offline": offline,
        "container": cfg["container"],
        "container_cache": str(cfg.path("singularity_cache")),
    }
    meta = {
        "pipeline": pipeline,
        "version": version,
        "params": merged,
        "params_file": str(params_file),
        "site_config": str(site_config),
        "samplesheet": str(sheet),
        "outdir": str(out),
        "work_dir": str(work),
        "offline": offline,
        "reference": ref,
        "input_type": sheet_meta.get("input_type"),
        "input_reference": sheet_meta.get("input_reference"),
        "species": sheet_meta.get("species"),
        "layouts": sheet_meta.get("layouts"),
        "declared_layouts": sheet_meta.get("declared_layouts"),
        "estimated_work_bytes": est,
        "access": sheet_meta.get("access"),
        "executor": site["executor"],
    }
    result = render.write_script(
        cfg, db, template="nextflow_run.sh.j2", kind="pipeline", name=run_name, group=run_name,
        runner=runner, resources=head, context=context, meta=meta, query_id=query_id,
        inputs=[str(sheet), str(params_file), str(site_config)],
        umask="077" if sheet_meta.get("access") == "controlled" else "027",
    )
    warnings: list[str] = []
    if not disk["ok"]:
        warnings.append(f"work 디렉토리 예상 사용량 {fmt_bytes(est)} > 가용 {disk['free_human']}")
    prefetch = None
    if offline:
        nf = tools.get("nextflow", "nextflow")
        cache = cfg.path("singularity_cache")
        csys = cfg["container"] if cfg["container"] in ("singularity", "apptainer") else "none"
        prefetch = {
            "why": "계산 노드에 인터넷이 없으므로 로그인 노드에서 파이프라인 코드·컨테이너·플러그인을 미리 받아야 합니다.",
            "commands": [
                f"export NXF_SINGULARITY_CACHEDIR={cache} NXF_APPTAINER_CACHEDIR={cache}",
                f"{nf} pull nf-core/{pipeline} -r {version}",
                *[f"{nf} plugin install {pl}" for pl in cfg.pipeline(pipeline).get("plugins", [])],
                f"nf-core pipelines download nf-core/{pipeline} --revision {version} --container-system {csys} "
                f"--container-cache-utilisation amend --compress none --outdir {cache}/nf-core-{pipeline}-{version}",
            ],
        }
        if ref["source"] in ("igenomes", "igenomes-default"):
            warnings.append("오프라인 계산 노드에서는 iGenomes(AWS S3) 레퍼런스를 받을 수 없습니다. "
                            "config.references에 로컬 경로를 등록하고 reference=<이름>을 쓰세요.")
    return {
        "script_path": result["script_path"],
        "log_path": result["log_path"],
        "params_file": str(params_file),
        "site_config": str(site_config),
        "outdir": str(out),
        "work_dir": str(work),
        "pipeline": f"nf-core/{pipeline}",
        "version": version,
        "reference": ref,
        "disk": disk,
        "offline_compute": offline,
        "prefetch": prefetch,
        "warnings": warnings,
        "script_text": result["script_text"],
        "params": merged,
        "next_step": "peer-reviewer 검수 후 사용자 승인을 받아 submit_job을 호출하세요.",
    }
