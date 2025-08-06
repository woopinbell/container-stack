#!/usr/bin/env python3
"""Compose 프로젝트의 MariaDB와 WordPress 볼륨을 함께 백업하고 복원합니다."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import BinaryIO, Iterator

from stack_runtime import (
    StackRuntimeError,
    load_secret_values,
    secret_payload,
)
from start_stack import start_application, start_database


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
DATABASE_DUMP = "database.sql"
WORDPRESS_ARCHIVE = "wordpress.tar.gz"
MANIFEST = "manifest.json"
EXPECTED_FILES = {DATABASE_DUMP, WORDPRESS_ARCHIVE, MANIFEST}
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

    def labelled_resources(self, kind: str) -> set[str]:
        format_field = "{{.Names}}" if kind == "container" else "{{.Name}}"
        command = ["docker", kind, "ls"]
        if kind == "container":
            command.append("--all")
        command.extend(
            (
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                format_field,
            )
        )
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        return {line for line in result.stdout.splitlines() if line}

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


def rendered_resource_names(project: ComposeProject) -> dict[str, set[str]]:
    result = project.run(
        "config",
        "--format",
        "json",
        capture=True,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    try:
        config = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError(f"Compose 설정 JSON을 읽을 수 없습니다: {error}") from error
    if not isinstance(config, dict):
        raise BackupError("Compose 설정이 객체 형식이 아닙니다")

    names: dict[str, set[str]] = {"volume": set(), "network": set()}
    for config_key, resource_kind in (("volumes", "volume"), ("networks", "network")):
        resources = config.get(config_key, {})
        if not isinstance(resources, dict):
            raise BackupError(f"Compose {config_key} 설정이 객체 형식이 아닙니다")
        for resource in resources.values():
            if not isinstance(resource, dict) or not isinstance(resource.get("name"), str):
                raise BackupError(f"Compose {resource_kind} 이름을 확인할 수 없습니다")
            names[resource_kind].add(resource["name"])
    return names


def existing_named_resources(kind: str, expected: set[str]) -> set[str]:
    if not expected:
        return set()
    result = subprocess.run(
        ["docker", kind, "ls", "--format", "{{.Name}}"],
        check=True,
        text=True,
        capture_output=True,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    return expected.intersection(result.stdout.splitlines())


def expected_container_names(project: ComposeProject) -> set[str]:
    config = project.config()
    services = config.get("services")
    if not isinstance(services, dict):
        raise BackupError("Compose 서비스 설정을 찾을 수 없습니다")
    names: set[str] = set()
    for service, service_config in services.items():
        if not isinstance(service, str) or not isinstance(service_config, dict):
            raise BackupError("Compose 서비스 설정 형식이 올바르지 않습니다")
        configured_name = service_config.get("container_name")
        if configured_name is not None:
            if not isinstance(configured_name, str):
                raise BackupError("Compose 컨테이너 이름이 문자열이 아닙니다")
            names.add(configured_name)
        else:
            names.add(f"{project.project}-{service}-1")
            names.add(f"{project.project}_{service}_1")
        names.add(f"{project.project}-{service}-bootstrap")
    return names


def existing_named_containers(expected: set[str]) -> set[str]:
    result = subprocess.run(
        ["docker", "container", "ls", "--all", "--format", "{{.Names}}"],
        check=True,
        text=True,
        capture_output=True,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    return expected.intersection(result.stdout.splitlines())


def ensure_fresh_project(project: ComposeProject) -> None:
    found: dict[str, set[str]] = {}
    for kind in ("container", "volume", "network"):
        identifiers = project.labelled_resources(kind)
        if identifiers:
            found[kind] = identifiers
    named_containers = existing_named_containers(expected_container_names(project))
    if named_containers:
        found.setdefault("container", set()).update(named_containers)
    rendered = rendered_resource_names(project)
    for kind in ("volume", "network"):
        identifiers = existing_named_resources(kind, rendered[kind])
        if identifiers:
            found.setdefault(kind, set()).update(identifiers)
    if found:
        summary = ", ".join(f"{kind}={len(items)}" for kind, items in found.items())
        raise BackupError(f"복원 대상 프로젝트가 비어 있지 않습니다: {summary}")


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


class VerifiedBackup:
    def __init__(
        self,
        directory_descriptor: int,
        database: BinaryIO,
        wordpress: BinaryIO,
        manifest: dict[str, object],
    ) -> None:
        self.directory_descriptor = directory_descriptor
        self.database = database
        self.wordpress = wordpress
        self.manifest = manifest

    def __enter__(self) -> "VerifiedBackup":
        return self

    def __exit__(self, *_: object) -> None:
        self.database.close()
        self.wordpress.close()
        os.close(self.directory_descriptor)


def open_regular_file(directory_descriptor: int, filename: str) -> BinaryIO:
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | NOFOLLOW | NONBLOCK,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise BackupError(f"백업 파일을 안전하게 열 수 없습니다: {filename}") from error
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise BackupError(f"백업 항목의 형식이나 권한이 안전하지 않습니다: {filename}")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        raise BackupError(f"다른 작업이 백업 파일을 변경하고 있습니다: {filename}") from error
    return os.fdopen(descriptor, "rb")


def load_and_verify_backup(source: Path) -> VerifiedBackup:
    source = source.expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    try:
        directory_descriptor = os.open(
            source,
            os.O_RDONLY | DIRECTORY | NOFOLLOW,
        )
    except OSError as error:
        raise BackupError("백업 입력은 심볼릭 링크가 아닌 디렉터리여야 합니다") from error
    opened: list[BinaryIO] = []
    try:
        directory_info = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077
        ):
            raise BackupError("백업 입력 디렉터리의 형식이나 권한이 안전하지 않습니다")
        present = set(os.listdir(directory_descriptor))
        if present != EXPECTED_FILES:
            raise BackupError(f"백업 파일 구성이 올바르지 않습니다: {sorted(present)}")
        manifest_stream = open_regular_file(directory_descriptor, MANIFEST)
        opened.append(manifest_stream)
        if os.fstat(manifest_stream.fileno()).st_size > 64 * 1024:
            raise BackupError("백업 manifest가 허용 크기를 넘었습니다")
        try:
            manifest = json.loads(manifest_stream.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupError(f"백업 manifest를 읽을 수 없습니다: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise BackupError("지원하지 않는 백업 형식입니다")
        checksums = manifest.get("sha256")
        if not isinstance(checksums, dict):
            raise BackupError("백업 manifest에 체크섬이 없습니다")
        database = open_regular_file(directory_descriptor, DATABASE_DUMP)
        opened.append(database)
        wordpress = open_regular_file(directory_descriptor, WORDPRESS_ARCHIVE)
        opened.append(wordpress)
        for filename, stream in (
            (DATABASE_DUMP, database),
            (WORDPRESS_ARCHIVE, wordpress),
        ):
            expected = checksums.get(filename)
            actual = sha256_stream(stream)
            if not isinstance(expected, str) or expected != actual:
                raise BackupError(f"백업 체크섬이 일치하지 않습니다: {filename}")
        validate_archive_stream(wordpress)
        manifest_stream.close()
        opened.remove(manifest_stream)
        return VerifiedBackup(directory_descriptor, database, wordpress, manifest)
    except Exception:
        for stream in opened:
            stream.close()
        os.close(directory_descriptor)
        raise


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


def normalize_backup_output(requested: Path) -> Path:
    expanded = requested.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    if expanded.name in ("", ".", ".."):
        raise BackupError("백업 출력 이름이 올바르지 않습니다")
    try:
        parent = expanded.parent.resolve(strict=True)
    except OSError as error:
        raise BackupError("백업 출력의 상위 디렉터리가 없습니다") from error
    if not parent.is_dir():
        raise BackupError("백업 출력의 상위 경로가 디렉터리가 아닙니다")
    return parent / expanded.name


def same_directory(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = os.lstat(path)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(actual.st_mode)
        and actual.st_dev == expected.st_dev
        and actual.st_ino == expected.st_ino
    )


def _create_backup(
    project: ComposeProject,
    output: Path,
    failure_stage: str | None = None,
    pause_stage: str | None = None,
    pause_ready_file: Path | None = None,
) -> None:
    secrets = load_secret_values(project)
    running = project.running_services()
    expected_services = {"mariadb", "wordpress", "nginx"}
    if running != expected_services:
        raise BackupError(
            "백업은 세 서비스가 모두 실행 중일 때만 시작할 수 있습니다: "
            f"{sorted(running)}"
        )
    output = normalize_backup_output(output)
    try:
        output.mkdir(mode=0o700)
    except FileExistsError as error:
        raise BackupError("백업 출력 경로가 이미 존재합니다") from error
    reservation = os.lstat(output)
    fsync_directory(output.parent)
    temporary: Path | None = None
    published = False
    stopped = False
    original_error: BaseException | None = None
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=str(output.parent))
        )
        stopped = True
        project.run(
            "stop",
            "nginx",
            "wordpress",
            timeout=CONTROL_TIMEOUT_SECONDS,
        )
        pause_for_test(pause_stage, "backup-stop", pause_ready_file)
        database_dump(
            project,
            temporary / DATABASE_DUMP,
            secrets["db_root_password"],
        )
        maybe_fail(failure_stage, "database-dump")
        wordpress_archive(project, temporary / WORDPRESS_ARCHIVE)
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "project": project.project,
            "sha256": {
                DATABASE_DUMP: sha256(temporary / DATABASE_DUMP),
                WORDPRESS_ARCHIVE: sha256(temporary / WORDPRESS_ARCHIVE),
            },
        }
        write_private(
            temporary / MANIFEST,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        validate_archive(temporary / WORDPRESS_ARCHIVE)
        fsync_directory(temporary)
        if not same_directory(output, reservation) or any(output.iterdir()):
            raise BackupError("백업 출력 예약 경로가 변경되었습니다")
        os.replace(temporary, output)
        published = True
        fsync_directory(output.parent)
        project.run(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "240",
            timeout=CONTROL_TIMEOUT_SECONDS,
        )
        stopped = False
    except BaseException as error:
        original_error = error
        raise
    finally:
        recovery_error = ""
        if stopped:
            try:
                result = project.run(
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "240",
                    capture=True,
                    check=False,
                    timeout=CONTROL_TIMEOUT_SECONDS,
                )
                if result.returncode != 0:
                    recovery_error = (
                        result.stderr.decode(errors="replace").strip()
                        or "docker compose up이 실패했습니다"
                    )
            except BackupError as error:
                recovery_error = str(error)
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
        reservation_removed = False
        if not published and same_directory(output, reservation):
            try:
                output.rmdir()
                reservation_removed = True
            except OSError:
                pass
        if reservation_removed:
            fsync_directory(output.parent)
        if recovery_error:
            raise BackupError(
                f"백업 작업 뒤 서비스를 복구하지 못했습니다: {recovery_error}"
            ) from original_error
    print(f"백업을 생성했습니다: {output}")


def create_backup(
    project: ComposeProject,
    output: Path,
    failure_stage: str | None = None,
    pause_stage: str | None = None,
    pause_ready_file: Path | None = None,
) -> None:
    with operation_signal_handlers():
        with project_operation_lock(project.project):
            _create_backup(
                project,
                output,
                failure_stage,
                pause_stage,
                pause_ready_file,
            )


def restore_database(
    project: ComposeProject, source: BinaryIO, root_password: str
) -> None:
    with tempfile.TemporaryFile(mode="w+b") as payload:
        payload.write(secret_payload(root_password))
        shutil.copyfileobj(source, payload, length=1024 * 1024)
        payload.seek(0)
        project.run(
            "exec",
            "--no-TTY",
            "mariadb",
            "sh",
            "-ceu",
            "umask 077; auth=\"$(mktemp /run/container-stack-restore.XXXXXX)\"; "
            "trap 'rm -f -- \"$auth\"' EXIT HUP INT TERM; "
            "IFS= read -r password; "
            "printf '[client]\\npassword=\"%s\"\\n' \"$password\" >\"$auth\"; "
            "mariadb --defaults-extra-file=\"$auth\" "
            "--socket=/run/mysqld/mysqld.sock -uroot",
            input_stream=payload,
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )


def restore_wordpress(project: ComposeProject, source: BinaryIO) -> None:
    project.run(
        "run",
        "--rm",
        "--no-TTY",
        "--no-deps",
        "--entrypoint",
        "sh",
        "wordpress",
        "-ceu",
        "test -z \"$(find /var/www/html -mindepth 1 -print -quit)\"; "
        "test -z \"$(find /var/www/config -mindepth 1 -print -quit)\"; "
        "exec tar -xzf - -C /var/www",
        input_stream=source,
        timeout=TRANSFER_TIMEOUT_SECONDS,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="컨테이너 스택 백업")
    parser.add_argument("operation", choices=("backup",))
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-after", choices=("database-dump",), help=argparse.SUPPRESS)
    parser.add_argument("--pause-after", choices=("backup-stop",), help=argparse.SUPPRESS)
    parser.add_argument("--pause-ready-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if shutil.which("docker") is None:
        print("docker 명령을 찾을 수 없습니다", file=sys.stderr)
        return 2
    try:
        if (args.pause_after is None) != (args.pause_ready_file is None):
            raise BackupError("일시정지 단계와 준비 파일을 함께 지정해야 합니다")
        project = ComposeProject(args.project, args.env_file, args.compose_file)
        create_backup(
            project, args.output, args.fail_after, args.pause_after, args.pause_ready_file
        )
        return 0
    except (BackupError, StackRuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"backup 실패: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
