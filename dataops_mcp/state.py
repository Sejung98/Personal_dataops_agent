"""SQLite 상태 저장소: 요청(query), 생성 산출물(artifact), 검수(review), job 기록.

모든 discovery 결과는 query_id로 저장되고, 이후 단계(전송·처리·검수·제출)는
query_id와 산출물의 sha256으로 서로 연결된다.

주의: SQLite는 NFS/Lustre 같은 공유 파일시스템에서 잠금 문제가 생길 수 있다.
가능하면 state_dir을 홈 디렉토리나 로컬 디스크에 둔다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    query_id     TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    api_calls_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    manifest_path TEXT,
    access       TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    path         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    query_id     TEXT,
    sha256       TEXT NOT NULL,
    meta_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    script_path  TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    checks_json  TEXT NOT NULL,
    defects_json TEXT NOT NULL,
    reviewer     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    scheduler_job_id TEXT,
    runner       TEXT NOT NULL,
    kind         TEXT,
    query_id     TEXT,
    script_path  TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    status       TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    log_path     TEXT,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id     TEXT NOT NULL,
    dest         TEXT NOT NULL,
    status       TEXT NOT NULL,
    report_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

ACTIVE_STATES = ("SUBMITTED", "PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "QUEUED", "HELD")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_query_id(source: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{source}-{stamp}-{secrets.token_hex(3)}"


class StateDB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path: Path = cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- queries -------------------------------------------------------
    def save_query(
        self,
        query_id: str,
        source: str,
        params: dict,
        api_calls: list[dict],
        summary: dict,
        manifest_path: str | None,
        access: str | None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?)",
                (
                    query_id,
                    source,
                    now_iso(),
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(api_calls, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    manifest_path,
                    access,
                ),
            )

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM queries WHERE query_id=?", (query_id,)).fetchone()
        if row is None:
            return None
        return {
            "query_id": row["query_id"],
            "source": row["source"],
            "created_at": row["created_at"],
            "params": json.loads(row["params_json"]),
            "api_calls": json.loads(row["api_calls_json"]),
            "summary": json.loads(row["summary_json"]),
            "manifest_path": row["manifest_path"],
            "access": row["access"],
        }

    def list_queries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT query_id, source, created_at, access, summary_json FROM queries "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "query_id": r["query_id"],
                "source": r["source"],
                "created_at": r["created_at"],
                "access": r["access"],
                "summary": json.loads(r["summary_json"]),
            }
            for r in rows
        ]

    # ---- artifacts -----------------------------------------------------
    def register_artifact(self, path: Path, kind: str, query_id: str | None, meta: dict) -> str:
        digest = sha256_file(path)
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?)",
                (str(path), kind, query_id, digest, json.dumps(meta, ensure_ascii=False), now_iso()),
            )
        return digest

    def get_artifact(self, path: str | Path) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM artifacts WHERE path=?", (str(path),)).fetchone()
        if row is None:
            return None
        return {
            "path": row["path"],
            "kind": row["kind"],
            "query_id": row["query_id"],
            "sha256": row["sha256"],
            "meta": json.loads(row["meta_json"]),
            "created_at": row["created_at"],
        }

    # ---- reviews -------------------------------------------------------
    def save_review(
        self,
        script_path: str,
        script_sha256: str,
        verdict: str,
        checks: list[dict],
        defects: list[str],
        reviewer: str,
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO reviews (script_path, script_sha256, verdict, checks_json, defects_json, "
                "reviewer, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    script_path,
                    script_sha256,
                    verdict,
                    json.dumps(checks, ensure_ascii=False),
                    json.dumps(defects, ensure_ascii=False),
                    reviewer,
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def latest_review(self, script_path: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM reviews WHERE script_path=? ORDER BY review_id DESC LIMIT 1",
                (script_path,),
            ).fetchone()
        if row is None:
            return None
        return {
            "review_id": row["review_id"],
            "script_sha256": row["script_sha256"],
            "verdict": row["verdict"],
            "checks": json.loads(row["checks_json"]),
            "defects": json.loads(row["defects_json"]),
            "reviewer": row["reviewer"],
            "created_at": row["created_at"],
        }

    # ---- jobs ----------------------------------------------------------
    def add_job(
        self,
        job_id: str,
        scheduler_job_id: str | None,
        runner: str,
        kind: str | None,
        query_id: str | None,
        script_path: str,
        script_sha256: str,
        status: str,
        log_path: str | None,
        note: str = "",
    ) -> None:
        ts = now_iso()
        with self.connect() as con:
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    scheduler_job_id,
                    runner,
                    kind,
                    query_id,
                    script_path,
                    script_sha256,
                    status,
                    ts,
                    ts,
                    log_path,
                    note,
                ),
            )

    def update_job_status(self, job_id: str, status: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (status, now_iso(), job_id)
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE job_id=? OR scheduler_job_id=? ORDER BY submitted_at DESC LIMIT 1",
                (job_id, job_id),
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(
        self,
        status: str | None = None,
        since: str | None = None,
        limit: int = 50,
        include_dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if since:
            sql += " AND submitted_at>=?"
            args.append(since)
        if not include_dry_run:
            sql += " AND status!='DRY_RUN'"
        sql += " ORDER BY submitted_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def count_active_jobs(self) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATES)
        with self.connect() as con:
            row = con.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({marks})", ACTIVE_STATES
            ).fetchone()
        return int(row["n"])

    # ---- verifications -------------------------------------------------
    def save_verification(self, query_id: str, dest: str, status: str, report: dict) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO verifications (query_id, dest, status, report_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (query_id, dest, status, json.dumps(report, ensure_ascii=False), now_iso()),
            )
