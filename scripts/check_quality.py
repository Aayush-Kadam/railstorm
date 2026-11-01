"""Portable maintained gate; legacy report generators are inventoried separately."""

import subprocess
import sys

SCOPE = [
    "railstorm",
    "tests",
    "scripts/load_scenarios.py",
    "experiments/p2a_campaign.py",
    "experiments/p2ar_contention.py",
    "experiments/p2ar_crash.py",
    "experiments/p2ar_crash_worker.py",
    "experiments/p2ar_ordering.py",
    "experiments/r2_crash_worker.py",
    "experiments/r4_profile.py",
    "experiments/r4_benchmark.py",
    "experiments/r4_environment.py",
    "experiments/r4_digest_compare.py",
    "experiments/r4_campaign.py",
    "experiments/r4_components.py",
    "experiments/r5_smoke.py",
    "scripts/r5_prepare.py",
    "scripts/r5_docker.py",
    "scripts/r5_local.py",
    "scripts/r5_metadata.py",
    "scripts/r5_quality.py",
    "scripts/r5_package.py",
    "scripts/r6_docker.py",
    "scripts/r6_metadata.py",
    "scripts/r6_package.py",
    "scripts/r6_prepare.py",
    "scripts/r6_quality.py",
    "scripts/r6_report.py",
    "scripts/r6_run.py",
    "experiments/r6_benchmark.py",
    "experiments/r6_contention.py",
    "migrations/versions/008_market_lookup.py",
    "scripts/r7_differential.py",
    "scripts/r7_metadata.py",
    "scripts/r7_package.py",
    "scripts/r7_preflight.py",
    "scripts/r7_prepare.py",
    "scripts/r7_quality.py",
    "scripts/r7_report.py",
    "scripts/r7_restart.py",
    "scripts/r7_run.py",
    "experiments/r7_benchmark.py",
    "experiments/r7_concurrency.py",
    "experiments/r7_protocol.py",
    "experiments/r7_restart_worker.py",
]


def main():
    for args in (
        ["-m", "ruff", "check", "railstorm", "tests", "experiments", "scripts"],
        ["-m", "ruff", "format", "--check", "railstorm", "tests", "experiments", "scripts"],
        ["-m", "mypy", "--explicit-package-bases", *SCOPE, "scripts/check_quality.py"],
    ):
        subprocess.run([sys.executable, *args], check=True)


if __name__ == "__main__":
    main()
