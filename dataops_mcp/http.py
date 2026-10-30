"""공용 HTTP 클라이언트: 재시도와 API 호출 기록(provenance).

discovery 도구는 모든 API 호출을 `ApiLog`에 남기고, 그 기록을 결과와 함께 반환한다.
에이전트가 "근거가 된 API 쿼리"를 보고할 수 있게 하기 위함이다.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from . import __version__
from .config import Config

RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    pass


class ApiLog:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, params: dict | None, body: Any, status: int | None) -> None:
        entry: dict[str, Any] = {"method": method, "url": url, "status": status}
        if params:
            entry["params"] = params
        if body is not None:
            entry["json"] = body
        self.calls.append(entry)


def make_client(cfg: Config) -> httpx.Client:
    http = cfg.get("http", {})
    ua = f"genomics-dataops-team/{__version__}"
    if http.get("contact_email"):
        ua += f" ({http['contact_email']})"
    return httpx.Client(
        timeout=float(http.get("timeout", 60)),
        headers={"User-Agent": ua},
        follow_redirects=True,
    )


def request(
    client: httpx.Client,
    log: ApiLog,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: Any = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    ok_status: tuple[int, ...] = (200,),
) -> httpx.Response:
    """재시도(429/5xx/네트워크 오류, 지수 백오프) 포함 요청. 실패 시 ApiError."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, params=params, json=json)
        except httpx.TransportError as exc:
            last_exc = exc
            log.add(method, url, params, json, None)
        else:
            log.add(method, url, params, json, resp.status_code)
            if resp.status_code in ok_status:
                return resp
            if resp.status_code not in RETRY_STATUS:
                raise ApiError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            last_exc = ApiError(f"{method} {url} -> HTTP {resp.status_code}")
        if attempt < retries:
            sleep(min(2 ** attempt, 30))
    raise ApiError(f"{method} {url} failed after {retries + 1} attempts: {last_exc}")
