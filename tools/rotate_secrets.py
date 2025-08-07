#!/usr/bin/env python3
"""실행 중인 스택의 DB·WordPress 자격증명을 검증 가능한 절차로 회전합니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from stack_backup import (
    BackupError,
    ComposeProject,
    DEFAULT_COMPOSE_FILE,
    QUERY_TIMEOUT_SECONDS,
    project_operation_lock,
)
from stack_runtime import StackRuntimeError, secret_source_paths


SECRET_FILES = {
    "db_root_password": "db_root_password.txt",
    "db_password": "db_password.txt",
    "wp_admin_password": "wp_admin_password.txt",
    "wp_user_password": "wp_user_password.txt",
}
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9_.~!@#%^+=,-]{24,128}$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class RotationError(RuntimeError):
    pass


def read_secret(path: Path, *, require_owner: bool) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
    except OSError as error:
        raise RotationError(f"비밀값을 안전하게 열 수 없습니다: {path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RotationError(f"비밀값은 단일 링크의 일반 파일이어야 합니다: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise RotationError(f"비밀 파일 권한은 0600이어야 합니다: {path}")
        if require_owner and info.st_uid != os.getuid():
            raise RotationError(f"현재 사용자가 소유하지 않은 비밀 파일입니다: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = stream.read(1025)
            if len(value) > 1024 or stream.read(1):
                raise RotationError(f"비밀 파일이 허용 크기를 넘었습니다: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value or not PASSWORD_PATTERN.fullmatch(value):
        raise RotationError(f"비밀값 형식이 올바르지 않습니다: {path.name}")
    return value


def atomic_secret_write(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | DIRECTORY | NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def compose_config(project: ComposeProject) -> dict[str, object]:
    result = project.run(
        "config",
        "--format",
        "json",
        capture=True,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RotationError(f"Compose 설정 JSON을 읽을 수 없습니다: {error}") from error
    if not isinstance(parsed, dict):
        raise RotationError("Compose 설정이 객체 형식이 아닙니다")
    return parsed


def current_secret_paths(
    config: dict[str, object], compose_directory: Path
) -> dict[str, Path]:
    try:
        return secret_source_paths(
            config,
            compose_directory=compose_directory,
        )
    except StackRuntimeError as error:
        raise RotationError(str(error)) from error


def service_environment(config: dict[str, object], service: str) -> dict[str, str]:
    services = config.get("services")
    if not isinstance(services, dict) or not isinstance(services.get(service), dict):
        raise RotationError(f"Compose 서비스를 찾을 수 없습니다: {service}")
    environment = services[service].get("environment")
    if not isinstance(environment, dict):
        raise RotationError(f"서비스 환경 변수를 찾을 수 없습니다: {service}")
    return {str(key): str(value) for key, value in environment.items()}


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def root_sql(
    project: ComposeProject,
    root_password: str,
    sql: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    payload = root_password.encode() + b"\n" + sql.encode() + b"\n"
    return project.run(
        "exec",
        "--no-TTY",
        "mariadb",
        "sh",
        "-ceu",
        "umask 077; auth=\"$(mktemp /run/container-stack-root.XXXXXX)\"; "
        "trap 'rm -f -- \"$auth\"' EXIT HUP INT TERM; "
        "IFS= read -r password; "
        "printf '[client]\\npassword=\"%s\"\\n' \"$password\" >\"$auth\"; "
        "mariadb --defaults-extra-file=\"$auth\" "
        "--socket=/run/mysqld/mysqld.sock -uroot",
        input_data=payload,
        capture=True,
        check=check,
    )


def alter_database_passwords(
    project: ComposeProject,
    root_password: str,
    database_user: str,
    *,
    app_password: str | None = None,
    new_root_password: str | None = None,
    fail_after_write: bool = False,
) -> None:
    statements = ["SET SESSION sql_mode='NO_BACKSLASH_ESCAPES'", "FLUSH PRIVILEGES"]
    if app_password is not None:
        statements.append(
            f"ALTER USER {sql_literal(database_user)}@'%' IDENTIFIED BY {sql_literal(app_password)}"
        )
    if new_root_password is not None:
        statements.append(
            "ALTER USER 'root'@'localhost' IDENTIFIED BY " + sql_literal(new_root_password)
        )
    statements.append("FLUSH PRIVILEGES")
    if fail_after_write:
        statements.append(
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected rotation failure'"
        )
    root_sql(project, root_password, ";\n".join(statements) + ";")
