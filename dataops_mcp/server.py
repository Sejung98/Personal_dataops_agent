"""DataOps MCP 서버 (stdio).

실행: python -m dataops_mcp.server
`mcp` 패키지 2.x에서는 FastMCP가 MCPServer로 이름이 바뀌었으므로 둘 다 지원한다.
"""

import inspect
import os
import shutil
import traceback
from typing import Any, Callable, Optional, Union

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

from . import (biostudies, catalog, cbioportal, cellxgene, ega, encode, gdc, geo, pride, processing, review,
               scheduler, sra, transfer, urlfiles, zenodo)
from .config import load_config
from .http import make_client
from .security import check_secret_file
from .state import StateDB

INSTRUCTIONS = """\
Genomics DataOps 도구 모음. 규칙:
1) 메타데이터 수치(파일 수·용량·accession)는 반드시 이 도구들의 결과로만 답한다. 기억으로 만들지 않는다.
2) 결과마다 query_id와 api_queries(근거 API 호출)가 있다. 사용자에게 보고할 때 함께 인용한다.
3) prepare_* 도구는 스크립트를 생성만 한다. 실행은 submit_job으로만 한다.
4) submit_job은 기본 dry_run=True. peer-reviewer PASS + 사용자 명시 승인 후에만 confirm=True, dry_run=False.
"""

server = _Server(name="dataops", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_TOOL_PARAMS = inspect.signature(server.tool).parameters

StrOrList = Optional[Union[str, list[str]]]


def tool(annotations: ToolAnnotations, meta: Optional[dict] = None) -> Callable:
    kwargs: dict[str, Any] = {"annotations": annotations}
    if meta and "meta" in _TOOL_PARAMS:
        kwargs["meta"] = meta
    return server.tool(**kwargs)


def _ctx():
    cfg = load_config()
    return cfg, StateDB(cfg)


def _run(fn: Callable[[], dict]) -> dict:
    try:
        out = fn()
        out.setdefault("ok", True)
        return out
    except (transfer.SubmissionRefused, transfer.ControlledAccessNotAcknowledged) as exc:
        return {"ok": False, "refused": True, "error_type": type(exc).__name__, "error": str(exc)}
    except Exception as exc:  # 도구 오류는 구조화해서 돌려준다
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "trace": traceback.format_exc(limit=3)[-1500:] if os.environ.get("DATAOPS_DEBUG") else None,
        }


# ---------------------------------------------------------------- environment
@tool(READ_ONLY)
def check_environment() -> dict:
    """설정 요약, 필요한 CLI(gdc-client, nextflow, pyega3, sbatch/qsub) 존재 여부, 크리덴셜 파일 권한을 점검한다.
    크리덴셜 내용은 읽지 않는다."""

    def go() -> dict:
        cfg, _ = _ctx()
        tools = cfg.get("tools", {})
        return {
            "config": cfg.public_summary(),
            "cli": {name: shutil.which(cmd) for name, cmd in {
                "gdc-client": tools.get("gdc_client", "gdc-client"),
                "nextflow": tools.get("nextflow", "nextflow"),
                "pyega3": tools.get("pyega3", "pyega3"),
                "singularity": "singularity",
                "apptainer": "apptainer",
                "java": "java",
                "md5sum": "md5sum",
            }.items()},
            "scheduler_cli": scheduler.available_scheduler_tools(),
            "credentials": [
                check_secret_file(cfg.path("gdc_token_path"), "GDC token"),
                check_secret_file(cfg.path("ega_credentials_path"), "EGA credentials"),
            ],
        }

    return _run(go)


@tool(READ_ONLY)
def list_databases() -> dict:
    """지원 데이터베이스 카탈로그(도메인, 데이터 종류, 접근 수준, 담당 도구·subagent, 다운로드 방식)와
    지원하지 않는 자원 및 이유. 수치는 없다(수치는 각 discovery 도구로만)."""
    return _run(catalog.list_databases)


# ---------------------------------------------------------------- discovery: GDC
@tool(READ_ONLY)
def gdc_search_files(
    project: StrOrList = None,
    primary_site: StrOrList = None,
    disease_type: StrOrList = None,
    sample_type: StrOrList = None,
    data_category: StrOrList = None,
    data_type: StrOrList = None,
    data_format: StrOrList = None,
    experimental_strategy: StrOrList = None,
    workflow_type: StrOrList = None,
    access: StrOrList = None,
    max_files: int = 50000,
) -> dict:
    """GDC /files 검색(live API). 파일 수·총 용량·케이스 수 요약과 gdc-client manifest TSV 경로를 반환하고
    query_id로 저장한다. 값 예: project='TCGA-BRCA', experimental_strategy='WGS', data_format='BAM',
    access='open'|'controlled', workflow_type='STAR - Counts'. 값 철자는 gdc_get_field_values로 확인."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.search_files(
                cfg, db, client, project=project, primary_site=primary_site, disease_type=disease_type,
                sample_type=sample_type, data_category=data_category, data_type=data_type,
                data_format=data_format, experimental_strategy=experimental_strategy,
                workflow_type=workflow_type, access=access, max_files=max_files,
            )

    return _run(go)


@tool(READ_ONLY)
def gdc_search_cases(
    project: StrOrList = None,
    primary_site: StrOrList = None,
    disease_type: StrOrList = None,
    sample_type: StrOrList = None,
    data_category: StrOrList = None,
    data_type: StrOrList = None,
    data_format: StrOrList = None,
    experimental_strategy: StrOrList = None,
    workflow_type: StrOrList = None,
    access: StrOrList = None,
    max_cases: int = 20000,
) -> dict:
    """GDC /cases 검색(live API). 조건(파일 조건 포함)에 맞는 케이스 수와 project/primary_site 분포를 반환한다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.search_cases(
                cfg, db, client, project=project, primary_site=primary_site, disease_type=disease_type,
                sample_type=sample_type, data_category=data_category, data_type=data_type,
                data_format=data_format, experimental_strategy=experimental_strategy,
                workflow_type=workflow_type, access=access, max_cases=max_cases,
            )

    return _run(go)


@tool(READ_ONLY)
def gdc_get_field_values(
    field: Optional[str] = None,
    endpoint: str = "files",
    search: Optional[str] = None,
    filters: Optional[dict[str, Union[str, list[str]]]] = None,
) -> dict:
    """GDC 필드 값 목록(facets) 또는 필드 이름 목록(_mapping) 조회.
    field 지정: 해당 필드의 값과 개수(예: field='experimental_strategy', filters={'project': 'TCGA-BRCA'}).
    field 생략: endpoint의 필드 이름 목록(search로 부분 문자열 필터)."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.get_field_values(cfg, db, client, field=field, endpoint=endpoint, search=search,
                                        filters_criteria=filters)

    return _run(go)


# ---------------------------------------------------------------- discovery: SRA / EGA
@tool(READ_ONLY)
def sra_search(accession: str, library_strategy: Optional[str] = None, max_runs: int = 20000) -> dict:
    """SRA/ENA/DDBJ/GEO accession(PRJNA, PRJEB, SRP, ERP, SRX, SRR, SAMN, GSE, GSM; 쉼표로 여러 개)의
    run 단위 메타데이터를 ENA Portal API filereport로 조회(GEO는 NCBI E-utilities로 해석).
    library_strategy(예: 'RNA-Seq', 'WGS')로 run을 거를 수 있다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return sra.search(cfg, db, client, accession=accession, library_strategy=library_strategy,
                              max_runs=max_runs)

    return _run(go)


@tool(READ_ONLY)
def ega_search(accession: str, include_files: bool = True) -> dict:
    """EGA 공개 메타데이터 API로 study(EGAS)/dataset(EGAD)/file(EGAF) 조회. 파일 목록과 총 용량을 반환한다.
    EGA 데이터는 모두 controlled access."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return ega.search(cfg, db, client, accession=accession, include_files=include_files)

    return _run(go)


# ---------------------------------------------------------------- discovery: ENCODE / single-cell / cBioPortal
@tool(READ_ONLY)
def encode_search(
    assay_title: StrOrList = None,
    biosample: StrOrList = None,
    target: StrOrList = None,
    organism: StrOrList = None,
    assembly: StrOrList = None,
    search_term: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """ENCODE 실험 검색(released). 예: assay_title='ATAC-seq'|'TF ChIP-seq'|'Histone ChIP-seq'|'total RNA-seq',
    biosample='K562', target='CTCF', organism='Homo sapiens', assembly='GRCh38'. 실험 목록 TSV와 분포를 반환."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return encode.search(cfg, db, client, assay_title=assay_title, biosample=biosample, target=target,
                                 organism=organism, assembly=assembly, search_term=search_term, limit=limit)

    return _run(go)


@tool(READ_ONLY)
def encode_files(
    experiments: Optional[list[str]] = None,
    from_query_id: Optional[str] = None,
    assembly: Optional[str] = None,
    output_type: StrOrList = None,
    file_format: StrOrList = None,
    preferred_default_only: bool = False,
) -> dict:
    """ENCODE 실험(ENCSR...)들의 released 파일 목록·크기·md5를 manifest로 저장. encode_search의 query_id도 받는다.
    예: assembly='GRCh38', output_type='conservative IDR thresholded peaks', file_format='bed'."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return encode.files(cfg, db, client, experiments=experiments, from_query_id=from_query_id,
                                assembly=assembly, output_type=output_type, file_format=file_format,
                                preferred_default_only=preferred_default_only)

    return _run(go)


@tool(READ_ONLY)
def cellxgene_search(
    organism: StrOrList = None,
    tissue: StrOrList = None,
    disease: StrOrList = None,
    assay: StrOrList = None,
    text: Optional[str] = None,
    collection_id: Optional[str] = None,
    asset_types: Optional[list[str]] = None,
) -> dict:
    """CZ CELLxGENE Discover에서 단일세포 데이터셋 검색(라벨 부분일치, 대소문자 무시). 예: tissue='lung',
    disease='COVID-19', assay='10x 3', organism='Homo sapiens'. 기본 asset은 H5AD."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return cellxgene.search(cfg, db, client, organism=organism, tissue=tissue, disease=disease, assay=assay,
                                    text=text, collection_id=collection_id, asset_types=asset_types)

    return _run(go)


@tool(READ_ONLY)
def cbioportal_search(keyword: Optional[str] = None, study_ids: Optional[list[str]] = None) -> dict:
    """cBioPortal study 검색(keyword: 이름·ID·암종 부분일치) 또는 study_ids 지정. study별 샘플 수·참조 게놈과
    datahub tarball manifest를 반환."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return cbioportal.search(cfg, db, client, keyword=keyword, study_ids=study_ids)

    return _run(go)


# ---------------------------------------------------------------- discovery: study files
@tool(READ_ONLY)
def geo_files(
    accession: str,
    include_matrix: bool = True,
    include_samples: bool = False,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> dict:
    """GEO 처리 데이터(GSE/GSM)의 supplementary 파일과 series matrix 목록·크기를 조회해 manifest로 저장.
    include/exclude는 파일명 glob(예: ['*.h5', '*counts*'], exclude=['*_RAW.tar']). 원시 read는 sra_search."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return geo.files(cfg, db, client, accession=accession, include_matrix=include_matrix,
                             include_samples=include_samples, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def arrayexpress_files(
    accession: str, include: Optional[list[str]] = None, exclude: Optional[list[str]] = None
) -> dict:
    """ArrayExpress(E-MTAB-, E-GEOD- 등; EBI BioStudies)의 처리 데이터·MAGE-TAB 파일 목록·크기를 manifest로 저장.
    연결된 ENA study(원시 read)도 알려준다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return biostudies.files(cfg, db, client, accession=accession, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def pride_files(
    accession: str,
    categories: Optional[list[str]] = None,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> dict:
    """PRIDE 프로테오믹스 프로젝트(PXD)의 파일 목록·크기·MD5를 manifest로 저장.
    categories로 RAW, SEARCH, RESULT, PEAK, OTHER 등을 고를 수 있다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return pride.files(cfg, db, client, accession=accession, categories=categories, include=include,
                               exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def zenodo_files(record: str, include: Optional[list[str]] = None, exclude: Optional[list[str]] = None) -> dict:
    """Zenodo 레코드(번호, 10.5281/zenodo.N DOI 또는 URL)의 파일 목록·크기·MD5를 manifest로 저장.
    concept DOI는 최신 버전으로 해석된다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return zenodo.files(cfg, db, client, record=record, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def url_inspect(urls: list[str], subdir: str = "") -> dict:
    """카탈로그에 없는 공개 파일(예: Ensembl/GENCODE 레퍼런스, gnomAD, 1000 Genomes)의 HTTPS URL을 HEAD로
    확인(존재·크기)하고 다운로드 manifest로 저장한다."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return urlfiles.inspect_urls(cfg, db, client, urls, subdir=subdir)

    return _run(go)


@tool(READ_ONLY)
def get_query(query_id: str) -> dict:
    """저장된 discovery 결과(요약, 파라미터, 근거 API 호출, manifest 경로)를 query_id로 조회."""

    def go() -> dict:
        _, db = _ctx()
        q = db.get_query(query_id)
        if q is None:
            raise KeyError(f"query_id {query_id!r} 없음")
        return q

    return _run(go)


@tool(READ_ONLY)
def list_queries(limit: int = 20) -> dict:
    """최근 discovery 결과 목록."""

    def go() -> dict:
        _, db = _ctx()
        return {"queries": db.list_queries(limit)}

    return _run(go)


# ---------------------------------------------------------------- transfer
@tool(LOCAL_WRITE)
def prepare_download(
    query_id: str,
    dest: Optional[str] = None,
    acknowledge_controlled_access: bool = False,
    nf_core_pipeline: Optional[str] = "rnaseq",
    download_method: Optional[str] = None,
    use_dbgap: bool = False,
) -> dict:
    """query_id의 다운로드 job 스크립트를 생성한다(실행하지 않음). GDC=gdc-client, SRA/ENA/GEO=nf-core/fetchngs,
    EGA=pyega3. controlled 데이터는 사용자 확인 후 acknowledge_controlled_access=True가 필요하다.
    nf_core_pipeline/download_method는 fetchngs 옵션(download_method: ftp|sratools|aspera|fastq-dl).
    use_dbgap=True: dbGaP 보호 SRA run을 config.dbgap_key_path(.ngc)로 받는다(controlled, 확인 필요)."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.prepare_download(cfg, db, query_id, dest=dest,
                                         acknowledge_controlled_access=acknowledge_controlled_access,
                                         nf_core_pipeline=nf_core_pipeline, download_method=download_method,
                                         use_dbgap=use_dbgap)

    return _run(go)


@tool(LOCAL_WRITE)
def verify_download(query_id: str, dest: Optional[str] = None, check_md5: bool = False) -> dict:
    """다운로드 결과를 manifest와 대조(파일 존재·크기, .partial 잔여). check_md5=True면 md5 재검증
    (용량이 크면 md5 검증 job 스크립트를 생성)."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.verify_download(cfg, db, query_id, dest=dest, check_md5=check_md5)

    return _run(go)


@tool(
    ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    meta={"anthropic/requiresUserInteraction": True},
)
def submit_job(script_path: str, confirm: bool = False, dry_run: bool = True) -> dict:
    """생성된 job 스크립트를 스케줄러에 제출. 기본값은 dry_run=True(제출하지 않고 명령만 보여줌).
    실제 제출 조건: confirm=True AND dry_run=False AND peer-reviewer PASS 기록(현재 해시와 일치)
    AND 동시 실행 한도 미만. 사용자의 명시적 승인 없이 confirm=True를 쓰지 말 것."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.submit_job(cfg, db, script_path, confirm=confirm, dry_run=dry_run)

    return _run(go)


@tool(READ_ONLY)
def job_status(job_id: str, log_lines: int = 30) -> dict:
    """job 상태(squeue/sacct, qstat, 또는 로컬 PID)와 로그 꼬리(민감 문자열 가림)를 반환."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.job_status(cfg, db, job_id, log_lines=log_lines)

    return _run(go)


@tool(READ_ONLY)
def list_jobs(
    status: Optional[str] = None,
    since_hours: Optional[float] = None,
    limit: int = 20,
    include_dry_run: bool = False,
) -> dict:
    """제출한 job 목록(상태 갱신 포함). since_hours=48이면 최근 48시간."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.list_jobs(cfg, db, status=status, since_hours=since_hours, limit=limit,
                                  include_dry_run=include_dry_run)

    return _run(go)


# ---------------------------------------------------------------- processing
@tool(LOCAL_WRITE)
def build_samplesheet(
    query_id: str,
    pipeline: str,
    dest: Optional[str] = None,
    strandedness: str = "auto",
    sample_column: str = "sample_accession",
    status_map: Optional[dict[str, int]] = None,
    sex_map: Optional[dict[str, str]] = None,
    patient_map: Optional[dict[str, str]] = None,
) -> dict:
    """다운로드된 파일로 nf-core samplesheet CSV 생성. pipeline='rnaseq'(sample,fastq_1,fastq_2,strandedness)
    또는 'sarek'(FASTQ: patient,sex,status,sample,lane,fastq_1,fastq_2 / GDC BAM: patient,sex,status,sample,bam,bai).
    status_map={'sample': 1}로 tumor 지정, sex_map 값은 XX|XY|NA."""

    def go() -> dict:
        cfg, db = _ctx()
        return processing.build_samplesheet(cfg, db, query_id, pipeline, dest=dest, strandedness=strandedness,
                                            sample_column=sample_column, status_map=status_map,
                                            sex_map=sex_map, patient_map=patient_map)

    return _run(go)


@tool(LOCAL_WRITE)
def prepare_pipeline(
    pipeline: str,
    samplesheet: str,
    version: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    outdir: Optional[str] = None,
    run_name: Optional[str] = None,
) -> dict:
    """nextflow run 스크립트(+params.yaml, 사이트 config)를 생성(실행하지 않음). 버전은 X.Y.Z로 고정
    (생략 시 config 값). params에 reference='<config.references 이름>' 또는 genome='<iGenomes 키>'를 지정."""

    def go() -> dict:
        cfg, db = _ctx()
        return processing.prepare_pipeline(cfg, db, pipeline, samplesheet, version=version, params=params,
                                           outdir=outdir, run_name=run_name)

    return _run(go)


# ---------------------------------------------------------------- review
@tool(READ_ONLY)
def preflight_check(script_path: str) -> dict:
    """[읽기 전용] 생성된 스크립트·samplesheet·파라미터 자동 점검: 레퍼런스 일치, paired/single, 디스크 여유,
    controlled-access 취급, 버전 고정, 무결성, 크리덴셜 노출. PASS/FAIL과 결함 목록 반환."""

    def go() -> dict:
        cfg, db = _ctx()
        return review.preflight_check(cfg, db, script_path)

    return _run(go)


@tool(LOCAL_WRITE)
def record_review(script_path: str, verdict: str, defects: Optional[list[str]] = None) -> dict:
    """peer-reviewer의 판정(PASS/FAIL)과 결함 목록을 감사 기록에 남긴다. 자동 점검이 FAIL이면 PASS를 줘도
    FAIL로 기록된다. 산출물 자체는 수정하지 않는다."""

    def go() -> dict:
        cfg, db = _ctx()
        return review.record_review(cfg, db, script_path, verdict, defects)

    return _run(go)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
