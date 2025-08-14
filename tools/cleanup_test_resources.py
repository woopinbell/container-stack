#!/usr/bin/env python3
"""현재 검증이 기록한 Compose 프로젝트의 잔여 자원만 회수합니다."""

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
