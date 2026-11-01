"""Restart only a newly created R7 PostgreSQL cluster; never the shared server."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from experiments.r4_profile import resources

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work/r7"
CLUSTER = WORK / "restart_cluster"
BIN = ROOT.parents[1] / "work/postgresql/pgsql/bin"
LOG = WORK / "logs"


def main():
    assert resources()["ram_available"] > 1024**3, "Isolated restart resource gate"
    assert not CLUSTER.exists()
    CLUSTER.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "RAILSTORM_DATABASE_URL": f"postgresql+psycopg://railstorm@127.0.0.1:{port}/postgres?connect_timeout=5",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    commands = []

    def run(label, arguments, timeout=60):
        with (LOG / (label + ".log")).open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                arguments, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout
            )
        commands.append(
            {"label": label, "command": [str(a) for a in arguments], "returncode": result.returncode}
        )
        (ROOT / "docs/r7_evidence/restart_commands.json").write_text(
            json.dumps(commands, indent=2), encoding="utf-8"
        )
        if result.returncode:
            print((LOG / (label + ".log")).read_text(encoding="utf-8")[-2500:])
            raise RuntimeError(label)
        print(json.dumps({"phase": label, "returncode": result.returncode}), flush=True)

    run(
        "restart_initdb",
        [
            str(BIN / "initdb.exe"),
            "-D",
            str(CLUSTER),
            "-U",
            "railstorm",
            "-A",
            "trust",
            "--no-locale",
            "-E",
            "UTF8",
        ],
    )
    start = [
        str(BIN / "pg_ctl.exe"),
        "start",
        "-D",
        str(CLUSTER),
        "-l",
        str(LOG / "restart_postgres.log"),
        "-o",
        f"-p {port} -h 127.0.0.1 -c fsync=on -c synchronous_commit=on",
        "-w",
        "-t",
        "30",
    ]
    stop = [str(BIN / "pg_ctl.exe"), "stop", "-D", str(CLUSTER), "-m", "fast", "-w", "-t", "30"]
    running = False
    try:
        run("restart_start_before", start)
        running = True
        run("restart_migrations", [sys.executable, "-m", "alembic", "upgrade", "head"])
        run("restart_before", [sys.executable, "-m", "experiments.r7_restart_worker", "before"])
        run("restart_real_stop", stop)
        running = False
        run("restart_start_after", start)
        running = True
        run("restart_after", [sys.executable, "-m", "experiments.r7_restart_worker", "after"])
    finally:
        if running:
            run("restart_final_stop", stop)


if __name__ == "__main__":
    main()
