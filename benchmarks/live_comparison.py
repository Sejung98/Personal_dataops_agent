"""Team vs plain-LLM live comparison: run the DataOps team's real code path on small public datasets.

For each request it records what the team reports (numbers from live APIs), whether the generated download
job ran, and whether the downloaded files verified against the repository's sizes/checksums. The plain-LLM
answers to the same requests are in benchmarks/baseline_plain_llm.md and are scored against these results.

Run on a machine that can reach the data hosts (no scheduler needed; jobs use the local runner):
    .venv/bin/python benchmarks/live_comparison.py --out benchmarks/results
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from dataops_mcp import config as cfgmod
from dataops_mcp import encode, gdc, geo, review, scheduler, sra, transfer, zenodo
from dataops_mcp.http import make_client
from dataops_mcp.state import StateDB

REQUESTS = {
    "P1": "TCGA-BRCA의 WGS BAM 중 open access인 것 몇 개, 몇 TB인지 알려줘",
    "P2": "GEO GSE137303의 count matrix(supplementary 파일) 받아줘",
    "P3": "ENCODE ENCSR356KRQ(ATAC-seq)의 GRCh38 peak(bed) 파일 받아줘",
    "P4": "SRA run SRR390278의 메타데이터(종, strategy, layout, read 수, FASTQ 크기)",
    "P5": "Zenodo 레코드 3490058의 파일 받아줘",
}


def timed(fn: Callable[[], Any]) -> tuple[Any, float, str | None]:
    t0 = time.time()
    try:
        return fn(), round(time.time() - t0, 2), None
    except Exception as exc:  # 결과에 그대로 기록
        return None, round(time.time() - t0, 2), f"{type(exc).__name__}: {exc}"


def run_download(cfg, db, query_id: str, timeout: int = 900) -> dict[str, Any]:
    """prepare → 자동 검수 기록 → confirm 제출(local runner) → 완료 대기 → verify(check_md5)."""
    steps: dict[str, Any] = {}
    prep, steps["prepare_s"], err = timed(lambda: transfer.prepare_download(cfg, db, query_id))
    if err:
        return {**steps, "error": err}
    script = prep["script_path"]
    rec, steps["review_s"], err = timed(lambda: review.record_review(cfg, db, script, "PASS"))
    if err or rec["final_verdict"] != "PASS":
        return {**steps, "error": err or "review FAIL", "defects": (rec or {}).get("defects")}
    dry = transfer.submit_job(cfg, db, script)  # 기본 dry run 확인
    steps["dry_run_would_run"] = dry["would_run"]
    sub, steps["submit_s"], err = timed(lambda: transfer.submit_job(cfg, db, script, confirm=True, dry_run=False))
    if err:
        return {**steps, "error": err}
    t0 = time.time()
    exit_file = scheduler.exit_file_for(Path(script))
    while not exit_file.exists() and time.time() - t0 < timeout:
        time.sleep(1)
    steps["download_s"] = round(time.time() - t0, 2)
    steps["job_status"] = transfer.job_status(cfg, db, sub["job_id"], log_lines=15)
    ver, steps["verify_s"], err = timed(lambda: transfer.verify_download(cfg, db, query_id, check_md5=True))
    steps["verify"] = ver if not err else {"error": err}
    return steps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/results")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dataops-bench-"))
    conf = {"scheduler": "local", "transfer_node_mode": "compute", "data_root": str(work / "data"),
            "work_dir": str(work / "work"), "state_dir": str(work / "state"), "max_concurrent_jobs": 2}
    (work / "config.yaml").write_text(yaml.safe_dump(conf))
    os.environ["DATAOPS_CONFIG"] = str(work / "config.yaml")
    cfgmod.clear_cache()
    cfg = cfgmod.load_config()
    db = StateDB(cfg)
    results: dict[str, Any] = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "requests": REQUESTS, "team": {}}

    with make_client(cfg) as client:
        r: dict[str, Any] = {}
        res, r["query_s"], r["error"] = timed(lambda: gdc.search_files(
            cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS", data_format="BAM", access="open"))
        r["open"] = res and {k: res["summary"][k] for k in ("n_files", "total_human", "n_cases")}
        r["api_queries"] = res and res["api_queries"]
        res2, _, _ = timed(lambda: gdc.search_files(
            cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS", data_format="BAM"))
        r["all_access"] = res2 and {k: res2["summary"][k] for k in ("n_files", "total_human", "n_cases", "by_access")}
        results["team"]["P1"] = r

        r = {}
        res, r["query_s"], r["error"] = timed(lambda: geo.files(cfg, db, client, accession="GSE137303"))
        if res:
            r["discovery"] = {"summary": res["summary"], "preview": res["preview"], "api_queries": res["api_queries"]}
            r["download"] = run_download(cfg, db, res["query_id"])
        results["team"]["P2"] = r

        r = {}
        res, r["query_s"], r["error"] = timed(lambda: encode.files(
            cfg, db, client, experiments=["ENCSR356KRQ"], assembly="GRCh38", file_format="bed"))
        if res:
            r["discovery"] = {"summary": res["summary"], "preview": res["preview"], "api_queries": res["api_queries"]}
            r["download"] = run_download(cfg, db, res["query_id"])
        results["team"]["P3"] = r

        r = {}
        res, r["query_s"], r["error"] = timed(lambda: sra.search(cfg, db, client, accession="SRR390278"))
        r["discovery"] = res and {"summary": res["summary"], "preview": res["preview"], "api_queries": res["api_queries"]}
        results["team"]["P4"] = r

        r = {}
        res, r["query_s"], r["error"] = timed(lambda: zenodo.files(cfg, db, client, record="3490058"))
        if res:
            r["discovery"] = {"summary": res["summary"], "preview": res["preview"], "api_queries": res["api_queries"]}
            r["download"] = run_download(cfg, db, res["query_id"])
        results["team"]["P5"] = r

    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results["workdir"] = str(work)
    (out_dir / "team_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({p: {k: v for k, v in d.items() if k in ("error", "query_s")} for p, d in results["team"].items()},
                     indent=1, ensure_ascii=False))
    print(f"saved {out_dir / 'team_results.json'}")


if __name__ == "__main__":
    main()
