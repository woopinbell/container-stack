#!/usr/bin/env python3
"""Compose 프로젝트의 MariaDB와 WordPress 볼륨을 함께 백업하고 복원합니다."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import signal
import time
from typing import BinaryIO, Iterator


FAILURE_STAGES = ("database-dump", "database-restore")
PAUSE_STAGES = ("backup-stop", "database-restore")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


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
