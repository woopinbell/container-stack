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
