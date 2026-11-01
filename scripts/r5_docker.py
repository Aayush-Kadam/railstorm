"""Operate only the generated isolated R5 Compose project, with bounded commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r5_evidence"
CONTEXT = json.loads((OUT / "context.json").read_text())
PROJECT = CONTEXT["project"]
assert PROJECT.startswith("railstorm-r5-") and len(PROJECT) == 21
ENV = {**os.environ, "RAILSTORM_PORT": str(CONTEXT["container_port"]), "COMPOSE_PARALLEL_LIMIT": "1"}
BASE = ["docker", "compose", "--progress", "plain", "-p", PROJECT]
LOG = ROOT / ".work/r5/logs"
LOG.mkdir(parents=True, exist_ok=True)


def command(label, args, timeout):
    with (LOG / (label + ".log")).open("w", encoding="utf-8") as file:
        try:
            result = subprocess.run(
                args, cwd=ROOT, env=ENV, stdout=file, stderr=subprocess.STDOUT, timeout=timeout
            )
            status = {"command": args, "returncode": result.returncode, "timeout": False}
        except subprocess.TimeoutExpired:
            status = {"command": args, "returncode": None, "timeout": True}
    records = (
        json.loads((OUT / "docker_commands.json").read_text())
        if (OUT / "docker_commands.json").exists()
        else {}
    )
    records[label] = status
    (OUT / "docker_commands.json").write_text(json.dumps(records, indent=2))
    print(json.dumps({"phase": label, **status}), flush=True)
    if status["returncode"] != 0:
        print((LOG / (label + ".log")).read_text(encoding="utf-8")[-3500:])
        raise SystemExit(1)


phase = sys.argv[1]
if phase == "build":
    command("build", [*BASE, "build"], 300)
elif phase == "up":
    command("up", [*BASE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120"], 150)
elif phase == "initial":
    command(
        "initial_smoke",
        [
            *BASE,
            "exec",
            "-T",
            "command-center",
            "python",
            "-m",
            "experiments.r5_smoke",
            "initial",
            "--checkpoint",
            "/app/docs/r5_container_smoke.json",
        ],
        120,
    )
elif phase == "restart":
    command("restart_down", [*BASE, "down"], 60)
    command("restart_up", [*BASE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120"], 150)
    command(
        "restart_smoke",
        [
            *BASE,
            "exec",
            "-T",
            "command-center",
            "python",
            "-m",
            "experiments.r5_smoke",
            "restart",
            "--restart-scope",
            "stack",
            "--checkpoint",
            "/app/docs/r5_container_smoke.json",
        ],
        120,
    )
elif phase == "collect":
    result = subprocess.run(
        [*BASE, "exec", "-T", "command-center", "cat", "/app/docs/r5_container_smoke.json"],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["phase"] == "RESTART_PASS"
    (OUT / "container_smoke.json").write_text(json.dumps(report, indent=2))
    command("container_logs", [*BASE, "logs", "--no-color"], 30)
    command("container_images", [*BASE, "images", "--format", "json"], 30)
    command("container_python", [*BASE, "exec", "-T", "command-center", "python", "--version"], 15)
elif phase == "down":
    command("final_down", [*BASE, "down"], 60)
else:
    raise ValueError("Unsupported phase")
