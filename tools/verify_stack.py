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
    # tempfile.mkdtemp: 충돌 없는 임시 디렉터리를 만들어 경로를 반환하는 표준 라이브러리 함수.
    # 이 디렉터리는 이번 verify 실행 동안 만들어진 Compose 프로젝트 이름들을 기록해뒀다가,
    # 끝날 때(성공/실패 무관) cleanup_test_resources.py가 그 기록만 근거로 자원을 회수하는 데 쓰인다
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
                # sys.executable: 지금 이 스크립트를 실행 중인 파이썬 인터프리터 자신의 경로 —
                # "python3"을 다시 PATH에서 찾는 대신, 같은 인터프리터로 하위 시나리오 스크립트를 실행하게 강제한다
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
        # try 블록에서 시나리오가 실패해 중간에 break로 빠져나오더라도, finally라 정리 단계는 항상 실행된다
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
        # cleanup_test_resources.py의 종료 코드는 0=회수할 게 없었음, 1=누수를 찾아 정리함, 2=정리 자체가 실패.
        # 2(정리 실패)는 항상 우선시켜 결과를 덮어쓰고, 시나리오가 전부 통과(result==0)했더라도 뒤늦게 자원 누수가
        # 발견됐다면(cleanup_result==1) "성공"이 아니라 그 누수 사실을 최종 결과로 남긴다 —
        # 시나리오 실패가 이미 있었다면 그 실패 코드가 누수 발견 코드보다 우선한다
        if cleanup_result == 2 or result == 0:
            result = cleanup_result
        if cleanup_result != 0:
            print(f"누수 재확인 자료를 보존했습니다: {temporary}", file=sys.stderr)
        else:
            shutil.rmtree(temporary, ignore_errors=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
