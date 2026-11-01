"""Operate only the generated isolated R6 Compose project, with bounded commands."""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from experiments.r4_profile import resources

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"
CONTEXT = json.loads((OUT / "context.json").read_text())
PROJECT = CONTEXT["project"]
assert PROJECT.startswith("railstorm-r6-") and len(PROJECT) == 21
ENV = {**os.environ, "RAILSTORM_PORT": str(CONTEXT["container_port"]), "COMPOSE_PARALLEL_LIMIT": "1"}
BASE = ["docker", "compose", "--progress", "plain", "-p", PROJECT]
LOG = ROOT / ".work/r6/logs"
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
if phase in {"build", "up", "initial", "restart"}:
    assert resources()["ram_available"] > 1536 * 1024**2, "Container resource gate"
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
            "/app/docs/r6_container_smoke.json",
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
            "/app/docs/r6_container_smoke.json",
        ],
        120,
    )
elif phase == "collect":
    result = subprocess.run(
        [*BASE, "exec", "-T", "command-center", "cat", "/app/docs/r6_container_smoke.json"],
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
    runtime = subprocess.run(
        [
            *BASE,
            "exec",
            "-T",
            "command-center",
            "python",
            "-c",
            "import hashlib,json,pathlib; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ['railstorm','migrations'] for p in pathlib.Path(folder).rglob('*.py')}))",
        ],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    hashes = json.loads(runtime.stdout)
    for name, sha in hashes.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
    (OUT / "container_source_sha256.json").write_text(json.dumps(hashes, indent=2))
elif phase == "down":
    command("final_down", [*BASE, "down"], 60)
elif phase == "host":
    base_url = "http://127.0.0.1:" + str(CONTEXT["container_port"])

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            base_url + path, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    health = request("/api/v1/health")
    assert health["status"] == "OK"
    session = request("/api/v1/live-crisis/sessions", {"scenario": "atomic", "seed": 20261031})
    run = session["session"]["id"]
    for _ in range(4):
        session = request(f"/api/v1/live-crisis/sessions/{run}/actions", {"action": "ADVANCE"})
        if session["financial"]["payments"]["payment"]["economic_status"] == "SETTLED":
            break
    assert session["financial"]["payments"]["payment"]["economic_status"] == "SETTLED"
    for action in ("AUDIT", "REPLAY"):
        session = request(f"/api/v1/live-crisis/sessions/{run}/actions", {"action": action})
    (OUT / "container_host_ingress.json").write_text(
        json.dumps({"health": health, "session": session}, indent=2)
    )
    print(json.dumps({"host_loopback_container_ingress": "PASS"}))
else:
    raise ValueError("Unsupported phase")
