"""스케줄러 추상화: slurm / pbs(PBS Pro·OpenPBS) / local(로그인 노드 백그라운드 실행).

LLM은 스크립트를 '생성'만 하고, 실제 실행은 이 모듈이 sbatch/qsub/bash로 한다.
제출 가능 여부(승인·검수·dry_run)는 transfer.submit_job이 판단한다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class Resources:
    cpus: int = 1
    mem: str = "4G"
    time: str = "24:00:00"
    partition: str = ""
    account: str = ""
    qos: str = ""


def _mem_amount(mem: str) -> tuple[int, str]:
    m = re.fullmatch(r"\s*(\d+)\s*([KMGTkmgt])?[Bb]?\s*", str(mem))
    if not m:
        raise ValueError(f"메모리 형식을 해석할 수 없습니다: {mem!r} (예: 8G, 16000M)")
    return int(m.group(1)), (m.group(2) or "M").upper()


def nf_memory(mem: str) -> str:
    """'8G' -> '8 GB' (Nextflow 메모리 표기)."""
    amount, unit = _mem_amount(mem)
    return f"{amount} {unit}B"


def safe_job_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "dataops"


def directives(runner: str, job_name: str, res: Resources, log_path: Path) -> list[str]:
    name = safe_job_name(job_name)
    if runner == "slurm":
        amount, unit = _mem_amount(res.mem)
        lines = [
            f"#SBATCH --job-name={name}",
            f"#SBATCH --output={log_path}",
            f"#SBATCH --error={log_path}",
            f"#SBATCH --cpus-per-task={res.cpus}",
            f"#SBATCH --mem={amount}{unit}",
            f"#SBATCH --time={res.time}",
        ]
        if res.partition:
            lines.append(f"#SBATCH --partition={res.partition}")
        if res.account:
            lines.append(f"#SBATCH --account={res.account}")
        if res.qos:
            lines.append(f"#SBATCH --qos={res.qos}")
        return lines
    if runner == "pbs":
        amount, unit = _mem_amount(res.mem)
        lines = [
            f"#PBS -N {name}",
            f"#PBS -o {log_path}",
            "#PBS -j oe",
            f"#PBS -l select=1:ncpus={res.cpus}:mem={amount}{unit.lower()}b",
            f"#PBS -l walltime={res.time}",
        ]
        if res.partition:
            lines.append(f"#PBS -q {res.partition}")
        if res.account:
            lines.append(f"#PBS -A {res.account}")
        return lines
    if runner == "local":
        return [f"# runner=local (로그인/전송 노드에서 nohup 실행). log: {log_path}"]
    raise ValueError(f"unknown runner {runner!r}")


def submit_command(runner: str, script: Path) -> list[str]:
    if runner == "slurm":
        return ["sbatch", "--parsable", str(script)]
    if runner == "pbs":
        return ["qsub", str(script)]
    if runner == "local":
        return ["bash", str(script)]
    raise ValueError(f"unknown runner {runner!r}")


def exit_file_for(script: Path) -> Path:
    return script.with_suffix(script.suffix + ".exitcode")


def submit(runner: str, script: Path, log_path: Path, run: Runner = subprocess.run) -> str:
    """실제 제출. 스케줄러 job id(local은 PID)를 반환."""
    cmd = submit_command(runner, script)
    if runner == "local":
        log_path.parent.mkdir(parents=True, exist_ok=True)
        exit_file_for(script).unlink(missing_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                ["nohup", *cmd], stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        return str(proc.pid)
    exit_file_for(script).unlink(missing_ok=True)
    proc = _try(run, cmd)
    if proc is None:
        raise RuntimeError(f"`{cmd[0]}`를 찾을 수 없습니다. 이 노드에서 스케줄러를 사용할 수 있는지 확인하세요.")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 실패 (rc={proc.returncode}): {proc.stderr.strip()[:500]}")
    out = proc.stdout.strip()
    if runner == "slurm":
        return out.split(";")[0]
    return out.splitlines()[0].strip() if out else ""


_PBS_STATE = {
    "Q": "QUEUED",
    "R": "RUNNING",
    "H": "HELD",
    "E": "COMPLETING",
    "W": "QUEUED",
    "B": "RUNNING",
    "C": "COMPLETED",
}


def _status_from_exit_file(script: Path | None) -> str | None:
    if script is None:
        return None
    f = exit_file_for(script)
    if not f.exists():
        return None
    try:
        rc = int(f.read_text().strip() or "1")
    except ValueError:
        return "FAILED"
    return "COMPLETED" if rc == 0 else "FAILED"


def _try(run: Runner, cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def query_status(
    runner: str, job_id: str, script: Path | None = None, run: Runner = subprocess.run
) -> str:
    """스케줄러 상태를 정규화된 문자열로 반환 (PENDING/RUNNING/COMPLETED/FAILED/... /UNKNOWN)."""
    if runner == "slurm":
        p = _try(run, ["squeue", "-h", "-j", job_id, "-o", "%T"])
        if p is not None and p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip().splitlines()[0].strip().upper()
        p = _try(run, ["sacct", "-n", "-P", "-X", "-j", job_id, "-o", "State"])
        if p is not None and p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip().splitlines()[0].split()[0].upper()
        return _status_from_exit_file(script) or "UNKNOWN"
    if runner == "pbs":
        for cmd in (["qstat", "-x", "-f", job_id], ["qstat", "-f", job_id]):
            p = _try(run, cmd)
            if p is None or p.returncode != 0:
                continue
            m = re.search(r"job_state\s*=\s*(\w)", p.stdout)
            if not m:
                continue
            code = m.group(1)
            if code == "F":
                e = re.search(r"Exit_status\s*=\s*(-?\d+)", p.stdout)
                return "COMPLETED" if e and int(e.group(1)) == 0 else "FAILED"
            return _PBS_STATE.get(code, code)
        return _status_from_exit_file(script) or "UNKNOWN"
    if runner == "local":
        done = _status_from_exit_file(script)
        if done:
            return done
        try:
            os.kill(int(job_id), 0)
        except (ProcessLookupError, ValueError):
            return "UNKNOWN"
        except PermissionError:
            return "RUNNING"
        return "RUNNING"
    raise ValueError(f"unknown runner {runner!r}")


def available_scheduler_tools() -> dict[str, str | None]:
    return {t: shutil.which(t) for t in ("sbatch", "squeue", "sacct", "qsub", "qstat")}
