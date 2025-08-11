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


def run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"command failed before completion: {error}\n"
    return (
        f"exit_code={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def compose_command(project: str, env_file: Path, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file),
        "--file",
        str(COMPOSE_FILE),
        *arguments,
    ]


def container_state(project: str) -> str:
    ids = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    container_ids = [line for line in ids.stdout.splitlines() if line]
    if not container_ids:
        return f"container_list_exit_code={ids.returncode}\n[]\n"
    result = subprocess.run(
        ["docker", "inspect", *container_ids],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        return f"exit_code={result.returncode}\nstderr:\n{result.stderr}"
    inspected = json.loads(result.stdout)
    selected = []
    for container in inspected:
        state = container.get("State", {})
        host = container.get("HostConfig", {})
        config = container.get("Config", {})
        health = state.get("Health") or {}
        selected.append(
            {
                "name": container.get("Name", "").lstrip("/"),
                "image": config.get("Image"),
                "status": state.get("Status"),
                "exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled"),
                "restarting": state.get("Restarting"),
                "restart_count": container.get("RestartCount"),
                "health": health.get("Status", "none"),
                "memory": host.get("Memory"),
                "nano_cpus": host.get("NanoCpus"),
                "pids_limit": host.get("PidsLimit"),
                "ulimits": host.get("Ulimits"),
                "log_config": host.get("LogConfig"),
                "security_opt": host.get("SecurityOpt"),
                "stop_signal": config.get("StopSignal"),
                "stop_timeout": config.get("StopTimeout"),
                "networks": sorted(
                    (container.get("NetworkSettings", {}).get("Networks") or {}).keys()
                ),
            }
        )
    return json.dumps(selected, ensure_ascii=False, indent=2) + "\n"


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def collect(project: str, env_file: Path, destination: Path) -> None:
    if not PROJECT_PATTERN.fullmatch(project):
        raise DiagnosticError("프로젝트 이름은 소문자·숫자로 시작하고 63자를 넘을 수 없습니다")
    if not env_file.is_file():
        raise DiagnosticError(f"환경 파일을 찾을 수 없습니다: {env_file}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as error:
        raise DiagnosticError(f"출력 경로가 이미 존재합니다: {destination}") from error
    try:
        config = rendered_compose_config(project, env_file)
        secrets = secret_values(config)
        outputs = {
            "versions.txt": run(["docker", "version"])
            + "\n"
            + run(["docker", "compose", "version"]),
            "compose-ps.txt": run(
                compose_command(project, env_file, "ps", "--all", "--format", "json")
            ),
            "compose-logs.txt": run(
                compose_command(
                    project,
                    env_file,
                    "logs",
                    "--no-color",
                    "--timestamps",
                    "--tail",
                    "200",
                )
            ),
            "compose-model.txt": run(
                compose_command(project, env_file, "config", "--no-interpolate")
            ),
            "container-state.txt": container_state(project),
        }
        for filename, output in outputs.items():
            redacted = redact(output, secrets)
            if any(value in redacted for value in secrets):
                raise DiagnosticError(f"진단 파일의 비밀값 제거에 실패했습니다: {filename}")
            write_private(destination / filename, redacted)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="컨테이너 스택 장애 진단 자료 수집")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    output = args.output.expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    try:
        collect(args.project, args.env_file.resolve(), output)
    except (DiagnosticError, OSError, subprocess.SubprocessError) as error:
        print(f"진단 자료 수집 실패: {error}", file=sys.stderr)
        return 2
    print(f"진단 자료를 저장했습니다: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
