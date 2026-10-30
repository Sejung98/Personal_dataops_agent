#!/usr/bin/env bash
# genomics-dataops-team 설치 스크립트
#   ./install.sh               # 기본 설치
#   PYTHON=python3.11 ./install.sh
#   ./install.sh --run-tests   # 설치 후 단위 테스트 실행
# 외부 도구(gdc-client, nextflow, pyega3 등)는 자동 설치하지 않고 점검·안내만 한다.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYTHON="${PYTHON:-python3}"
RUN_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --run-tests) RUN_TESTS=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 1. Python >= 3.10
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python을 찾을 수 없습니다: $PYTHON  (예: module load python/3.11 후 다시 실행)" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 이상이 필요합니다 (현재: $("$PYTHON" -V 2>&1))." >&2
  echo "예: module load python/3.11 && PYTHON=python3.11 ./install.sh  또는 conda/uv로 3.10+ 준비" >&2
  exit 1
fi
ok "Python: $("$PYTHON" -V 2>&1)"

# ---------------------------------------------------------------- 2. venv + 의존성
if [ ! -x .venv/bin/python ]; then
  say ".venv 생성"
  "$PYTHON" -m venv .venv
fi
say "의존성 설치 (mcp, httpx, pyyaml, jinja2, pytest, respx)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[test]"
ok "패키지 설치 완료"

# ---------------------------------------------------------------- 3. config.yaml
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  chmod 600 config.yaml
  ok "config.yaml 생성 (config.example.yaml 복사). 반드시 편집하세요."
else
  ok "기존 config.yaml 유지"
fi
STATE_DIR="$(.venv/bin/python -c 'from dataops_mcp.config import load_config; print(load_config().state_dir)')"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
ok "state_dir: $STATE_DIR (권한 700)"

# ---------------------------------------------------------------- 4. .mcp.json 점검
if ! .venv/bin/python - <<'PY'
import json, sys
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["dataops"]
ok = srv["command"].endswith("/.venv/bin/python") and srv["args"][-1] == "dataops_mcp.server"
sys.exit(0 if ok else 1)
PY
then
  warn ".mcp.json의 dataops 서버가 .venv/bin/python을 가리키지 않습니다. 저장소의 기본 .mcp.json으로 되돌리세요."
fi
if .venv/bin/python -c 'import asyncio; from dataops_mcp import server; print(len(asyncio.run(server.server.list_tools())))' >/tmp/dataops_tools.$$ 2>&1; then
  ok "MCP 서버 로드 확인: 도구 $(cat /tmp/dataops_tools.$$)개 (.mcp.json -> .venv/bin/python -m dataops_mcp.server)"
else
  warn "MCP 서버 로드 실패:"; cat /tmp/dataops_tools.$$
fi
rm -f /tmp/dataops_tools.$$

# ---------------------------------------------------------------- 5. 외부 도구 점검 (설치는 안내만)
check_tool() {
  local name="$1" hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name: $(command -v "$name")"
  else
    warn "$name 없음 → $hint"
  fi
}
say "외부 도구 점검 (자동 설치하지 않음)"
check_tool claude      "Claude Code 설치: https://code.claude.com/docs (npm install -g @anthropic-ai/claude-code 등)"
check_tool gdc-client  "GDC Data Transfer Tool: https://gdc.cancer.gov/access-data/gdc-data-transfer-tool 에서 바이너리를 받아 PATH에 두거나 config.yaml tools.gdc_client에 경로 지정"
check_tool nextflow    "Nextflow >= 25.10.4 필요: curl -s https://get.nextflow.io | bash (Java 17+ 필요) 또는 module load nextflow"
check_tool java        "Java 17 이상 (Nextflow용): module load java/17 등"
check_tool pyega3      "EGA 다운로드 시에만 필요: pip install pyega3 (별도 환경 권장) 또는 conda install -c bioconda pyega3"
check_tool nf-core     "오프라인 계산 노드에서 컨테이너 사전 다운로드 시 필요: pip install nf-core"
if command -v sbatch >/dev/null 2>&1; then ok "sbatch: $(command -v sbatch)";
elif command -v qsub >/dev/null 2>&1; then ok "qsub: $(command -v qsub)";
else warn "sbatch/qsub 없음 → 스케줄러가 없으면 config.yaml에서 scheduler: local"; fi
if command -v apptainer >/dev/null 2>&1; then ok "apptainer: $(command -v apptainer)";
elif command -v singularity >/dev/null 2>&1; then ok "singularity: $(command -v singularity)";
else warn "singularity/apptainer 없음 → module load singularity 또는 config.yaml container 값 확인"; fi

# ---------------------------------------------------------------- 6. 크리덴셜 권한
.venv/bin/python - <<'PY'
from dataops_mcp.config import load_config
from dataops_mcp.security import check_secret_file
cfg = load_config()
for key, label in (("gdc_token_path", "GDC token"), ("ega_credentials_path", "EGA credentials")):
    r = check_secret_file(cfg.path(key), label)
    if r["exists"]:
        print(("[ok] " if r["ok"] else "[warn] ") + f"{label}: {r['path']} ({r['mode']})" + ("" if r["ok"] else " -> " + r["warning"]))
    else:
        print(f"[info] {label}: 아직 없음 ({r['path']}) - controlled 데이터를 받을 때만 필요")
PY

# ---------------------------------------------------------------- 7. 테스트 (선택)
if [ "$RUN_TESTS" = 1 ]; then
  say "단위 테스트 실행"
  .venv/bin/python -m pytest -q
fi

cat <<EOF

다음 단계
  1) config.yaml 편집: partition, account, transfer_node_mode, data_root, work_dir, singularity_cache, references
  2) 계산 노드 인터넷 확인: srun --pty curl -sI https://api.gdc.cancer.gov/status  (또는 qsub -I)
  3) 이 폴더에서 claude 실행:  cd "$ROOT" && claude
     첫 실행 시 폴더 신뢰(trust)와 dataops MCP 서버 사용을 허용하세요.
EOF
