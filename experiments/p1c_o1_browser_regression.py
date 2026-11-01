"""Run existing actual workflows while preserving their historical output files."""

import json
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
temporary = root / "frontend/.p1c-o1-browser-check.mjs"
assert not temporary.exists()
results = []
try:
    for name in (
        "correspondent-routing.e2e.mjs",
        "routing.e2e.mjs",
        "e2e.mjs",
        "correspondent.e2e.mjs",
        "instant.e2e.mjs",
    ):
        source = (root / "frontend" / name).read_text()
        source = source.replace("../docs/screenshots/", "../docs/screenshots/p1c-o1-regression-")
        source = re.sub(r"\.\./docs/([^/'\"]+\.json)", r"../docs/P1C_O1_regression_\1", source)
        temporary.write_text(source)
        run = subprocess.run(["node", str(temporary)], cwd=root / "frontend", capture_output=True, text=True)
        results.append(
            {"workflow": name, "exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr}
        )
        (root / "docs/P1C_O1_browser_regression.json").write_text(json.dumps(results, indent=2))
        print(name, run.returncode, run.stdout, flush=True)
        assert run.returncode == 0, run.stderr
finally:
    temporary.unlink(missing_ok=True)
