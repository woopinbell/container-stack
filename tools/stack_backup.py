#!/usr/bin/env python3
"""Compose 프로젝트의 MariaDB와 WordPress 볼륨을 함께 백업하고 복원합니다."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import tarfile
import time
from typing import BinaryIO, Iterator

from stack_runtime import secret_payload


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


def validate_archive_stream(stream: BinaryIO) -> None:
    try:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise BackupError("WordPress 아카이브가 비어 있습니다")
            seen: set[str] = set()
            for member in members:
                member_path = PurePosixPath(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise BackupError(f"안전하지 않은 아카이브 경로입니다: {member.name}")
                normalized = member_path.as_posix()
                if normalized in seen:
                    raise BackupError(f"중복된 아카이브 경로입니다: {member.name}")
                seen.add(normalized)
                if not (member.isdir() or member.isfile()):
                    raise BackupError(f"지원하지 않는 아카이브 항목입니다: {member.name}")
    except (tarfile.TarError, OSError) as error:
        raise BackupError(f"WordPress 아카이브를 읽을 수 없습니다: {error}") from error
    finally:
        stream.seek(0)


def validate_archive(path: Path) -> None:
    with path.open("rb") as stream:
        validate_archive_stream(stream)


@contextmanager
def project_operation_lock(project_name: str) -> Iterator[None]:
    lock_directory = Path("/tmp") / f"container-stack-operation-locks-{os.getuid()}"
    try:
        lock_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        directory_info = os.lstat(lock_directory)
    except OSError as error:
        raise BackupError("관리 작업 잠금 디렉터리를 확인할 수 없습니다") from error
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or stat.S_IMODE(directory_info.st_mode) & 0o077
    ):
        raise BackupError("관리 작업 잠금 디렉터리 권한이 안전하지 않습니다")
    directory_descriptor = os.open(
        lock_directory,
        os.O_RDONLY | DIRECTORY | NOFOLLOW,
    )
    lock_name = hashlib.sha256(project_name.encode("utf-8")).hexdigest() + ".lock"
    lock_descriptor: int | None = None
    try:
        lock_descriptor = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(lock_descriptor, 0o600)
        lock_info = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
            raise BackupError("관리 작업 잠금 파일이 안전하지 않습니다")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError("같은 프로젝트의 다른 관리 작업이 실행 중입니다") from error
        try:
            yield
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(directory_descriptor)


def validate_database_dump(path: Path) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(1024 * 1024)
    if not prefix.startswith(b"/*M!") and b"CREATE DATABASE" not in prefix:
        raise BackupError("MariaDB 덤프가 예상한 SQL 형식이 아닙니다")


def database_dump(
    project: ComposeProject, destination: Path, root_password: str
) -> None:
    with private_output(destination) as output:
        project.run(
            "exec",
            "--no-TTY",
            "mariadb",
            "sh",
            "-ceu",
            "umask 077; auth=\"$(mktemp /run/container-stack-dump.XXXXXX)\"; "
            "trap 'rm -f -- \"$auth\"' EXIT HUP INT TERM; "
            "IFS= read -r password; "
            "printf '[client]\\npassword=\"%s\"\\n' \"$password\" >\"$auth\"; "
            "mariadb-dump --defaults-extra-file=\"$auth\" "
            "--socket=/run/mysqld/mysqld.sock -uroot --single-transaction "
            "--routines --events --triggers --hex-blob --add-drop-database "
            "--databases \"$MYSQL_DATABASE\"",
            input_data=secret_payload(root_password),
            output_stream=output,
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )
    validate_database_dump(destination)


def wordpress_archive(project: ComposeProject, destination: Path) -> None:
    with private_output(destination) as output:
        project.run(
            "run",
            "--rm",
            "--no-TTY",
            "--no-deps",
            "--entrypoint",
            "tar",
            "wordpress",
            "-C",
            "/var/www",
            "-czf",
            "-",
            "--exclude=html/wp-config.php",
            "html",
            "config",
            output_stream=output,
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )
    with destination.open("rb") as stream:
        magic = stream.read(2)
    if magic != b"\x1f\x8b":
        raise BackupError("WordPress 볼륨 아카이브가 gzip 형식이 아닙니다")
