"""Read-only host, PostgreSQL and Docker/WSL headroom snapshot."""

import ctypes
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from sqlalchemy import text

from experiments.r4_profile import resources
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"


def cpu_ticks():
    values = [wintypes.FILETIME() for _ in range(3)]
    assert ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(v) for v in values))
    return [v.dwHighDateTime * 2**32 + v.dwLowDateTime for v in values]


def bounded(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()[-500:],
        }
    except (subprocess.TimeoutExpired, OSError) as error:
        return {"status": "UNAVAILABLE", "error": type(error).__name__}


def main():
    name = sys.argv[1]
    path = OUT / (name + ".json")
    assert not path.exists()
    before = cpu_ticks()
    readings = [resources()]
    for _ in range(3):
        time.sleep(0.5)
        readings.append(resources())
    after = cpu_ticks()
    total = after[1] + after[2] - before[1] - before[2]
    utilization = 100 * (1 - (after[0] - before[0]) / total)
    processes = bounded(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process postgres,vmmem*,*docker* -ErrorAction SilentlyContinue | Select-Object ProcessName,CPU,WorkingSet64,PeakWorkingSet64 | ConvertTo-Json -Compress",
        ]
    )
    docker = bounded(["docker", "info", "--format", "{{json .MemTotal}} {{json .NCPU}} {{json .OSType}}"])
    wsl = (
        bounded(["wsl", "-d", "docker-desktop", "--exec", "cat", "/proc/meminfo"])
        if docker.get("returncode") == 0
        else {
            "status": "UNVERIFIED",
            "reason": "Docker engine unavailable; did not start unrelated infrastructure",
        }
    )
    base = Store()
    context = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    isolated = Store(base.engine.url.set(database=context["database"]).render_as_string(hide_password=False))
    with isolated.engine.connect() as conn:
        settings = {
            key: conn.exec_driver_sql("SHOW " + key).scalar_one()
            for key in (
                "server_version",
                "fsync",
                "synchronous_commit",
                "shared_buffers",
                "max_connections",
                "shared_preload_libraries",
                "track_io_timing",
            )
        }
        waits = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT wait_event_type,wait_event,state,count(*) n FROM pg_stat_activity WHERE datname=current_database() GROUP BY 1,2,3"
                )
            ).mappings()
        ]
        extensions = [r[0] for r in conn.exec_driver_sql("SELECT extname FROM pg_extension")]
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "versions": {
            n: importlib.metadata.version(n) for n in ("SQLAlchemy", "psycopg", "alembic", "pytest")
        },
        "resources": readings,
        "host_cpu_percent_1_5s": utilization,
        "server_and_docker_processes": processes,
        "docker_memory_total_ncpu_os": docker,
        "docker_wsl_meminfo": wsl,
        "database_settings": settings,
        "current_database_waits": waits,
        "extensions": extensions,
        "major_run_gate": "2GiB available host RAM,3GiB free disk; sample100 requires768MiB; stop at512MiB after committed batch",
        "configuration_changes": "None; pg_stat_statements unavailable if not installed/preloaded; no global config/restart to enable profiling",
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "minimum_free_ram": min(r["ram_available"] for r in readings),
                "host_cpu_percent": utilization,
                "docker_info_returncode": docker.get("returncode"),
                "extensions": extensions,
            }
        )
    )
    isolated.engine.dispose()
    base.engine.dispose()


if __name__ == "__main__":
    main()
