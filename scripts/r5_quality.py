"""Explicit maintained quality gate plus a disclosed historical type inventory."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".work/r5/source/.venv/Scripts/python.exe"
OUT = ROOT / "docs/r5_evidence"
LOG = ROOT / ".work/r5/logs"
ACTIVE = [
    "railstorm",
    "tests",
    "scripts/load_scenarios.py",
    "experiments/p2a_campaign.py",
    "experiments/p2ar_contention.py",
    "experiments/p2ar_crash.py",
    "experiments/p2ar_crash_worker.py",
    "experiments/p2ar_ordering.py",
    "experiments/r2_crash_worker.py",
    *[
        "experiments/r4_" + name + ".py"
        for name in ("profile", "benchmark", "environment", "digest_compare", "campaign", "components")
    ],
    "experiments/r5_smoke.py",
    *[
        "scripts/r5_" + name + ".py"
        for name in ("prepare", "docker", "local", "metadata", "quality", "package")
        if (ROOT / ("scripts/r5_" + name + ".py")).exists()
    ],
]
LEGACY = {
    "experiments/" + name + ".py"
    for name in (
        "p1c_o1_summarize",
        "p1c_o1_attribution",
        "p1c_o0_summarize",
        "p1c_o1_final_audit",
        "p1b_c2_evidence",
        "p1c_d1_evidence",
        "p1c_o1_diagnose",
        "p1c_o0_campaign",
        "p01_forensics",
        "p1c_o1_campaign",
    )
}


def run(label, args, required=True):
    result = subprocess.run([str(PYTHON), *args], cwd=ROOT, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    (LOG / (label + ".log")).write_text(output, encoding="utf-8")
    if required and result.returncode:
        raise RuntimeError(label + ": " + output)
    return {"command": ["fresh-python", *args], "returncode": result.returncode, "output": output}


results = {
    "lint": run("quality_lint", ["-m", "ruff", "check", "railstorm", "tests", "experiments", "scripts"]),
    "format": run(
        "quality_format", ["-m", "ruff", "format", "--check", "railstorm", "tests", "experiments", "scripts"]
    ),
    "maintained_types": run("quality_types", ["-m", "mypy", "--explicit-package-bases", *ACTIVE]),
    "all_source_inventory": run(
        "quality_legacy_inventory",
        ["-m", "mypy", "--explicit-package-bases", "railstorm", "tests", "experiments", "scripts"],
        False,
    ),
}
errors = re.findall(r"^(.+?\.py):\d+: error:", results["all_source_inventory"]["output"], re.MULTILINE)
files = {path.replace("\\", "/") for path in errors}
assert files <= LEGACY, files - LEGACY
results.update(
    maintained_scope=ACTIVE,
    legacy_error_count=len(errors),
    legacy_error_files=sorted(files),
    immutable_scope="Prior ZIP archives and frozen baseline source under docs are preserved, excluded from maintained edits",
    generated_scope="Virtual environments, node_modules, build outputs, caches and isolated install copies are generated, not maintained source",
)
(OUT / "quality.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps({"maintained": "PASS", "legacy_errors_disclosed": len(errors), "legacy_files": len(files)}))
