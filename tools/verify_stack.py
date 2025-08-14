#!/usr/bin/env python3
"""정적 검사와 런타임 시나리오를 직렬 실행하고 잔여 자원을 확인합니다."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    "bootstrap",
    "e2e",
    "persistence",
    "backup-restore",
    "rotation",
    "operations",
)
SCENARIO_TIMEOUTS = {
    "bootstrap": 2400,
    "e2e": 1500,
    "persistence": 1500,
    "backup-restore": 1800,
    "rotation": 1800,
    "operations": 1500,
}


def run(command: list[str], *, timeout: int) -> int:
    try:
        return subprocess.run(command, cwd=ROOT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(
            f"검증 명령이 {timeout}초 안에 끝나지 않았습니다: {' '.join(command)}",
            file=sys.stderr,
        )
        return 124


def main() -> int:
    temporary = Path(tempfile.mkdtemp(prefix="container-stack-verify-"))
    temporary.chmod(0o700)
    records = temporary / "projects"
    records.mkdir(mode=0o700)
    cleanup_report = temporary / "cleanup.txt"
    result = 0
    try:
        commands = (
            ["make", "test"],
            ["make", "config-strict", "ENV_FILE=.env.example"],
        )
        for command in commands:
            result = run(command, timeout=300)
            if result != 0:
                break
        if result == 0:
            for scenario in SCENARIOS:
                result = run(
                    [
                        sys.executable,
                        str(ROOT / "tests" / "runtime_stack.py"),
                        scenario,
                        "--project-record-dir",
                        str(records),
                    ],
                    timeout=SCENARIO_TIMEOUTS[scenario],
                )
                if result != 0:
                    break
    finally:
        cleanup_result = run(
            [
                sys.executable,
                str(ROOT / "tools" / "cleanup_test_resources.py"),
                "--project-record-dir",
                str(records),
                "--report",
                str(cleanup_report),
            ],
            timeout=300,
        )
        if cleanup_result == 2 or result == 0:
            result = cleanup_result
        if cleanup_result != 0:
            print(f"누수 재확인 자료를 보존했습니다: {temporary}", file=sys.stderr)
        else:
            shutil.rmtree(temporary, ignore_errors=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
