"""SRA/ENA/DDBJ(+GEO) run 단위 메타데이터 discovery.

근거:
- ENA Portal API filereport (result=read_run, fields=...; 기본 응답 TSV):
  https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/file-reports.html
  사용 가능한 필드 목록: https://www.ebi.ac.uk/ena/portal/api/returnFields?dataPortal=ena&result=read_run
- NCBI E-utilities (esearch/esummary/efetch rettype=runinfo):
  https://www.ncbi.nlm.nih.gov/books/NBK25499/
- GEO(GSE/GSM) -> SRA 해석 절차는 nf-core/fetchngs bin/sra_ids_to_runinfo.py 와 같은 방식
  (gds esearch -> esummary samples(GSM) -> sra esearch -> efetch runinfo).

INSDC(SRA·ENA·DDBJ)는 데이터를 상호 미러링하므로 SRA accession도 ENA filereport로 조회된다.
ENA에 아직 반영되지 않은 run은 NCBI runinfo로 보조 조회하고 그 사실을 결과에 표시한다.
"""

from __future__ import annotations

import csv
import io
import os
import re
from typing import Any

import httpx

from . import http
from .config import Config
from .state import StateDB, new_query_id
from .util import counts, fmt_bytes, write_tsv

ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

ENA_FIELDS = [
    "run_accession",
    "experiment_accession",
    "sample_accession",
    "secondary_sample_accession",
    "study_accession",
    "secondary_study_accession",
    "library_layout",
    "library_strategy",
    "library_source",
    "library_selection",
    "instrument_platform",
    "instrument_model",
    "read_count",
    "base_count",
    "tax_id",
    "scientific_name",
    "sample_title",
    "sample_alias",
    "experiment_title",
    "fastq_ftp",
    "fastq_bytes",
    "fastq_md5",
]

INSDC_RE = re.compile(
    r"^(PRJ(NA|EB|DB)\d+|[SED]R[APXRS]\d+|SAM(N|EA|D)\d+)$"
)
GEO_RE = re.compile(r"^(GSE|GSM)\d+$")


def classify(accession: str) -> str:
    acc = accession.strip().upper()
    if GEO_RE.match(acc):
        return "geo"
    if INSDC_RE.match(acc):
        return "insdc"
    raise ValueError(
        f"지원하지 않는 accession 형식: {accession!r}. "
        "PRJNA/PRJEB/PRJDB, SRP/ERP/DRP, SRX/SRR/SRS, SAMN/SAMEA, GSE/GSM 형식을 사용하세요."
    )


def _eutils_params(cfg: Config, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"tool": "genomics-dataops-team"}
    httpcfg = cfg.get("http", {})
    if httpcfg.get("contact_email"):
        params["email"] = httpcfg["contact_email"]
    key = os.environ.get(httpcfg.get("ncbi_api_key_env", "NCBI_API_KEY"))
    if key:
        params["api_key"] = key
    params.update(extra)
    return params


def _parse_tsv(text: str) -> list[dict[str, str]]:
    text = text.strip()
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def ena_filereport(client: httpx.Client, log: http.ApiLog, accession: str, retries: int) -> list[dict[str, str]]:
    params = {"accession": accession, "result": "read_run", "fields": ",".join(ENA_FIELDS)}
    resp = http.request(client, log, "GET", ENA_FILEREPORT, params=params, retries=retries, ok_status=(200, 204))
    if resp.status_code == 204:
        return []
    return _parse_tsv(resp.text)


def _ncbi_runinfo(
    cfg: Config, client: httpx.Client, log: http.ApiLog, term: str, retries: int
) -> list[dict[str, str]]:
    """NCBI SRA esearch -> efetch(rettype=runinfo). CSV 행 목록."""
    es = http.request(
        client, log, "GET", f"{EUTILS}/esearch.fcgi",
        params=_eutils_params(cfg, db="sra", term=term, retmode="json", retmax=10000), retries=retries,
    ).json()
    ids = es.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    rows: list[dict[str, str]] = []
    for i in range(0, len(ids), 200):
        resp = http.request(
            client, log, "GET", f"{EUTILS}/efetch.fcgi",
            params=_eutils_params(cfg, db="sra", id=",".join(ids[i:i + 200]), rettype="runinfo", retmode="text"),
            retries=retries,
        )
        text = resp.text.strip()
        if text:
            rows.extend(r for r in csv.DictReader(io.StringIO(text)) if r.get("Run") and r["Run"] != "Run")
    return rows


def resolve_geo(
    cfg: Config, client: httpx.Client, log: http.ApiLog, accession: str, retries: int
) -> tuple[list[str], set[str]]:
    """GSE/GSM -> (ENA로 조회할 study accession 목록, 해당 GEO에 속한 run 집합)."""
    acc = accession.upper()
    gsms: list[str]
    if acc.startswith("GSM"):
        gsms = [acc]
    else:
        es = http.request(
            client, log, "GET", f"{EUTILS}/esearch.fcgi",
            params=_eutils_params(cfg, db="gds", term=f"{acc}[ACCN]", retmode="json"), retries=retries,
        ).json()
        uids = es.get("esearchresult", {}).get("idlist", [])
        if not uids:
            raise ValueError(f"GEO에서 {acc}를 찾지 못했습니다.")
        summ = http.request(
            client, log, "GET", f"{EUTILS}/esummary.fcgi",
            params=_eutils_params(cfg, db="gds", id=",".join(uids), retmode="json"), retries=retries,
        ).json()
        result = summ.get("result", {})
        gsms = []
        for uid in result.get("uids", uids):
            rec = result.get(uid, {})
            if str(rec.get("accession", "")).upper() == acc:
                gsms.extend(s.get("accession") for s in rec.get("samples", []) if s.get("accession"))
        if not gsms:
            raise ValueError(f"{acc}에 연결된 GSM 샘플이 없습니다(마이크로어레이 전용 시리즈일 수 있음).")
    runinfo: list[dict[str, str]] = []
    for i in range(0, len(gsms), 100):
        term = " OR ".join(f"{g}" for g in gsms[i:i + 100])
        runinfo.extend(_ncbi_runinfo(cfg, client, log, term, retries))
    runs = {r["Run"] for r in runinfo}
    studies = sorted({r.get("SRAStudy") or r.get("BioProject") for r in runinfo if r.get("SRAStudy") or r.get("BioProject")})
    if not runs:
        raise ValueError(f"{acc}의 GSM에 연결된 SRA run이 없습니다.")
    return studies, runs


def _from_ncbi_row(r: dict[str, str]) -> dict[str, str]:
    return {
        "run_accession": r.get("Run", ""),
        "experiment_accession": r.get("Experiment", ""),
        "sample_accession": r.get("BioSample", ""),
        "secondary_sample_accession": r.get("Sample", ""),
        "study_accession": r.get("BioProject", ""),
        "secondary_study_accession": r.get("SRAStudy", ""),
        "library_layout": r.get("LibraryLayout", ""),
        "library_strategy": r.get("LibraryStrategy", ""),
        "library_source": r.get("LibrarySource", ""),
        "library_selection": r.get("LibrarySelection", ""),
        "instrument_platform": r.get("Platform", ""),
        "instrument_model": r.get("Model", ""),
        "read_count": r.get("spots", ""),
        "base_count": r.get("bases", ""),
        "tax_id": r.get("TaxID", ""),
        "scientific_name": r.get("ScientificName", ""),
        "sample_title": r.get("SampleName", ""),
        "sample_alias": r.get("SampleName", ""),
        "experiment_title": "",
        "fastq_ftp": "",
        "fastq_bytes": "",
        "fastq_md5": "",
        "metadata_source": "ncbi_runinfo",
        # dbGaP 보호 run 표시: Consent가 'public'이 아니거나 dbgap_study_accession이 있음
        "consent": r.get("Consent", ""),
        "dbgap_study_accession": r.get("dbgap_study_accession", ""),
    }


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fastq_bytes(row: dict[str, str]) -> int:
    return sum(_int(x) for x in (row.get("fastq_bytes") or "").split(";") if x)


def is_protected(row: dict[str, str]) -> bool:
    consent = (row.get("consent") or "").strip().lower()
    return bool(row.get("dbgap_study_accession")) or (consent not in ("", "public"))


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    library_strategy: str | None = None,
    max_runs: int = 20000,
) -> dict[str, Any]:
    accessions = [a for a in re.split(r"[\s,]+", accession.strip()) if a]
    if not accessions:
        raise ValueError("accession이 비어 있습니다.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    rows: dict[str, dict[str, str]] = {}
    notes: list[str] = []
    for acc in accessions:
        kind = classify(acc)
        if kind == "geo":
            studies, runs = resolve_geo(cfg, client, log, acc, retries)
            notes.append(f"{acc} -> SRA study {', '.join(studies)} ({len(runs)} runs, NCBI E-utilities로 해석)")
            for study in studies:
                for r in ena_filereport(client, log, study, retries):
                    if r.get("run_accession") in runs:
                        r["metadata_source"] = "ena_filereport"
                        rows[r["run_accession"]] = r
            missing = runs - set(rows)
            if missing:
                notes.append(f"{len(missing)}개 run이 ENA에 없어 NCBI runinfo로 보조 조회했습니다.")
                for r in _ncbi_runinfo(cfg, client, log, " OR ".join(sorted(missing)[:500]), retries):
                    rows.setdefault(r["Run"], _from_ncbi_row(r))
        else:
            found = ena_filereport(client, log, acc.upper(), retries)
            for r in found:
                r["metadata_source"] = "ena_filereport"
                rows[r["run_accession"]] = r
            if not found:
                fallback = _ncbi_runinfo(cfg, client, log, acc.upper(), retries)
                if fallback:
                    notes.append(f"{acc}: ENA 결과가 없어 NCBI runinfo로 조회했습니다(FASTQ URL/md5 없음).")
                for r in fallback:
                    rows.setdefault(r["Run"], _from_ncbi_row(r))

    all_rows = list(rows.values())
    if library_strategy:
        wanted = library_strategy.lower()
        selected = [r for r in all_rows if (r.get("library_strategy") or "").lower() == wanted]
        notes.append(f"library_strategy={library_strategy} 필터: {len(all_rows)} -> {len(selected)} runs")
    else:
        selected = all_rows
    truncated = len(selected) > max_runs
    selected = sorted(selected, key=lambda r: r.get("run_accession", ""))[:max_runs]

    query_id = new_query_id("sra")
    runinfo_path = None
    if selected:
        runinfo_path = write_tsv(cfg.manifests_dir / f"{query_id}.runinfo.tsv", selected,
                                 ENA_FIELDS + ["metadata_source", "consent", "dbgap_study_accession"])
    total_fastq = sum(fastq_bytes(r) for r in selected)
    protected = [r for r in selected if is_protected(r)]
    no_fastq = [r["run_accession"] for r in selected if not r.get("fastq_ftp")]
    summary = {
        "n_runs": len(selected),
        "n_experiments": len({r.get("experiment_accession") for r in selected}),
        "n_samples": len({r.get("sample_accession") for r in selected}),
        "studies": sorted({r.get("study_accession") for r in selected if r.get("study_accession")}),
        "by_library_strategy": counts(r.get("library_strategy") for r in selected),
        "by_library_layout": counts(r.get("library_layout") for r in selected),
        "by_library_source": counts(r.get("library_source") for r in selected),
        "by_instrument_model": counts(r.get("instrument_model") for r in selected),
        "by_scientific_name": counts(r.get("scientific_name") for r in selected),
        "total_reads": sum(_int(r.get("read_count")) for r in selected),
        "total_bases": sum(_int(r.get("base_count")) for r in selected),
        "total_bytes": total_fastq,
        "total_human": fmt_bytes(total_fastq),
        "runs_without_fastq": len(no_fastq),
        "dbgap_protected_runs": len(protected),
        "dbgap_studies": sorted({r.get("dbgap_study_accession") for r in protected if r.get("dbgap_study_accession")}),
        "consent_groups": counts(r.get("consent") for r in protected),
        "truncated": truncated,
    }
    access = "controlled" if protected else "open"
    params = {"accessions": accessions, "library_strategy": library_strategy}
    db.save_query(query_id, "sra", params, log.calls, summary, str(runinfo_path) if runinfo_path else None, access)
    result: dict[str, Any] = {
        "query_id": query_id,
        "summary": summary,
        "access": access,
        "runinfo_path": str(runinfo_path) if runinfo_path else None,
        "preview": [
            {k: r.get(k) for k in ("run_accession", "sample_accession", "library_strategy", "library_layout",
                                   "read_count", "scientific_name", "sample_title")}
            for r in selected[:5]
        ],
        "notes": notes,
        "api_queries": log.calls,
    }
    if protected:
        result["dbgap"] = (
            f"{len(protected)}개 run이 dbGaP 보호 데이터입니다({', '.join(summary['dbgap_studies']) or 'study 미상'}). "
            "해당 study의 dbGaP 승인과 repository key(.ngc)가 있어야 하며, prepare_download(use_dbgap=True)로 받습니다."
        )
    if no_fastq:
        result["warning"] = (
            f"{len(no_fastq)}개 run에 ENA FASTQ URL이 없습니다(예: {', '.join(no_fastq[:5])}). "
            "dbGaP 보호 데이터이거나 아직 미러링되지 않았을 수 있습니다."
        )
    if not selected:
        result["hint"] = "run이 0개입니다. accession 철자 또는 library_strategy 값(예: RNA-Seq, WGS)을 확인하세요."
    return result
