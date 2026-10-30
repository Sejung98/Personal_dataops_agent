"""크리덴셜 취급 규칙.

- 토큰/크리덴셜 파일은 **경로만** 다루고 내용은 절대 읽지 않는다.
- 파일 권한이 600(소유자 전용)이 아니면 경고한다.
- 도구 출력으로 내보내는 로그 꼬리(tail)에서는 토큰처럼 보이는 문자열을 가린다.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

# GDC 토큰은 수백 자 길이의 base64url 계열 문자열이다. 40자 이상 연속 문자열을 가린다.
_TOKENISH = re.compile(r"[A-Za-z0-9_\-\.=+/]{40,}")
_KEYVAL = re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*)(\S+)")


def check_secret_file(path: str | os.PathLike | None, label: str) -> dict[str, Any]:
    """크리덴셜 파일 상태 점검. 내용을 열지 않는다."""
    if not path:
        return {"label": label, "path": None, "exists": False, "ok": False,
                "warning": f"{label} 경로가 config에 없습니다."}
    p = Path(path)
    if not p.exists():
        return {"label": label, "path": str(p), "exists": False, "ok": False,
                "warning": f"{label} 파일이 없습니다: {p}"}
    mode = stat.S_IMODE(p.stat().st_mode)
    out: dict[str, Any] = {"label": label, "path": str(p), "exists": True, "mode": oct(mode)}
    if mode & 0o077:
        out["ok"] = False
        out["warning"] = (
            f"{label} 파일 권한이 {oct(mode)} 입니다. 소유자 전용이어야 합니다: chmod 600 {p}"
        )
    else:
        out["ok"] = True
    return out


def nearest_existing(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def dir_exposure(path: Path) -> dict[str, Any]:
    """대상 디렉토리(또는 가장 가까운 존재하는 상위)의 group/other 접근 권한."""
    p = nearest_existing(path)
    mode = stat.S_IMODE(p.stat().st_mode)
    return {
        "checked_path": str(p),
        "mode": oct(mode),
        "world_accessible": bool(mode & 0o007),
        "group_accessible": bool(mode & 0o070),
    }


def redact(text: str) -> str:
    text = _KEYVAL.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _TOKENISH.sub("***REDACTED***", text)


def script_has_inline_secret(text: str, secret_paths: list[Path]) -> bool:
    """스크립트에 토큰/비밀번호 값이 직접 들어있는지 검사. 크리덴셜은 파일 경로로만 전달해야 한다."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "X-Auth-Token" in stripped:
            return True
        if _KEYVAL.search(stripped) and not any(str(p) in stripped for p in secret_paths):
            return True
    return False
