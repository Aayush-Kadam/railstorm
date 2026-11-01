"""Create a fresh isolated source installation, never copying installed dependencies."""

import hashlib
import json
import shutil
import socket
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".work/r5/source"
if target.exists():
    raise FileExistsError("Preserve existing installation attempt")
target.mkdir(parents=True)
for folder in ("railstorm", "migrations", "scenarios", "tests", "experiments", "scripts"):
    shutil.copytree(
        root / folder,
        target / folder,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "reports", "work"),
    )
(target / "frontend").mkdir()
shutil.copytree(root / "frontend/src", target / "frontend/src")
for name in (
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "vite.config.ts",
    "index.html",
    "r3.e2e.mjs",
):
    shutil.copyfile(root / "frontend" / name, target / "frontend" / name)
for name in (
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "Dockerfile",
    "docker-compose.yml",
    "README.md",
    "LICENSE",
    ".dockerignore",
):
    shutil.copyfile(root / name, target / name)
(target / "docs/r4_evidence").mkdir(parents=True)
for name in ("baseline_audit.py", "baseline_store.py"):
    shutil.copyfile(root / "docs/r4_evidence" / name, target / "docs/r4_evidence" / name)
assert not (target / ".venv").exists() and not (target / "frontend/node_modules").exists()
out = root / "docs/r5_evidence"
out.mkdir(exist_ok=True)
manifest = {
    p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in target.rglob("*")
    if p.is_file()
}
(out / "fresh_source_manifest.json").write_text(
    json.dumps(
        {
            "initial_virtual_environment_absent": True,
            "initial_node_modules_absent": True,
            "source_files": manifest,
        },
        indent=2,
    )
)


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


identifier = uuid.uuid4().hex[:8]
(out / "context.json").write_text(
    json.dumps(
        {
            "project": "railstorm-r5-" + identifier,
            "database": "railstorm_r5_" + identifier,
            "container_port": free_port(),
            "local_port": free_port(),
        },
        indent=2,
    )
)
print(json.dumps({"fresh_source_files": len(manifest), "environment": ".work/r5/source"}))
