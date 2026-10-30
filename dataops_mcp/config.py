"""config.yaml 로딩.

설정 파일 위치 결정 순서:
1. 환경변수 DATAOPS_CONFIG
2. <저장소 루트>/config.yaml

상대경로는 저장소 루트 기준으로 해석한다. `~`와 `$VAR`는 확장한다.
서버 이름, 사용자명, 절대경로는 코드에 두지 않고 모두 config에서 읽는다.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEDULERS = ("slurm", "pbs", "local")
TRANSFER_MODES = ("compute", "dtn", "login")
CONTAINERS = ("singularity", "apptainer", "docker")

DEFAULTS: dict[str, Any] = {
    "scheduler": "slurm",
    "partition": "",
    "account": "",
    "qos": "",
    "transfer_node_mode": "compute",
    "transfer_partition": "",
    "container": "singularity",
    "data_root": "./data",
    "work_dir": "./work",
    "state_dir": "./state",
    "singularity_cache": "./singularity_cache",
    "max_concurrent_jobs": 4,
    "disk_headroom": 1.5,
    "gdc_token_path": "~/.gdc/gdc-user-token.txt",
    "ega_credentials_path": "~/.ega/credentials.json",
    "dbgap_key_path": "",
    "module_loads": [],
    "tools": {
        "gdc_client": "gdc-client",
        "pyega3": "pyega3",
        "nextflow": "nextflow",
    },
    "transfer_job": {"cpus": 4, "mem": "8G", "time": "48:00:00", "connections": 8},
    "nextflow_head_job": {"cpus": 2, "mem": "8G", "time": "120:00:00"},
    "nextflow_extra_config": "",
    "md5_inline_limit_gb": 20,
    # 2026-10-30 기준 최신 릴리스. 세 파이프라인 모두 Nextflow >= 25.10.4 필요.
    # plugins: 오프라인 계산 노드용 사전 설치 목록(각 릴리스 nextflow.config 기준).
    "pipelines": {
        "fetchngs": {
            "version": "1.13.0",
            "plugins": ["nf-core-utils@0.5.0", "nf-schema@2.7.3"],
            "default_params": {},
        },
        "rnaseq": {
            "version": "3.27.0",
            "work_expansion": 3.0,
            "plugins": ["nf-schema@2.7.2"],
            "default_params": {},
        },
        "sarek": {
            "version": "3.11.0",
            "work_expansion": 4.0,
            "plugins": ["nf-core-utils@0.4.0", "nf-fgbio@1.0.0", "nf-prov@1.7.0", "nf-schema@2.7.2"],
            "default_params": {},
        },
    },
    # 레퍼런스: 이름 -> {build, species, fasta, gtf, ...}
    "references": {},
    "http": {
        "timeout": 60,
        "retries": 3,
        "contact_email": "",
        "ncbi_api_key_env": "NCBI_API_KEY",
    },
}

_PATH_KEYS = (
    "data_root",
    "work_dir",
    "state_dir",
    "singularity_cache",
    "gdc_token_path",
    "ega_credentials_path",
    "dbgap_key_path",
    "nextflow_extra_config",
)


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_path(value: str | os.PathLike | None, base: Path = REPO_ROOT) -> Path | None:
    if value in (None, ""):
        return None
    expanded = os.path.expanduser(os.path.expandvars(str(value)))
    p = Path(expanded)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


class Config:
    """설정값 접근 래퍼. `cfg["key"]` 또는 `cfg.path("key")`."""

    def __init__(self, data: dict[str, Any], source: Path | None, base: Path):
        self.data = data
        self.source = source
        self.base = base
        self._validate()

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def path(self, key: str) -> Path | None:
        return resolve_path(self.data.get(key), self.base)

    @property
    def state_dir(self) -> Path:
        return self.path("state_dir")  # type: ignore[return-value]

    @property
    def scripts_dir(self) -> Path:
        return self.state_dir / "scripts"

    @property
    def manifests_dir(self) -> Path:
        return self.state_dir / "manifests"

    @property
    def samplesheets_dir(self) -> Path:
        return self.state_dir / "samplesheets"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "dataops.sqlite"

    def pipeline(self, name: str) -> dict[str, Any]:
        return self.data.get("pipelines", {}).get(name, {})

    def transfer_runner(self) -> str:
        """전송 job을 어디서 실행할지: 'local'(로그인 노드) 또는 스케줄러 이름."""
        if self.data["transfer_node_mode"] == "login":
            return "local"
        return self.data["scheduler"]

    def compute_has_internet(self) -> bool:
        return self.data["transfer_node_mode"] == "compute"

    def _validate(self) -> None:
        d = self.data
        if d["scheduler"] not in SCHEDULERS:
            raise ConfigError(f"scheduler must be one of {SCHEDULERS}, got {d['scheduler']!r}")
        if d["transfer_node_mode"] not in TRANSFER_MODES:
            raise ConfigError(
                f"transfer_node_mode must be one of {TRANSFER_MODES}, got {d['transfer_node_mode']!r}"
            )
        if d["container"] not in CONTAINERS:
            raise ConfigError(f"container must be one of {CONTAINERS}, got {d['container']!r}")
        if d["transfer_node_mode"] == "dtn" and not d.get("transfer_partition"):
            raise ConfigError("transfer_node_mode=dtn requires transfer_partition")
        if int(d["max_concurrent_jobs"]) < 1:
            raise ConfigError("max_concurrent_jobs must be >= 1")

    def public_summary(self) -> dict[str, Any]:
        """도구 출력용 요약. 크리덴셜은 경로만 보여주고 내용은 절대 읽지 않는다."""
        keys = (
            "scheduler",
            "partition",
            "account",
            "transfer_node_mode",
            "transfer_partition",
            "container",
            "max_concurrent_jobs",
        )
        out = {k: self.data.get(k) for k in keys}
        for k in _PATH_KEYS:
            p = self.path(k)
            out[k] = str(p) if p else None
        out["pipelines"] = {k: v.get("version") for k, v in self.data.get("pipelines", {}).items()}
        out["references"] = sorted(self.data.get("references", {}).keys())
        out["config_file"] = str(self.source) if self.source else None
        return out


_CACHE: dict[str, Config] = {}


def load_config(path: str | os.PathLike | None = None, *, use_cache: bool = True) -> Config:
    if path is None:
        path = os.environ.get("DATAOPS_CONFIG") or (REPO_ROOT / "config.yaml")
    p = Path(path).expanduser()
    key = str(p.resolve()) if p.exists() else str(p)
    if use_cache and key in _CACHE:
        return _CACHE[key]
    user: dict[str, Any] = {}
    source: Path | None = None
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        source = p.resolve()
    data = _deep_merge(DEFAULTS, user)
    # 상대경로는 config 파일이 있는 디렉토리(=보통 저장소 루트) 기준
    base = source.parent if source else REPO_ROOT
    cfg = Config(data, source, base)
    _CACHE[key] = cfg
    return cfg


def clear_cache() -> None:
    _CACHE.clear()
