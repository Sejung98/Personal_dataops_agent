"""peer-reviewer용 사전 점검(preflight)과 검수 기록.

preflight_check는 읽기 전용이다. 스크립트·samplesheet·파라미터를 수정하지 않는다.
record_review는 검수 결과를 감사 기록(DB)에 남기며, 자동 점검에 FAIL이 있으면
리뷰어가 PASS를 주더라도 FAIL로 기록한다. submit_job은 현재 스크립트 묶음 해시와
일치하는 최신 PASS 기록이 있을 때만 실제 제출을 허용한다.

점검 항목: 레퍼런스 일치, paired/single 구분, 디스크 여유, controlled-access 취급,
파이프라인 버전 고정, (부가) 스크립트 무결성·크리덴셜 노출·오프라인 준비 상태.
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any

from . import render
from .config import Config
from .security import check_secret_file, dir_exposure, nearest_existing, script_has_inline_secret
from .state import StateDB
from .util import fmt_bytes, read_tsv

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SEMVER_FLAG = re.compile(r"-r\s+(\d+\.\d+\.\d+)\b")
SEX_REQUIRED_TOOLS = {"ascat", "controlfreec", "varlociraptor"}


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _disk(path: Path, need: int | None, label: str) -> dict[str, str]:
    if not need:
        return _check(f"disk_space:{label}", WARN, f"{label} 예상 용량을 알 수 없어 디스크 여유를 확인하지 못했습니다.")
    target = nearest_existing(path)
    free = shutil.disk_usage(target).free
    if free < need:
        return _check(f"disk_space:{label}", FAIL,
                      f"필요 {fmt_bytes(need)} > 가용 {fmt_bytes(free)} ({target})")
    return _check(f"disk_space:{label}", PASS, f"필요 {fmt_bytes(need)}, 가용 {fmt_bytes(free)} ({target})")


def _controlled_checks(cfg: Config, meta: dict, text: str, paths: dict[str, str]) -> list[dict[str, str]]:
    out = []
    if meta.get("source") and meta.get("acknowledged_controlled_access") is False:
        out.append(_check("controlled_access:ack", FAIL, "사용자의 DAC/기관 규정 확인(acknowledge) 기록이 없습니다."))
    if "umask 077" not in text:
        out.append(_check("controlled_access:umask", FAIL, "controlled 데이터 스크립트는 umask 077이어야 합니다."))
    for label, p in paths.items():
        exp = dir_exposure(Path(p))
        if exp["world_accessible"]:
            out.append(_check(f"controlled_access:{label}", FAIL,
                              f"{exp['checked_path']}({exp['mode']})가 other 사용자에게 열려 있습니다."))
        elif exp["group_accessible"]:
            out.append(_check(f"controlled_access:{label}", WARN,
                              f"{exp['checked_path']}({exp['mode']})는 그룹 접근 가능. 그룹원 전원이 승인 대상인지 확인하세요."))
        else:
            out.append(_check(f"controlled_access:{label}", PASS, f"{exp['checked_path']} 소유자 전용"))
    if meta.get("secret_path"):
        sc = check_secret_file(meta["secret_path"], "credential")
        if not sc["exists"]:
            out.append(_check("controlled_access:credential", FAIL, sc["warning"]))
        elif not sc["ok"]:
            out.append(_check("controlled_access:credential", WARN, sc["warning"]))
        else:
            out.append(_check("controlled_access:credential", PASS, f"권한 {sc['mode']}"))
    out.append(_check("controlled_access:policy", WARN,
                      "자동 점검은 DAC 승인 자체를 확인할 수 없습니다. 데이터 사용 승인 범위와 기관 규정을 사람이 확인하세요."))
    return out


def _read_sheet(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _download_checks(cfg: Config, db: StateDB, art: dict, text: str) -> list[dict[str, str]]:
    meta = art["meta"]
    checks = []
    headroom = float(cfg["disk_headroom"])
    tb = meta.get("total_bytes")
    checks.append(_disk(Path(meta["dest"]), int(tb * headroom) if tb else None, "dest"))
    if meta.get("transfer_node_mode") != cfg["transfer_node_mode"]:
        checks.append(_check("transfer_placement", WARN,
                             f"스크립트 생성 후 transfer_node_mode가 {meta.get('transfer_node_mode')} -> "
                             f"{cfg['transfer_node_mode']}로 바뀌었습니다. 다시 생성하세요."))
    if meta.get("pipeline") == "fetchngs":
        m = SEMVER_FLAG.search(text)
        ok = bool(m and m.group(1) == meta.get("version"))
        checks.append(_check("version_pinned", PASS if ok else FAIL,
                             f"nf-core/fetchngs -r {meta.get('version')}" if ok else "fetchngs 버전이 X.Y.Z로 고정되지 않았습니다."))
        if meta.get("executor") != "local" and cfg["transfer_node_mode"] != "compute":
            checks.append(_check("transfer_placement", FAIL,
                                 "계산 노드에 인터넷이 없는데 fetchngs task가 스케줄러로 나갑니다."))
        q = db.get_query(art["query_id"]) if art.get("query_id") else None
        if q and q.get("manifest_path"):
            layouts = {(r.get("library_layout") or "").upper() for r in read_tsv(q["manifest_path"])}
            target = (meta.get("params") or {}).get("nf_core_pipeline")
            if target == "sarek" and "SINGLE" in layouts:
                checks.append(_check("paired_single", FAIL, "sarek은 paired-end FASTQ만 지원하는데 SINGLE run이 있습니다."))
            elif len(layouts - {""}) > 1:
                checks.append(_check("paired_single", WARN, f"PAIRED/SINGLE 혼합: {sorted(layouts)}"))
            else:
                checks.append(_check("paired_single", PASS, f"layout: {sorted(layouts - {''}) or ['unknown']}"))
    if meta.get("access") == "controlled":
        checks += _controlled_checks(cfg, meta, text, {"dest": meta["dest"]})
    return checks


def _reference_checks(meta: dict) -> list[dict[str, str]]:
    ref = meta.get("reference") or {}
    pipeline = meta.get("pipeline")
    checks = []
    if not ref.get("label"):
        return [_check("reference_match", FAIL, f"{pipeline}에 레퍼런스(genome 또는 fasta/reference)가 지정되지 않았습니다.")]
    species_in = [s for s in (meta.get("species") or []) if s and s != "NA"]
    if len(set(species_in)) > 1:
        checks.append(_check("reference_match:species", FAIL, f"입력에 여러 종이 섞여 있습니다: {species_in}"))
    elif species_in and ref.get("species"):
        same = species_in[0].lower() == str(ref["species"]).lower()
        checks.append(_check("reference_match:species", PASS if same else FAIL,
                             f"입력 종 {species_in[0]} / 레퍼런스 {ref['label']}({ref['species']})"))
    else:
        checks.append(_check("reference_match:species", WARN,
                             f"레퍼런스 {ref['label']}의 종 정보를 확인할 수 없습니다. config.references에 species를 적으세요."))
    if meta.get("input_type") == "bam":
        need = meta.get("input_reference")
        have = ref.get("assembly")
        step = (meta.get("params") or {}).get("step")
        if need and have != need:
            checks.append(_check("reference_match:assembly", FAIL,
                                 f"정렬된 BAM의 레퍼런스는 {need}인데 파이프라인 레퍼런스는 {have or ref['label']}입니다. "
                                 f"step={step}에서는 BAM 헤더의 contig 구성과 fasta가 정확히 같아야 합니다. "
                                 f"config.references에 assembly: {need}인 항목을 등록해 쓰세요."))
        else:
            checks.append(_check("reference_match:assembly", PASS, f"BAM/레퍼런스 assembly 일치: {need}"))
    if ref.get("source") == "igenomes-default":
        checks.append(_check("reference_match:explicit", WARN,
                             f"genome을 지정하지 않아 {pipeline} 기본값 {ref['label']}이 쓰입니다. 의도한 것인지 확인하세요."))
    if ref.get("source") in ("igenomes", "igenomes-default") and pipeline == "rnaseq":
        checks.append(_check("reference_match:annotation", WARN,
                             "nf-core/rnaseq 문서는 오래된 iGenomes 주석 대신 --fasta/--gtf 명시를 권장합니다."))
    return checks


def _sheet_checks(meta: dict) -> list[dict[str, str]]:
    checks = []
    sheet = meta.get("samplesheet")
    rows = _read_sheet(sheet) if sheet and Path(sheet).exists() else []
    if not rows:
        return [_check("samplesheet", FAIL, f"samplesheet를 읽을 수 없습니다: {sheet}")]
    file_cols = [c for c in ("fastq_1", "fastq_2", "bam", "bai") if c in rows[0]]
    missing = [r[c] for r in rows for c in file_cols if r.get(c) and not Path(r[c]).exists()]
    checks.append(_check("samplesheet:files_exist", FAIL if missing else PASS,
                         f"없는 파일 {len(missing)}개 (예: {missing[:3]})" if missing else f"{len(rows)}행 파일 존재 확인"))
    pipeline = meta.get("pipeline")
    if "fastq_1" in rows[0]:
        actual = {"PAIRED" if r.get("fastq_2") else "SINGLE" for r in rows}
        declared = set(meta.get("declared_layouts") or [])
        if pipeline == "sarek" and "SINGLE" in actual:
            checks.append(_check("paired_single", FAIL, "sarek FASTQ 입력은 paired-end(lane, fastq_2)만 허용됩니다."))
        elif declared and actual != declared:
            checks.append(_check("paired_single", FAIL,
                                 f"메타데이터 layout {sorted(declared)}와 samplesheet {sorted(actual)}가 다릅니다(mate 누락 가능)."))
        else:
            checks.append(_check("paired_single", PASS, f"layout {sorted(actual)}"))
        if pipeline == "sarek":
            gz = all(r["fastq_1"].endswith((".fq.gz", ".fastq.gz")) for r in rows)
            checks.append(_check("samplesheet:gzip", PASS if gz else FAIL, "sarek은 gzip FASTQ만 허용"))
    if pipeline == "sarek":
        tools = {t.strip() for t in str((meta.get("params") or {}).get("tools") or "").split(",") if t.strip()}
        step = (meta.get("params") or {}).get("step", "mapping")
        if not tools and step not in ("mapping", "markduplicates", "prepare_recalibration", "recalibrate"):
            checks.append(_check("sarek:tools", WARN, "tools가 비어 있어 variant calling이 수행되지 않습니다."))
        if tools & SEX_REQUIRED_TOOLS and any((r.get("sex") or "NA") == "NA" for r in rows):
            checks.append(_check("sarek:sex", FAIL,
                                 f"{sorted(tools & SEX_REQUIRED_TOOLS)}는 sex(XX/XY)가 필요한데 NA인 샘플이 있습니다."))
        if "status" in rows[0] and {r.get("status") for r in rows} == {"0"} and tools & {"mutect2", "strelka", "manta", "ascat", "controlfreec", "msisensorpro"}:
            checks.append(_check("sarek:status", WARN, "모든 샘플이 normal(status=0)입니다. somatic 분석이면 tumor/normal 지정을 확인하세요."))
    return checks


def _pipeline_checks(cfg: Config, art: dict, text: str) -> list[dict[str, str]]:
    meta = art["meta"]
    checks = []
    m = SEMVER_FLAG.search(text)
    ok = bool(m and m.group(1) == meta.get("version"))
    checks.append(_check("version_pinned", PASS if ok else FAIL,
                         f"nf-core/{meta.get('pipeline')} -r {meta.get('version')}" if ok
                         else "파이프라인 버전이 X.Y.Z 릴리스 태그로 고정되지 않았습니다."))
    checks += _reference_checks(meta)
    checks += _sheet_checks(meta)
    checks.append(_disk(Path(meta["work_dir"]), meta.get("estimated_work_bytes"), "work_dir"))
    if meta.get("offline"):
        ref = meta.get("reference") or {}
        if ref.get("source") in ("igenomes", "igenomes-default") and not (meta.get("params") or {}).get("igenomes_base"):
            checks.append(_check("offline_ready:reference", FAIL,
                                 "계산 노드가 오프라인인데 iGenomes(S3) 레퍼런스를 씁니다. 로컬 레퍼런스를 등록하세요."))
        cache = cfg.path("singularity_cache")
        if cfg["container"] in ("singularity", "apptainer"):
            filled = bool(cache and cache.exists() and any(cache.iterdir()))
            checks.append(_check("offline_ready:containers", PASS if filled else WARN,
                                 f"컨테이너 캐시 {cache}" + ("" if filled else " 가 비어 있습니다. prefetch 명령을 먼저 실행하세요.")))
    if meta.get("access") == "controlled":
        checks += _controlled_checks(cfg, meta, text, {"outdir": meta["outdir"], "work_dir": meta["work_dir"]})
    return checks


def preflight_check(cfg: Config, db: StateDB, script_path: str) -> dict[str, Any]:
    script = Path(script_path).expanduser().resolve()
    art = db.get_artifact(script)
    if art is None:
        return {"script_path": str(script), "verdict": FAIL,
                "checks": [_check("registered", FAIL, "DataOps 도구가 생성한 스크립트가 아닙니다.")],
                "defects": ["DataOps 도구가 생성한 스크립트가 아닙니다."], "bundle_sha256": None}
    meta = art["meta"]
    text = script.read_text(encoding="utf-8") if script.exists() else ""
    digest = render.bundle_digest(script, meta.get("inputs", []))
    checks = [_check("bundle_integrity", PASS if digest == meta.get("bundle_sha256") else FAIL,
                     "생성 이후 변경 없음" if digest == meta.get("bundle_sha256")
                     else "스크립트 또는 입력 파일이 생성 이후 변경되었습니다.")]
    secrets = [Path(meta["secret_path"])] if meta.get("secret_path") else []
    checks.append(_check("no_inline_secret", FAIL if script_has_inline_secret(text, secrets) else PASS,
                         "크리덴셜은 파일 경로로만 전달"))
    if meta.get("runner") in ("slurm", "pbs") and not (meta.get("resources") or {}).get("partition"):
        checks.append(_check("scheduler:partition", WARN, "partition이 비어 있어 클러스터 기본 파티션이 쓰입니다."))
    if art["kind"] == "download":
        checks += _download_checks(cfg, db, art, text)
    elif art["kind"] == "pipeline":
        checks += _pipeline_checks(cfg, art, text)
    verdict = FAIL if any(c["status"] == FAIL for c in checks) else PASS
    defects = [f"[{c['status']}] {c['name']}: {c['detail']}" for c in checks if c["status"] != PASS]
    return {
        "script_path": str(script),
        "kind": art["kind"],
        "verdict": verdict,
        "checks": checks,
        "defects": defects,
        "bundle_sha256": digest,
    }


def record_review(
    cfg: Config,
    db: StateDB,
    script_path: str,
    verdict: str,
    defects: list[str] | None = None,
    reviewer: str = "peer-reviewer",
) -> dict[str, Any]:
    verdict = verdict.upper()
    if verdict not in (PASS, FAIL):
        raise ValueError("verdict는 PASS 또는 FAIL")
    auto = preflight_check(cfg, db, script_path)
    if auto["bundle_sha256"] is None:
        raise ValueError("등록되지 않은 스크립트는 검수 기록을 남길 수 없습니다.")
    final = PASS if verdict == PASS and auto["verdict"] == PASS else FAIL
    all_defects = list(defects or []) + auto["defects"]
    rid = db.save_review(auto["script_path"], auto["bundle_sha256"], final, auto["checks"], all_defects, reviewer)
    return {
        "review_id": rid,
        "script_path": auto["script_path"],
        "reviewer_verdict": verdict,
        "automatic_verdict": auto["verdict"],
        "final_verdict": final,
        "overridden_to_fail": verdict == PASS and final == FAIL,
        "defects": all_defects,
    }
