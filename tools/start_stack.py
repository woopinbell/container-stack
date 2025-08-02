#!/usr/bin/env python3
"""비밀값을 런타임 컨테이너에 남기지 않고 Compose 스택을 시작합니다."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys

from stack_runtime import (
    ComposeProject,
    DEFAULT_COMPOSE_FILE,
    StackRuntimeError,
    load_secret_values,
    project_operation_lock,
    secret_payload,
)


DATABASE_STAGES = {
    "system-tables",
    "temporary-server",
    "database-state",
    "database-marker",
    "database-publish",
}
APPLICATION_STAGES = {
    "core-files",
    "wordpress-config",
    "wordpress-core",
    "wordpress-users",
    "wordpress-marker",
}
BOOTSTRAP_LABEL = "com.container-stack.bootstrap"
BUILD_TIMEOUT_SECONDS = 900


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="초기화용 컨테이너에만 표준 입력으로 비밀값을 전달해 스택을 시작합니다."
    )
    result.add_argument("action", choices=("start", "database", "application"))
    result.add_argument("--project", required=True)
    result.add_argument("--env-file", type=Path, required=True)
    result.add_argument(
        "--compose-file",
        type=Path,
        default=DEFAULT_COMPOSE_FILE,
    )
    result.add_argument("--build", action="store_true")
    result.add_argument("--wait-timeout", type=int, default=300)
    result.add_argument(
        "--pause-after",
        choices=sorted(DATABASE_STAGES | APPLICATION_STAGES),
        help=argparse.SUPPRESS,
    )
    result.add_argument(
        "--pause-ready-file",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return result


def docker(
    *arguments: str,
    capture: bool = False,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=check,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise StackRuntimeError(
            f"Docker 명령이 {timeout}초 안에 끝나지 않았습니다"
        ) from error


def remove_stale_bootstrap(project: ComposeProject, service: str) -> None:
    name = f"{project.project}-{service}-bootstrap"
    inspected = docker(
        "container",
        "inspect",
        name,
        capture=True,
        check=False,
    )
    if inspected.returncode != 0:
        return
    try:
        entries = json.loads(inspected.stdout)
        labels = entries[0]["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise StackRuntimeError(
            f"초기화 컨테이너의 소유권을 확인할 수 없습니다: {name}"
        ) from error
    if (
        labels.get("com.docker.compose.project") != project.project
        or labels.get(BOOTSTRAP_LABEL) != service
    ):
        raise StackRuntimeError(
            f"다른 컨테이너가 초기화 이름을 사용 중입니다: {name}"
        )
    docker("container", "rm", "--force", name)


def pause_arguments(
    service: str,
    stage: str | None,
    ready_file: Path | None,
) -> list[str]:
    if stage is None:
        if ready_file is not None:
            raise StackRuntimeError(
                "--pause-ready-file은 --pause-after와 함께 사용해야 합니다"
            )
        return []
    service_stages = DATABASE_STAGES if service == "mariadb" else APPLICATION_STAGES
    if stage not in service_stages:
        return []
    if ready_file is None:
        raise StackRuntimeError("--pause-after에는 --pause-ready-file이 필요합니다")
    path = ready_file.expanduser().resolve()
    if path.exists():
        raise StackRuntimeError(f"일시정지 준비 파일이 이미 존재합니다: {path}")
    if not path.parent.is_dir():
        raise StackRuntimeError(
            f"일시정지 준비 파일 디렉터리가 없습니다: {path.parent}"
        )
    return [
        "--volume",
        f"{path.parent}:/run/container-stack-test",
        "--env",
        f"CONTAINER_STACK_PAUSE_AFTER={stage}",
        "--env",
        f"CONTAINER_STACK_PAUSE_READY_FILE={path.name}",
    ]


def run_bootstrap(
    project: ComposeProject,
    service: str,
    payload: bytes,
    *,
    pause_after_stage: str | None,
    pause_ready_file: Path | None,
) -> None:
    remove_stale_bootstrap(project, service)
    name = f"{project.project}-{service}-bootstrap"
    extra = pause_arguments(
        service,
        pause_after_stage,
        pause_ready_file,
    )
    project.run(
        "run",
        "--rm",
        "--no-deps",
        "--no-TTY",
        "--name",
        name,
        "--label",
        f"{BOOTSTRAP_LABEL}={service}",
        *extra,
        service,
        "bootstrap",
        input_data=payload,
        timeout=project.timeout,
    )


def wait_for_services(project: ComposeProject, *services: str) -> None:
    project.run(
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(project.timeout),
        *services,
        timeout=project.timeout + 30,
    )


def start_database(
    project: ComposeProject,
    secrets: dict[str, str],
    *,
    build: bool,
    pause_after_stage: str | None,
    pause_ready_file: Path | None,
) -> None:
    if build:
        project.run(
            "build",
            "mariadb",
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    if "mariadb" not in project.running_services():
        run_bootstrap(
            project,
            "mariadb",
            secret_payload(
                secrets["db_root_password"],
                secrets["db_password"],
            ),
            pause_after_stage=pause_after_stage,
            pause_ready_file=pause_ready_file,
        )
    wait_for_services(project, "mariadb")


def start_application(
    project: ComposeProject,
    secrets: dict[str, str],
    *,
    build: bool,
    pause_after_stage: str | None,
    pause_ready_file: Path | None,
) -> None:
    if "mariadb" not in project.running_services():
        raise StackRuntimeError(
            "MariaDB가 실행 중이 아닙니다. database 단계부터 실행하십시오"
        )
    if build:
        project.run(
            "build",
            "wordpress",
            "nginx",
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    project.run("stop", "nginx", "wordpress")
    run_bootstrap(
        project,
        "wordpress",
        secret_payload(
            secrets["db_password"],
            secrets["wp_admin_password"],
            secrets["wp_user_password"],
        ),
        pause_after_stage=pause_after_stage,
        pause_ready_file=pause_ready_file,
    )
    wait_for_services(project, "wordpress", "nginx")


def run_action(
    project: ComposeProject,
    action: str,
    *,
    secrets: dict[str, str] | None = None,
    build: bool = False,
    pause_after_stage: str | None = None,
    pause_ready_file: Path | None = None,
    acquire_lock: bool = True,
) -> None:
    if action not in {"start", "database", "application"}:
        raise StackRuntimeError(f"알 수 없는 시작 단계입니다: {action}")
    if (
        pause_after_stage in DATABASE_STAGES
        and action == "application"
    ) or (
        pause_after_stage in APPLICATION_STAGES
        and action == "database"
    ):
        raise StackRuntimeError(
            "요청한 일시정지 단계가 선택한 시작 단계에 속하지 않습니다"
        )
    lock = project_operation_lock(project.project) if acquire_lock else nullcontext()
    with lock:
        resolved_secrets = (
            secrets if secrets is not None else load_secret_values(project)
        )
        if action in {"start", "database"}:
            start_database(
                project,
                resolved_secrets,
                build=build,
                pause_after_stage=pause_after_stage,
                pause_ready_file=pause_ready_file,
            )
        if action in {"start", "application"}:
            start_application(
                project,
                resolved_secrets,
                build=build,
                pause_after_stage=pause_after_stage,
                pause_ready_file=pause_ready_file,
            )


def execute(arguments: argparse.Namespace) -> None:
    project = ComposeProject(
        arguments.project,
        arguments.env_file,
        arguments.compose_file,
        timeout=arguments.wait_timeout,
    )
    run_action(
        project,
        arguments.action,
        build=arguments.build,
        pause_after_stage=arguments.pause_after,
        pause_ready_file=arguments.pause_ready_file,
    )


def main() -> int:
    arguments = parser().parse_args()
    try:
        execute(arguments)
    except (
        StackRuntimeError,
        OSError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"start-stack: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
