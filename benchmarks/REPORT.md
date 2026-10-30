# DataOps 팀과 일반 LLM 의 공공 데이터 다운로드 비교 (2026-10-30, cpm-server1)

## 논지 흐름

- 같은 요청 다섯 개에 대해 숫자 · 파일명 · 크기 · 체크섬을 실제 값으로 내놓은 쪽은 팀뿐이었음. 일반 LLM 은 기억만으로 답해 P1 의 "open access WGS BAM 은 0개" 하나를 맞혔고 나머지 넷에서는 파일 단위 사실을 내놓지 못했지만, 팀은 다섯 요청 모두 live API 조회 결과로 답함 → 지지
- 일반 LLM 이 준 명령을 고치지 않고 그대로 실행했을 때 원하는 파일을 다 받고 검증까지 끝난 것은 Zenodo 한 건이었음. GEO 는 wget 이 robots.txt 만 받고 끝났고, ENCODE 는 자리표시자 ENCFFxxxxxx 때문에 404 HTML 이 .bed.gz 이름으로 저장됐고, SRA 는 paired 파일명을 가정해 내려받기가 실패함. 팀은 다운로드 세 건(GEO 2개 1.35 MB, ENCODE 12개 95.87 MB, Zenodo 1개 433 KB)을 손대지 않고 받았고 크기와 md5 검증을 모두 통과함 → 지지
- 일반 LLM 은 SRR390278 을 SRR390728 의 오타로 의심했는데 이 의심은 틀렸음. 기억해 낸 SRR390728 정보(사람 RNA-seq, paired, 7,178,576 spots)는 ENA 조회값과 정확히 맞았지만, SRR390278 은 실제로 있는 다른 run(초파리 ChIP-seq, single-end, 17,978,113 reads)이었음 → 지지
- 이번 비교는 팀의 도구 경로(MCP 도구 함수, job 스크립트, 검증)만 시험했고 Claude 가 대화 중에 도구와 파라미터를 고르는 오케스트레이션은 시험하지 않았음. 검수 PASS 기록과 제출 승인도 벤치마크 스크립트가 자동으로 넣었음 → 판정보류

## 1. 같은 요청을 두 방식으로 처리해 결과가 실제 데이터와 맞는지 확인하기 위해 비교함

요청은 작고 공개된 데이터 다섯 개로 골랐음(P1 TCGA-BRCA open access WGS BAM 개수와 용량, P2 GEO GSE137303 count matrix, P3 ENCODE ENCSR356KRQ 의 GRCh38 peak bed 파일, P4 SRA SRR390278 메타데이터, P5 Zenodo 레코드 3490058 파일).

일반 LLM 쪽(arm B)은 같은 모델의 다른 세션에 도구와 인터넷 없이 평소 채팅처럼 답하게 한 것이고, 원문은 [`baseline_plain_llm.md`](baseline_plain_llm.md) 에 있음. 이 답변의 사실 주장은 팀 결과와 대조했고, 답변에 들어 있던 셸 명령은 cpm-server1 에서 고치지 않고 그대로 실행해 봄.

팀 쪽(arm A)은 [`live_comparison.py`](live_comparison.py) 가 팀의 실제 코드 경로를 그대로 돌린 것임. 순서는 live API 조회, manifest 저장, 다운로드 job 스크립트 생성, 자동 검수 기록, `submit_job` 기본 dry run 확인, `confirm=True` 제출(local runner, curl), 완료 대기, `verify_download(check_md5=True)` 이고, 결과는 [`results/2026-10-30_cpm-server1/team_results.json`](results/2026-10-30_cpm-server1/team_results.json) 에 있음. 전체 실행 시간은 3분 32초였고 그중 3분이 ENCODE 파일 95.87 MB 를 받는 데 쓰였음.

## 2. 요청별로 두 방식이 무엇을 내놓았고 실제 값은 무엇이었는지

| 요청 | 실제 값 (팀이 API 로 확인) | 일반 LLM 의 답 | 일반 LLM 명령을 그대로 실행한 결과 | 팀 다운로드 · 검증 |
|---|---|---|---|---|
| P1 TCGA-BRCA WGS BAM | open 0개. 전체 2,106개 · 536.67 TB · 952 cases, 전부 controlled | open 0개(맞음). 전체는 "수백\~1천여 개, 수백 TB" 로 추정 | API 조회 명령 정상 동작(open 0, controlled 2,106 · 536.67 TB) | 해당 없음(숫자만) |
| P2 GEO GSE137303 | 토마토(Solanum lycopersicum) southern tomato virus 연구. suppl 1개(GSE137303_count.txt.gz, 1,344,528 B) + series matrix 1개 | 연구 내용 · 파일명 · 크기 모름. RAW.tar 나 NCBI 생성 사람/마우스 count 를 가정 | wget -r 이 robots.txt 만 받음. NCBI count URL 은 0 B 파일. gzip 검사 실패 | 2개 1.35 MB, 크기 검증 PASS (GEO 는 체크섬 없음) |
| P3 ENCODE ENCSR356KRQ | GRCh38 bed peak 12개 95.87 MB, 전부 md5 있음. IDR thresholded 계열 bed 는 4개(3.6\~4.2 MB) | IDR peak 의 accession · 크기 · md5 모름. 크기는 "2\~5 MB" 로 추정 | 검색 명령은 정상 동작(bed 4개 · bigBed 4개와 md5). 다운로드 명령은 자리표시자라 404 HTML(23,563 B)을 .bed.gz 로 저장 | 12개 95.87 MB, md5 12/12 PASS |
| P4 SRA SRR390278 | 초파리 ChIP-seq, single-end, 17,978,113 reads, FASTQ 1개 447.47 MB | 메타데이터 모름. SRR390728 의 오타일 가능성을 제시 | ENA 조회 명령 정상 동작. FTP wget 은 _1/_2 파일을 찾아 둘 다 실패(single-end 파일은 따로 있음) | 해당 없음(숫자만) |
| P5 Zenodo 3490058 | 포스터 PDF 1개 433,244 B, md5 a1399249… | 파일명 · 크기 · md5 모름 | 정상 동작. 받은 뒤 md5 OK | 1개 433 KB, md5 PASS |

숫자를 맞힌 것은 일반 LLM 이 P1 에서 open access 가 0개라는 정책 지식 하나였고(실제 0), controlled 전체 규모 추정("1천여 개")은 실제 2,106개보다 작았음. P2 · P3 · P5 에서는 스스로 모른다고 밝히고 조회 명령을 대신 줬는데, 이것은 거짓 숫자를 만들지 않았다는 점에서 바람직한 태도로 볼 수 있음. 다만 그 명령을 돌려 실제 값을 확인하고 파일을 고르는 일은 사람에게 남음.

## 3. 일반 LLM 명령은 조회는 대체로 동작했고 다운로드에서 실패가 몰림

조회 명령 다섯 개(P1 GDC, P3 ENCODE 검색, P4 ENA filereport, P5 Zenodo, P2 디렉토리 목록)는 모두 그대로 동작했음. API 엔드포인트와 필드명에 대한 기억은 대부분 맞았다는 뜻임.

다운로드에서는 세 가지 실패가 나왔음.

(1) 조용한 실패(P2). `wget -r` 이 ftp.ncbi.nlm.nih.gov 의 robots.txt 를 따르느라 아무 파일도 받지 않았는데 종료 코드는 0 이었음. NCBI 생성 count URL 은 이 시리즈(토마토)에 없어서 0 B 파일이 남음 → 사람이 출력을 들여다보지 않으면 받았다고 착각할 수 있는 실패임.

(2) 손으로 채워야 하는 자리(P3). 다운로드 명령의 `ACC=ENCFFxxxxxx` 를 그대로 돌리면 `curl -L` 이 404 페이지를 `ENCFFxxxxxx.bed.gz` 로 저장하고 오류를 내지 않음(`--fail` 없음) → 검색 결과를 보고 accession 을 골라 넣는 단계가 사람 몫임.

(3) 틀린 전제(P4). SRR390278 을 paired 로 가정해 `_1`/`_2` 파일을 받으려 해 둘 다 실패함. single-end 일 때의 파일명도 주석으로 적어 두긴 했음 → 사람이 ENA 조회 결과의 layout 을 읽고 고르면 받을 수 있음.

팀은 같은 단계를 조회 결과에서 바로 만든 manifest 로 처리해 이 세 가지가 생기지 않았음. 받은 직후 스크립트가 파일 크기를 manifest 와 맞춰 보고, 체크섬이 있는 저장소(ENCODE, Zenodo)는 md5 까지 확인했음. GEO 는 저장소가 체크섬을 주지 않아 크기만 확인했고 보고서에도 그렇게 표시됨.

## 4. 오타 의심은 기억이 맞아도 판단이 틀릴 수 있다는 사례로 볼 수 있음

일반 LLM 은 SRR390278 을 모른다고 하면서 SRA Toolkit 예제로 유명한 SRR390728 을 떠올려 오타일 수 있다고 했음. ENA 로 SRR390728 을 조회하니 기억한 내용(사람, RNA-seq, paired, 7,178,576 spots)이 모두 맞았지만, SRR390278 은 실제로 존재하는 초파리 ChIP-seq run 이었음(nf-core/fetchngs 테스트 ID 목록에도 들어 있음). 사용자가 이 제안을 따랐다면 전혀 다른 데이터를 받았을 것으로 생각됨. 팀은 accession 을 조회해 실재 여부와 메타데이터를 먼저 보여 주므로 이런 갈래가 생기지 않음.

## 5. 팀 쪽에서 새로 확인된 것

- 이번이 팀 코드가 실제 API 에 처음 닿은 실행이었고, GDC · NCBI E-utilities · GEO FTP · ENA · ENCODE · Zenodo 클라이언트가 문서 기반으로 짠 그대로 동작했음(조회 0.86\~2.59초).
- P3 은 벤치마크 스크립트가 `file_format=bed` 만 걸어 IDR 외의 peak(pseudoreplicated, ranked 등)까지 12개 95.87 MB 를 받았음. 요청 뜻대로라면 `output_type` 을 함께 걸어 4개(약 15.7 MB)만 받았어야 함. 실제 사용에서는 encode-discovery agent 가 이 인자를 고르는 자리임.
- 로그 꼬리 마스킹이 40자 넘는 파일명(`Poster-Efficiently-starting-institutional-RDM.pdf`)을 토큰으로 오인해 `***REDACTED***` 로 가렸음. 다운로드에는 영향이 없지만 `security.redact` 의 기준을 좁힐 필요가 있음.

## 한계

- 요청 다섯 개, 실행 한 번이고 요청은 팀을 만든 쪽(Claude)이 골랐음.
- 일반 LLM 답변은 같은 모델의 한 번 응답이라 다른 모델이나 반복 응답에서는 달라질 수 있음.
- 팀은 MCP 도구 함수를 고정 인자로 직접 불렀고, 대화 중 Claude 가 subagent 와 인자를 고르는 단계와 Slurm 제출은 시험하지 않았음.
- 검수 PASS 와 `confirm=True` 는 스크립트가 넣었음. 실제 사용에서는 peer-reviewer 판정과 사용자 승인이 그 자리에 들어감.
- controlled 데이터 다운로드(GDC 토큰, EGA, dbGaP)와 ArrayExpress · CELLxGENE · cBioPortal · PRIDE 는 이번 비교에 넣지 않았음.
