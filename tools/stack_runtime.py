#!/usr/bin/env python3
"""Compose 관리 도구가 공유하는 프로젝트·비밀 파일 경계를 제공합니다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import BinaryIO


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9_.~!@#%^+=,-]{24,128}$")
SECRET_FILENAMES = {
    "db_root_password": "db_root_password.txt",
    "db_password": "db_password.txt",
    "wp_admin_password": "wp_admin_password.txt",
    "wp_user_password": "wp_user_password.txt",
}
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


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


def _private_directory(path: Path, description: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise StackRuntimeError(f"{description}을 확인할 수 없습니다: {path}") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise StackRuntimeError(
            f"{description}은 현재 사용자만 접근하는 일반 디렉터리여야 합니다: {path}"
        )


def read_private_secret(path: Path) -> str:
    path = path.expanduser()
    _private_directory(path.parent, "비밀 파일 상위 디렉터리")
    try:
        descriptor = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
    except OSError as error:
        raise StackRuntimeError(f"비밀 파일을 안전하게 열 수 없습니다: {path}") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StackRuntimeError(
                f"비밀 파일은 현재 사용자가 소유한 0600 단일 링크 일반 파일이어야 합니다: {path}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = stream.read(1025)
            if len(value) > 1024 or stream.read(1):
                raise StackRuntimeError(f"비밀 파일이 허용 크기를 넘었습니다: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    value = value.removesuffix("\n")
    if not PASSWORD_PATTERN.fullmatch(value):
        raise StackRuntimeError(
            f"비밀값은 허용 문자로 작성한 24~128자 한 줄이어야 합니다: {path.name}"
        )
    return value


def secret_source_paths(
    config: dict[str, object],
    *,
    compose_directory: Path | None = None,
) -> dict[str, Path]:
    configured = config.get("x-secret-files")
    if not isinstance(configured, dict):
        raise StackRuntimeError("Compose x-secret-files 설정을 찾을 수 없습니다")
    paths: dict[str, Path] = {}
    for name in SECRET_FILENAMES:
        entry = configured.get(name)
        if not isinstance(entry, str):
            raise StackRuntimeError(f"Compose secret 원본을 찾을 수 없습니다: {name}")
        path = Path(entry)
        if not path.is_absolute():
            if compose_directory is None:
                raise StackRuntimeError(
                    f"상대 secret 경로의 기준 디렉터리가 없습니다: {name}"
                )
            path = compose_directory / path
        paths[name] = Path(os.path.abspath(path))
    canonical = [path.resolve(strict=True) for path in paths.values()]
    if len(set(canonical)) != len(canonical):
        raise StackRuntimeError("Compose secret 원본 경로는 서로 달라야 합니다")
    return paths


def load_secret_values(project: ComposeProject) -> dict[str, str]:
    paths = secret_source_paths(
        project.config(),
        compose_directory=project.compose_file.parent,
    )
    return {name: read_private_secret(path) for name, path in paths.items()}


def service_environment(
    config: dict[str, object], service: str
) -> dict[str, str]:
    services = config.get("services")
    if not isinstance(services, dict):
        raise StackRuntimeError("Compose 서비스 설정을 찾을 수 없습니다")
    configured = services.get(service)
    if not isinstance(configured, dict):
        raise StackRuntimeError(f"Compose 서비스를 찾을 수 없습니다: {service}")
    environment = configured.get("environment")
    if not isinstance(environment, dict):
        raise StackRuntimeError(f"서비스 환경 변수를 찾을 수 없습니다: {service}")
    return {str(key): str(value) for key, value in environment.items()}


def secret_payload(*values: str) -> bytes:
    for value in values:
        if not PASSWORD_PATTERN.fullmatch(value):
            raise StackRuntimeError("표준 입력 비밀값의 형식이 올바르지 않습니다")
    return ("\n".join(values) + "\n").encode()
