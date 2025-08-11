#!/usr/bin/env python3
"""비밀값을 제외한 Compose 실행 상태를 비공개 디렉터리에 저장합니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from stack_runtime import (
    StackRuntimeError,
    read_private_secret,
    secret_source_paths,
)


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(password|secret|token)([ \t]*[:=][ \t]*)([^\s,;]+)"
)


class DiagnosticError(RuntimeError):
    pass


def rendered_compose_config(project: str, env_file: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            compose_command(project, env_file, "config", "--format", "json"),
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiagnosticError(f"렌더링된 Compose 설정을 읽을 수 없습니다: {error}") from error
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DiagnosticError(f"Compose 설정 JSON을 읽을 수 없습니다: {error}") from error
    if not isinstance(config, dict):
        raise DiagnosticError("Compose 설정이 객체 형식이 아닙니다")
    return config


def secret_values(config: dict[str, object]) -> set[str]:
    configured = config.get("x-secret-files")
    if not isinstance(configured, dict):
        raise DiagnosticError("Compose x-secret-files 설정이 객체 형식이 아닙니다")
    secrets: set[str] = set()
    try:
        paths = secret_source_paths(
            config,
            compose_directory=COMPOSE_FILE.parent,
        )
    except StackRuntimeError as error:
        raise DiagnosticError(str(error)) from error
    for name, secret_path in paths.items():
        raw_path = configured.get(name)
        if not isinstance(raw_path, str):
            raise DiagnosticError("Compose secret 파일 경로가 올바르지 않습니다")
        secrets.add(raw_path)
        secrets.add(str(secret_path))
        try:
            value = read_private_secret(secret_path)
        except StackRuntimeError as error:
            raise DiagnosticError(
                f"진단 자료에서 가릴 비밀값을 읽을 수 없습니다: {secret_path}"
            ) from error
        if value:
            secrets.add(value)
    return secrets


def redact(text: str, secrets: set[str]) -> str:
    redacted = text
    for value in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(value, "<redacted>")
    return SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", redacted)
