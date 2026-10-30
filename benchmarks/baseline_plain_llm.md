# Baseline: plain chat LLM answers (no tools, no internet)

Collected 2026-10-30 from a separate session of the same model, instructed to answer from its own knowledge
only, as a normal chat assistant would. Audit: its only tool call was the harness hand-back that delivered this
text; no facts came from tools. Answers are reproduced verbatim (Korean).

---

## P1. TCGA-BRCA WGS BAM 중 open access 개수/용량

**결론: open access인 WGS BAM은 0개(0 TB)입니다.**

GDC 정책상 aligned reads(BAM) 파일은 전부 **controlled access**입니다. TCGA-BRCA의 WGS BAM도 모두 dbGaP 승인(phs000178)과 GDC 토큰이 있어야 받을 수 있습니다. open access로 받을 수 있는 건 MAF(masked somatic mutation), CNV segment, 발현량 같은 가공 데이터뿐입니다.

참고로 전체(controlled 포함) 규모는 제가 정확히 기억하지 못합니다. 대략 **수백~1천여 개 BAM, 수백 TB 규모**로 추정합니다(WGS BAM은 파일당 보통 50~150 GB). 정확한 숫자는 GDC API로 직접 확인하시는 걸 권합니다.

```bash
# 1) open access WGS BAM 개수/총용량(TB) 확인 → 0이 나와야 정상
curl -s -X POST https://api.gdc.cancer.gov/files \
  -H 'Content-Type: application/json' \
  -d '{
    "filters":{"op":"and","content":[
      {"op":"in","content":{"field":"cases.project.project_id","value":["TCGA-BRCA"]}},
      {"op":"in","content":{"field":"experimental_strategy","value":["WGS"]}},
      {"op":"in","content":{"field":"data_format","value":["BAM"]}},
      {"op":"in","content":{"field":"access","value":["open"]}}
    ]},
    "fields":"file_id,file_name,file_size,access",
    "size":"10000","format":"JSON"}' \
| jq '{n: .data.pagination.total, TB: ([.data.hits[].file_size] | add // 0) / 1e12}'

# 2) access를 "controlled"로 바꾸면 전체(controlled) 개수/용량 확인 가능

# 3) controlled 파일 다운로드(dbGaP 승인 + GDC 토큰 필요)
#    GDC Portal에서 manifest 받은 뒤:
gdc-client download -m gdc_manifest.txt -t gdc-user-token.txt -n 8
```

## P2. GEO GSE137303 count matrix(supplementary) 다운로드

솔직히 말씀드리면 GSE137303이 정확히 어떤 연구인지, supplementary 파일명과 크기가 무엇인지는 기억나지 않습니다. GEO의 일반 구조를 바탕으로 안내드리겠습니다.

- supplementary 경로: `https://ftp.ncbi.nlm.nih.gov/geo/series/GSE137nnn/GSE137303/suppl/`
- 흔한 파일명 패턴(추정): `GSE137303_RAW.tar`(샘플별 파일 묶음, 수십~수백 MB) 또는 `GSE137303_counts.txt.gz` / `GSE137303_raw_counts.csv.gz`(수 MB)
- 인간/마우스 bulk RNA-seq라면 NCBI가 생성한 count도 있을 수 있음: `GSE137303_raw_counts_GRCh38.p13_NCBI.tsv.gz`

```bash
# 1) 실제 파일 목록 확인(파일명/크기)
curl -s https://ftp.ncbi.nlm.nih.gov/geo/series/GSE137nnn/GSE137303/suppl/

# 2) suppl 폴더 전체 다운로드
mkdir -p GSE137303 && cd GSE137303
wget -r -np -nd -R "index.html*" \
  https://ftp.ncbi.nlm.nih.gov/geo/series/GSE137nnn/GSE137303/suppl/

# 3) (해당되는 경우) NCBI 생성 raw count
wget -O GSE137303_raw_counts_GRCh38.p13_NCBI.tsv.gz \
 "https://www.ncbi.nlm.nih.gov/geo/download/?type=rnaseq_counts&acc=GSE137303&format=file&file=GSE137303_raw_counts_GRCh38.p13_NCBI.tsv.gz"

# 4) 확인 (GEO는 md5를 제공하지 않으므로 무결성만 체크)
ls -lh
gzip -t *.gz && echo OK
tar -tvf GSE137303_RAW.tar | head        # RAW.tar가 있다면
zcat GSE137303_*count*.gz | head -3 | cut -c1-200
```

## P3. ENCODE ENCSR356KRQ (ATAC-seq) GRCh38 IDR thresholded peaks

ENCSR356KRQ는 ENCODE ATAC-seq 파이프라인(ENCODE-DCC/atac-seq-pipeline)의 **예제/테스트 데이터셋**으로 쓰이는 인간 ATAC-seq 실험입니다(제 기억으로는 keratinocyte 계열, 2 replicates, paired-end). 예제 JSON의 FASTQ accession(예: ENCFF341MYG, ENCFF106QGY, ENCFF248EJF, ENCFF368TYI 등)은 어느 정도 기억하지만, **IDR thresholded peak 파일의 accession·크기·md5는 기억하지 못합니다.** 보통 `ENCFFxxxxxx.bed.gz`(narrowPeak, 대략 2~5 MB)이고 bigBed 버전이 따로 있습니다. ENCODE4 재처리본은 output_type이 "conservative IDR thresholded peaks" / "optimal IDR thresholded peaks"로 나뉘어 있을 수 있습니다.

```bash
# 1) 해당 실험의 GRCh38 IDR peak 파일 목록(accession, 포맷, 크기, md5, 상태)
curl -s -H 'Accept: application/json' \
 'https://www.encodeproject.org/search/?type=File&dataset=/experiments/ENCSR356KRQ/&assembly=GRCh38&output_type=IDR+thresholded+peaks&output_type=conservative+IDR+thresholded+peaks&output_type=optimal+IDR+thresholded+peaks&status=released&frame=object&format=json&limit=all' \
| jq -r '.["@graph"][] | [.accession,.output_type,.file_format,.file_type,.file_size,.md5sum,.status,.href] | @tsv'

# 2) 다운로드 (ACC는 위 결과의 bed narrowPeak accession으로 교체)
ACC=ENCFFxxxxxx
curl -L -o ${ACC}.bed.gz https://www.encodeproject.org/files/${ACC}/@@download/${ACC}.bed.gz

# 3) md5 검증
EXPECTED=$(curl -s -H 'Accept: application/json' https://www.encodeproject.org/files/${ACC}/?format=json | jq -r .md5sum)
echo "${EXPECTED}  ${ACC}.bed.gz" | md5sum -c -
zcat ${ACC}.bed.gz | wc -l; zcat ${ACC}.bed.gz | head -3
```

## P4. SRA SRR390278 메타데이터 및 FASTQ 다운로드

먼저 확인할 점이 있습니다. **SRR390728**(숫자 순서 다름)은 SRA Toolkit 공식 문서 예제(`fastq-dump -X 5 -Z SRR390728`)로 유명한 run입니다. 제 기억으로는 인간(추정) Illumina **paired-end 36+36 bp**(fastq-dump 출력상 `length=72`), 약 **7.2M spots**(7,178,576?) 정도입니다. 혹시 SRR390728을 의도하신 건 아닌지 확인해 보세요.

**SRR390278 자체의 종·strategy·layout·read 수·FASTQ 크기는 기억하지 못합니다.** 아래로 확인하세요.

```bash
# 1) 메타데이터: ENA filereport (종, strategy, layout, read 수, FASTQ 크기/md5/URL)
curl -s "https://www.ebi.ac.uk/ena/portal/api/filereport?accession=SRR390278&result=read_run&fields=run_accession,scientific_name,library_strategy,library_layout,instrument_model,read_count,base_count,fastq_ftp,fastq_bytes,fastq_md5&format=tsv"

# 1') NCBI 쪽 (EDirect 설치 시)
esearch -db sra -query SRR390278 | efetch -format runinfo | cut -d, -f1,4,5,13,16,19,29,30

# 2) 다운로드 방법 A: SRA Toolkit
prefetch SRR390278
vdb-validate SRR390278
fasterq-dump --split-files --threads 8 SRR390278   # paired면 _1/_2 생성
pigz -p 8 SRR390278*.fastq

# 2) 다운로드 방법 B: ENA에서 gzip FASTQ 직접 (md5 제공)
wget ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR390/SRR390278/SRR390278_1.fastq.gz
wget ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR390/SRR390278/SRR390278_2.fastq.gz
#   (single-end면 SRR390278.fastq.gz 하나)

# 3) 확인
md5sum SRR390278*.fastq.gz      # ENA fastq_md5와 비교
echo $(( $(zcat SRR390278_1.fastq.gz | wc -l) / 4 ))   # read 수
```

## P5. Zenodo 레코드 3490058 파일 다운로드

**레코드 3490058에 들어 있는 파일명·크기·md5는 기억하지 못합니다.** Zenodo API로 바로 확인·다운로드할 수 있습니다.

```bash
# 1) 파일 목록(이름, 크기 bytes, md5, 다운로드 링크)
curl -s https://zenodo.org/api/records/3490058 \
 | jq -r '.metadata.title, (.files[] | [.key, .size, .checksum, .links.self] | @tsv)'

# 2) 전체 다운로드
mkdir -p zenodo_3490058 && cd zenodo_3490058
curl -s https://zenodo.org/api/records/3490058 \
 | jq -r '.files[] | .links.self + "\t" + .key' \
 | while IFS=$'\t' read url name; do wget -c -O "$name" "$url"; done
#   대안: pip install zenodo-get && zenodo_get 3490058

# 3) md5 검증 (Zenodo checksum은 "md5:xxxx" 형식)
curl -s https://zenodo.org/api/records/3490058 \
 | jq -r '.files[] | (.checksum|sub("md5:";"")) + "  " + .key' > md5sums.txt
md5sum -c md5sums.txt
```

## 자기평가표 (baseline 본인 작성)

| P | 비교적 확신 | 추측/모름 |
|---|---|---|
| P1 | GDC에서 BAM은 controlled → open WGS BAM은 0개/0 TB. GDC API 엔드포인트·필터 필드명, gdc-client 사용법, phs000178 | controlled 전체 개수·용량("수백~1천여 개, 수백 TB")은 근거 약한 추정 |
| P2 | GEO suppl 경로 패턴, wget 방법, NCBI 생성 count URL 형식, GEO는 md5 미제공 | 이 GSE의 내용, 실제 파일명(_RAW.tar인지 counts인지)과 크기는 전부 모름 |
| P3 | ENCSR356KRQ가 ENCODE ATAC 파이프라인 예제, ENCODE REST/@@download URL 형식, md5 검증 방법 | biosample은 반쯤 추측, FASTQ accession 일부 틀릴 수 있음, IDR peak의 accession·크기·md5는 모름 |
| P4 | SRR390728이 SRA Toolkit 예제, ENA filereport API, ENA FTP 경로 규칙, prefetch/fasterq-dump | SRR390278 메타데이터 전부 모름 |
| P5 | Zenodo REST 구조와 "md5:" 접두어, zenodo_get | 레코드 3490058의 제목·파일명·크기·md5 전부 모름 |
