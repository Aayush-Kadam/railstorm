"""Collect only selected reproducibility metadata; never serialize environment secrets."""

import hashlib
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r5_evidence"
FRESH = ROOT / ".work/r5/source"
context = json.loads((OUT / "context.json").read_text())
project = context["project"]
assert project.startswith("railstorm-r5-")


def output(args, cwd=ROOT):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=30, check=True
    ).stdout.strip()


image = json.loads(output(["docker", "image", "inspect", project + "-command-center:latest"]))[0]
selected = {k: image[k] for k in ("Id", "RepoTags", "Created", "Architecture", "Os")}
volumes = json.loads(output(["docker", "volume", "inspect", project + "_ledger", project + "_evidence"]))
selected["volumes"] = [{k: v[k] for k in ("Name", "CreatedAt", "Driver", "Labels")} for v in volumes]
assert all(v["Labels"]["com.docker.compose.project"] == project for v in volumes)
image_code = "import json,sys; from pathlib import Path; import hashlib; print(json.dumps(dict(python=sys.version.split()[0],source_bytecode=[str(p) for p in Path('/app/railstorm').rglob('*.pyc')],workspace_cache=Path('/app/.work').exists(),extra_environment=Path('/app/.venv-thunderbolt').exists(),frontend={p.relative_to('/app/frontend/dist').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app/frontend/dist').rglob('*') if p.is_file()})))"
checks = json.loads(
    output(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            project + "-command-center:latest",
            "-c",
            image_code,
        ]
    )
)
application_code = "import json,hashlib; from pathlib import Path; print(json.dumps({p.relative_to('/app').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for name in ('railstorm','migrations','scenarios') for p in Path('/app',name).rglob('*') if p.is_file() and p.suffix not in ('.pyc','.pyo')}))"
image_sources = json.loads(
    output(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            project + "-command-center:latest",
            "-c",
            application_code,
        ]
    )
)
assert all(
    hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha for name, sha in image_sources.items()
)
assert not checks["source_bytecode"] and not checks["workspace_cache"] and not checks["extra_environment"]
local_dist = {
    p.relative_to(FRESH / "frontend/dist").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in (FRESH / "frontend/dist").rglob("*")
    if p.is_file()
}
assert local_dist == checks["frontend"]
fresh_code = "import json,sys,importlib.metadata as m,railstorm; print(json.dumps(dict(python=sys.version.split()[0],isolated_source=('.work/r5/source/' in railstorm.__file__.replace(chr(92),'/')),dependencies={k:m.version(k) for k in ('SQLAlchemy','psycopg','pydantic','fastapi','pytest','ruff','mypy')},installed_distributions=len(list(m.distributions())))))"
local = json.loads(output([str(FRESH / ".venv/Scripts/python.exe"), "-c", fresh_code], cwd=FRESH))
assert local["isolated_source"]
configuration = (FRESH / ".venv/pyvenv.cfg").read_text()
assert "include-system-site-packages = false" in configuration
local["system_site_packages_included"] = False
files = {}
for folder in ("railstorm", "migrations", "scenarios", "tests", "frontend/src"):
    for p in (ROOT / folder).rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"):
            relative = p.relative_to(ROOT)
            assert (FRESH / relative).read_bytes() == p.read_bytes(), str(relative)
            files[relative.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R4_REVIEW.zip") as archive:
    for p in (ROOT / "railstorm").rglob("*.py"):
        relative_name = p.relative_to(ROOT).as_posix()
        if p.name != "live_crisis_api.py":
            assert archive.read("railstorm/" + relative_name) == p.read_bytes(), relative_name
result = {
    "local": local,
    "container": selected,
    "image_checks": checks,
    "container_application_sources_match_release": image_sources,
    "local_and_container_frontend_bytes_identical": True,
    "financial_sources_match_verified_R4": True,
    "isolated_app_and_tests_match_release_source": files,
    "lockfiles": {
        n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in ("uv.lock", "frontend/pnpm-lock.yaml")
    },
    "node": output(["node", "--version"]),
    "uv": output(["uv", "--version"]),
    "docker_cli": output(["docker", "--version"]),
    "compose": output(["docker", "compose", "version"]),
    "docker_context": output(["docker", "context", "show"]),
    "docker_engine": output(["docker", "info", "--format", "{{.ServerVersion}}"]),
}
(OUT / "environment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(
    json.dumps(
        {
            "local_python": local["python"],
            "container_python": checks["python"],
            "financial_sources_unchanged": True,
            "frontend_bytes_identical": True,
            "host_source_bytecode_in_image": 0,
        }
    )
)
