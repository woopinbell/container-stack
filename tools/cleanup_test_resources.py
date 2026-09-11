#!/usr/bin/env python3
"""현재 검증이 기록한 Compose 프로젝트의 잔여 자원만 회수합니다."""

# [INTV:ARCH] CI(container-stack.yml)의 "격리 자원 정리 확인" 스텝이 6개 시나리오가 전부 끝난
# 뒤(성공/실패 무관하게 if: always()) 이 스크립트를 한 번 호출한다 — RuntimeStack.close()가 각
# 시나리오 자신의 컨테이너/볼륨/네트워크/이미지를 정상적으로 정리하는 것과는 별개의, 마지막
# 안전망이다. "project_record_dir"(각 RuntimeStack이 자기 프로젝트 이름을 파일로 남겨둔 디렉터리)에
# 기록된 프로젝트명으로만 docker 자원을 필터링해 지운다 — docker에 떠 있는 아무 컨테이너나 라벨
# 없이 지우면 CI 러너에 우연히 같이 떠 있는 무관한 컨테이너까지 건드릴 위험이 있어, 반드시 "이번
# 실행이 스스로 만들었다고 기록해둔 프로젝트"로만 범위를 좁힌다.
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


PROJECT_PATTERN = re.compile(r"^container-stack-[0-9]+-[0-9a-f]{6}$")
PROJECT_LABEL = "com.docker.compose.project"
IMAGE_SERVICES = ("nginx", "wordpress", "mariadb")


class CleanupError(RuntimeError):
    pass


def load_projects(directory: Path) -> list[str]:
    if directory.is_symlink() or not directory.is_dir():
        raise CleanupError(f"프로젝트 기록 디렉터리가 올바르지 않습니다: {directory}")
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise CleanupError(f"프로젝트 기록 디렉터리 권한이 안전하지 않습니다: {directory}")
    projects: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise CleanupError(f"프로젝트 기록에 일반 파일이 아닌 항목이 있습니다: {path}")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise CleanupError(f"프로젝트 기록 파일 권한이 0600이 아닙니다: {path}")
        raw_project = path.read_text(encoding="utf-8")
        project = raw_project.removesuffix("\n")
        if (
            not PROJECT_PATTERN.fullmatch(project)
            or path.name != project
            or raw_project != f"{project}\n"
        ):
            raise CleanupError(f"프로젝트 기록 내용이 올바르지 않습니다: {path}")
        projects.append(project)
    return projects


def list_resources(kind: str, project: str) -> list[str]:
    if kind == "image":
        images: list[str] = []
        for service in IMAGE_SERVICES:
            tag = f"{project}-image-{service}:local"
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "ls",
                    "--filter",
                    f"reference={tag}",
                    "--format",
                    "{{.Repository}}:{{.Tag}}",
                ],
                check=True,
                text=True,
                capture_output=True,
                timeout=30,
            )
            if tag in result.stdout.splitlines():
                images.append(tag)
        return images

    commands = {
        "container": [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            "{{.ID}}",
        ],
        "volume": [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            "{{.Name}}",
        ],
        "network": [
            "docker",
            "network",
            "ls",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            "{{.ID}}",
        ],
    }
    result = subprocess.run(
        commands[kind], check=True, text=True, capture_output=True, timeout=30
    )
    return [line for line in result.stdout.splitlines() if line]


def remove(kind: str, identifier: str) -> subprocess.CompletedProcess[str]:
    commands = {
        "container": ["docker", "rm", "--force", identifier],
        "volume": ["docker", "volume", "rm", identifier],
        "network": ["docker", "network", "rm", identifier],
        "image": ["docker", "image", "rm", identifier],
    }
    return subprocess.run(
        commands[kind], text=True, capture_output=True, timeout=30
    )


def write_private(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        raise CleanupError(f"정리 보고서 경로가 이미 존재합니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


# [INTV:TRAP] 반환값이 세 갈래(0/1/2)이고, 1도 실패로 취급된다는 점이 이 스크립트의 핵심 설계다 —
# 0(자원 없음)만 "완전히 정상"이고, 1(자원을 찾았지만 전부 회수는 성공)조차도 exit code가 0이
# 아니라서 이 스텝을 실패로 만든다. 얼핏 "지웠으면 성공 아닌가" 싶지만, 정상적인 실행이라면
# RuntimeStack.close()가 이미 자기 몫을 다 지웠어야 하므로, 이 스크립트가 여기서 뭔가를 "찾았다"는
# 사실 자체가 "어떤 시나리오의 자체 정리 로직이 실패했다"는 신호다 — 그 실패를 자동 복구(회수)
# 해주면서도 CI에는 여전히 빨간불을 띄워, 조용히 넘어가지 않고 사람이 원인을 조사하게 만든다.
# 2(회수 자체도 일부 실패)는 그보다 더 심각한, 사람이 직접 개입해야 할 상태.
def cleanup(project_record_dir: Path, report: Path | None) -> int:
    projects = load_projects(project_record_dir)
    discovered: dict[str, list[tuple[str, str]]] = {
        kind: [] for kind in ("container", "volume", "network", "image")
    }
    for project in projects:
        for kind in discovered:
            discovered[kind].extend(
                (identifier, project)
                for identifier in list_resources(kind, project)
            )
    if not any(discovered.values()):
        print("현재 검증이 남긴 Compose 자원이 없습니다")
        return 0

    lines = ["현재 검증에서 회수한 자원입니다."]
    failures: list[str] = []
    for kind in ("container", "volume", "network", "image"):
        for identifier, project in discovered[kind]:
            result = remove(kind, identifier)
            outcome = "removed" if result.returncode == 0 else "failed"
            lines.append(f"{kind}\t{identifier}\t{project}\t{outcome}")
            if result.returncode != 0:
                failures.append(
                    f"{kind} {identifier}: {result.stderr.strip() or result.stdout.strip()}"
                )
    if failures:
        lines.extend(("", "회수 실패:", *failures))
    if report is not None:
        write_private(report, "\n".join(lines) + "\n")
        print(f"정리 보고서: {report}", file=sys.stderr)
    if failures:
        print("일부 검증 자원을 회수하지 못했습니다", file=sys.stderr)
        return 2
    print("검증 자원 누수를 발견해 기록된 프로젝트만 회수했습니다", file=sys.stderr)
    return 1


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="현재 검증의 Compose 자원 누수 검사")
    parser.add_argument("--project-record-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        return cleanup(args.project_record_dir, args.report)
    except (CleanupError, OSError, subprocess.SubprocessError) as error:
        print(f"검증 자원 정리 실패: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
