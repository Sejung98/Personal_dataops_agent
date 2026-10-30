# Team vs plain LLM: live download comparison

Goal: on the same small, real requests, compare what a plain chat LLM produces with what the DataOps team
produces and actually downloads.

| ID | Request | Expected download |
|---|---|---|
| P1 | TCGA-BRCA WGS BAM, open access: how many, how many TB | none (numbers only) |
| P2 | GEO GSE137303 count matrix (supplementary) | ~1 MB |
| P3 | ENCODE ENCSR356KRQ (ATAC-seq) GRCh38 peak files (bed) | a few MB |
| P4 | SRA SRR390278 metadata (species, strategy, layout, reads, FASTQ size) | none (numbers only) |
| P5 | Zenodo record 3490058 files | ~0.4 MB |

Arms:
- **A. Team**: `live_comparison.py` runs the team's real code path: live discovery → generated job → automatic
  review → confirmed `submit_job` (local runner) → curl download → `verify_download(check_md5=True)`.
- **B. Plain chat LLM**: the same model answering from memory with no tools or internet
  (`baseline_plain_llm.md`, collected 2026-10-30; its only tool call was the harness hand-back). Its factual
  claims are scored against arm A's live results, and its suggested commands are run as written.

Scored: correct numbers and file names, whether the download worked without manual edits, integrity checking,
provenance (query, sizes, checksums recorded), and safety (approval before execution, controlled-data handling).

Run (needs access to api.gdc.cancer.gov, eutils.ncbi.nlm.nih.gov, ftp.ncbi.nlm.nih.gov, www.ebi.ac.uk,
www.encodeproject.org and its S3 redirect target, zenodo.org):

    .venv/bin/python benchmarks/live_comparison.py --out benchmarks/results

## Status (2026-10-30)

| Part | State |
|---|---|
| Plain-LLM answers (arm B) | Collected: [`baseline_plain_llm.md`](baseline_plain_llm.md) |
| Team run (arm A) | Done on cpm-server1 (2026-10-30): [`results/2026-10-30_cpm-server1/`](results/2026-10-30_cpm-server1/). The earlier sandbox attempt (all proxy 403) is kept in [`results/2026-10-30_cloud-sandbox_blocked/`](results/2026-10-30_cloud-sandbox_blocked/). |
| Comparison report | [`REPORT.md`](REPORT.md) (Korean) |

## Folder layout

```
benchmarks/
├── README.md                    # protocol and status (this file)
├── REPORT.md                    # comparison report
├── live_comparison.py           # arm A runner (team code path, local runner, real downloads)
├── baseline_plain_llm.md        # arm B answers, verbatim
└── results/
    └── <date>_<where>/team_results.json   # one folder per run
```

After a successful run, add it as `results/<date>_<host>/team_results.json`.
