#!/usr/bin/env python3
"""Compose 프로젝트의 MariaDB와 WordPress 볼륨을 함께 백업하고 복원합니다."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import BinaryIO, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
FAILURE_STAGES = ("database-dump", "database-restore")
PAUSE_STAGES = ("backup-stop", "database-restore")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
QUERY_TIMEOUT_SECONDS = 30
CONTROL_TIMEOUT_SECONDS = 360
TRANSFER_TIMEOUT_SECONDS = 3600


class BackupError(RuntimeError):
    pass


def sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256_stream(stream)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def maybe_fail(requested: str | None, stage: str) -> None:
    if requested == stage:
        raise BackupError(f"실패 주입: {stage}")


def pause_for_test(requested: str | None, stage: str, ready_file: Path | None) -> None:
    if requested != stage:
        return
    if ready_file is None:
        raise BackupError("일시정지 준비 파일이 지정되지 않았습니다")
    ready_file = ready_file.expanduser()
    if not ready_file.is_absolute():
        ready_file = Path.cwd() / ready_file
    blocked_signals = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
    owns_ready_file = False
    mask_restored = False
    try:
        descriptor = os.open(
            ready_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
            0o600,
        )
        owns_ready_file = True
        try:
            os.write(descriptor, (stage + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        mask_restored = True
        while True:
            time.sleep(3600)
    finally:
        if not mask_restored:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if owns_ready_file:
            try:
                ready_file.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def operation_signal_handlers() -> Iterator[None]:
    previous_handlers: dict[signal.Signals, object] = {}

    def interrupt(signum: int, _frame: object) -> None:
        signal_name = signal.Signals(signum).name
        raise BackupError(f"{signal_name} 신호로 관리 작업이 중단되었습니다")

    for current_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[current_signal] = signal.signal(current_signal, interrupt)
    try:
        yield
    finally:
        for current_signal, previous in previous_handlers.items():
            signal.signal(current_signal, previous)


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def private_output(path: Path) -> Iterator[BinaryIO]:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class ComposeProject:
    def __init__(self, project: str, env_file: Path, compose_file: Path) -> None:
        if not PROJECT_PATTERN.fullmatch(project):
            raise BackupError("프로젝트 이름은 소문자·숫자·밑줄·하이픈 3~63자여야 합니다")
        self.project = project
        self.env_file = env_file.resolve(strict=True)
        self.compose_file = compose_file.resolve(strict=True)
        self.timeout = CONTROL_TIMEOUT_SECONDS

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
        output_stream: BinaryIO | None = None,
        capture: bool = False,
        check: bool = True,
        timeout: int = CONTROL_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        if input_data is not None and input_stream is not None:
            raise BackupError("subprocess 입력 형식을 하나만 지정해야 합니다")
        if output_stream is not None and capture:
            raise BackupError("subprocess 출력을 스트림과 메모리에 동시에 받을 수 없습니다")
        try:
            return subprocess.run(
                self.command(*arguments),
                cwd=ROOT,
                input=input_data,
                stdin=input_stream,
                stdout=output_stream if output_stream is not None else (
                    subprocess.PIPE if capture else None
                ),
                stderr=subprocess.PIPE if capture else None,
                check=check,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            operation = arguments[0] if arguments else "command"
            raise BackupError(
                f"Compose {operation} 명령이 {timeout}초 안에 끝나지 않았습니다"
            ) from error

    def config(self) -> dict[str, object]:
        result = self.run(
            "config",
            "--format",
            "json",
            capture=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        try:
            parsed = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupError(f"Compose 설정 JSON을 읽을 수 없습니다: {error}") from error
        if not isinstance(parsed, dict):
            raise BackupError("Compose 설정이 객체 형식이 아닙니다")
        return parsed

    def running_services(self) -> set[str]:
        result = self.run(
            "ps",
            "--status",
            "running",
            "--services",
            capture=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        return {
            line
            for line in result.stdout.decode(errors="replace").splitlines()
            if line
        }
