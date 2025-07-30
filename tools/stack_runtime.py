#!/usr/bin/env python3
"""Compose 관리 도구가 공유하는 프로젝트 실행 경계를 제공합니다."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import BinaryIO


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")


class StackRuntimeError(RuntimeError):
    pass


class ComposeProject:
    def __init__(
        self,
        project: str,
        env_file: Path,
        compose_file: Path = DEFAULT_COMPOSE_FILE,
        *,
        timeout: int = 300,
    ) -> None:
        if not PROJECT_PATTERN.fullmatch(project):
            raise StackRuntimeError(
                "프로젝트 이름은 소문자·숫자·밑줄·하이픈 3~63자여야 합니다"
            )
        if timeout < 1 or timeout > 3600:
            raise StackRuntimeError("Compose 제한 시간은 1~3600초여야 합니다")
        self.project = project
        self.env_file = env_file.expanduser().resolve(strict=True)
        self.compose_file = compose_file.expanduser().resolve(strict=True)
        self.timeout = timeout

    def command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
            "--file",
            str(self.compose_file),
            *arguments,
        ]

    def run(
        self,
        *arguments: str,
        input_data: bytes | None = None,
        input_stream: BinaryIO | None = None,
        capture: bool = False,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if input_data is not None and input_stream is not None:
            raise StackRuntimeError("subprocess 입력 형식을 하나만 지정해야 합니다")
        try:
            return subprocess.run(
                self.command(*arguments),
                cwd=ROOT,
                input=input_data,
                stdin=input_stream,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=check,
                timeout=self.timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise StackRuntimeError(
                f"Docker Compose 명령이 {error.timeout}초 안에 끝나지 않았습니다"
            ) from error

    def config(self) -> dict[str, object]:
        result = self.run("config", "--format", "json", capture=True)
        try:
            parsed = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StackRuntimeError(
                f"Compose 설정 JSON을 읽을 수 없습니다: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise StackRuntimeError("Compose 설정이 객체 형식이 아닙니다")
        return parsed

    def running_services(self) -> set[str]:
        result = self.run(
            "ps",
            "--status",
            "running",
            "--services",
            capture=True,
        )
        return {
            line
            for line in result.stdout.decode(errors="replace").splitlines()
            if line
        }
