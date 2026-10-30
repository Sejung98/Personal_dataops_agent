"""지원 데이터베이스 카탈로그.

Orchestrator가 "어떤 데이터는 어디서 받나"를 판단하고 사용자에게 설명할 때 쓰는 정적 목록이다.
수치(파일 수·용량)는 여기 없다. 수치는 반드시 각 discovery 도구의 live 결과로만 말한다.
"""

from __future__ import annotations

from typing import Any

DATABASES: list[dict[str, Any]] = [
    {
        "id": "gdc", "name": "NCI Genomic Data Commons (GDC)",
        "domain": "cancer genomics (TCGA, TARGET, CPTAC, HCMI, ...)",
        "data": "aligned reads (BAM), somatic/germline variants (VCF/MAF), expression counts, methylation, CNV, clinical",
        "access": "open + controlled (dbGaP)", "tools": ["gdc_search_files", "gdc_search_cases", "gdc_get_field_values"],
        "download": "gdc-client", "subagent": "gdc-discovery", "nf_core": "sarek (BAM, step=variant_calling)",
    },
    {
        "id": "sra", "name": "SRA / ENA / DDBJ (INSDC)",
        "domain": "raw sequencing reads of any organism/assay",
        "data": "FASTQ per run (RNA-seq, WGS/WES, ChIP/ATAC, amplicon, metagenome, ...)",
        "access": "open", "tools": ["sra_search"], "download": "nf-core/fetchngs", "subagent": "sra-discovery",
        "nf_core": "rnaseq, sarek (and any FASTQ pipeline)",
    },
    {
        "id": "geo_raw", "name": "NCBI GEO -> SRA (raw reads)",
        "domain": "GEO series with sequencing data", "data": "GSE/GSM resolved to SRA runs -> FASTQ",
        "access": "open", "tools": ["sra_search"], "download": "nf-core/fetchngs", "subagent": "sra-discovery",
        "nf_core": "rnaseq, sarek",
    },
    {
        "id": "dbgap", "name": "dbGaP-protected SRA runs",
        "domain": "controlled human sequencing data hosted by NCBI SRA",
        "data": "FASTQ (via SRA Toolkit with a dbGaP repository key, .ngc)",
        "access": "controlled (dbGaP DAC)", "tools": ["sra_search"],
        "download": "nf-core/fetchngs --dbgap_key (sratools)", "subagent": "sra-discovery", "nf_core": "rnaseq, sarek",
    },
    {
        "id": "ega", "name": "European Genome-phenome Archive (EGA)",
        "domain": "controlled human genomic/phenotypic data", "data": "BAM/CRAM/VCF/FASTQ per dataset",
        "access": "controlled (study DAC)", "tools": ["ega_search"], "download": "pyega3", "subagent": "ega-discovery",
        "nf_core": "manual samplesheet",
    },
    {
        "id": "geo", "name": "NCBI GEO (processed / supplementary files)",
        "domain": "functional genomics, expression (incl. microarray)",
        "data": "series matrix, supplementary files (count matrices, RAW.tar, processed peaks, ...)",
        "access": "open", "tools": ["geo_files"], "download": "URL manifest (curl)", "subagent": "study-files-discovery",
        "nf_core": "-",
    },
    {
        "id": "arrayexpress", "name": "ArrayExpress (EMBL-EBI BioStudies)",
        "domain": "functional genomics", "data": "processed data files, SDRF/IDF metadata (raw reads live in ENA)",
        "access": "open", "tools": ["arrayexpress_files"], "download": "URL manifest (curl)",
        "subagent": "study-files-discovery", "nf_core": "raw reads via sra_search on the linked ENA study",
    },
    {
        "id": "encode", "name": "ENCODE portal",
        "domain": "regulatory genomics (human, mouse)",
        "data": "peaks (bed/bigBed), signal (bigWig), alignments, FASTQ, quantifications",
        "access": "open", "tools": ["encode_search", "encode_files"], "download": "URL manifest (curl)",
        "subagent": "encode-discovery", "nf_core": "-",
    },
    {
        "id": "cellxgene", "name": "CZ CELLxGENE Discover",
        "domain": "single-cell transcriptomics (curated, standardized)", "data": "AnnData .h5ad per dataset",
        "access": "open", "tools": ["cellxgene_search"], "download": "URL manifest (curl)",
        "subagent": "singlecell-discovery", "nf_core": "-",
    },
    {
        "id": "cbioportal", "name": "cBioPortal (datahub)",
        "domain": "processed cancer genomics", "data": "mutations, CNA, expression, methylation, clinical per study",
        "access": "open", "tools": ["cbioportal_search"], "download": "URL manifest (curl, study tarball)",
        "subagent": "cbioportal-discovery", "nf_core": "-",
    },
    {
        "id": "pride", "name": "PRIDE Archive (ProteomeXchange)",
        "domain": "mass-spectrometry proteomics", "data": "raw spectra, search results, peak lists",
        "access": "open", "tools": ["pride_files"], "download": "URL manifest (curl)",
        "subagent": "study-files-discovery", "nf_core": "-",
    },
    {
        "id": "zenodo", "name": "Zenodo",
        "domain": "any research output deposited with a DOI",
        "data": "processed matrices, models, supplementary data of papers", "access": "open (public records)",
        "tools": ["zenodo_files"], "download": "URL manifest (curl)", "subagent": "study-files-discovery",
        "nf_core": "-",
    },
    {
        "id": "url", "name": "Direct HTTPS URLs (long tail)",
        "domain": "reference genomes/annotations (Ensembl, GENCODE, UCSC), gnomAD, 1000 Genomes, ...",
        "data": "any public file reachable over HTTPS", "access": "open", "tools": ["url_inspect"],
        "download": "URL manifest (curl)", "subagent": "study-files-discovery", "nf_core": "-",
    },
]

NOT_SUPPORTED: list[dict[str, str]] = [
    {"name": "UK Biobank, All of Us, AnVIL/Terra", "reason": "controlled data analysed inside cloud enclaves; bulk download to on-prem HPC is not the intended access route"},
    {"name": "JGA (Japan), GSA/GSA-Human (China NGDC)", "reason": "separate access systems and no stable public programmatic API verified for this project; use url_inspect for public GSA files"},
    {"name": "ICGC (25K legacy / ARGO)", "reason": "the ICGC Data Portal closed in June 2024; legacy raw data is hosted mainly at EGA (use ega_search) and partly GDC; ARGO data needs DACO approval"},
    {"name": "Synapse-hosted consortia (e.g. AMP-AD)", "reason": "requires Synapse account/token and per-project terms; nf-core/fetchngs dropped Synapse support in 1.12.0"},
]


def list_databases() -> dict[str, Any]:
    return {"supported": DATABASES, "not_supported": NOT_SUPPORTED}
