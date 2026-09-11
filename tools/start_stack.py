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


# 이 두 집합의 문자열들은 srcs/requirements/{mariadb,wordpress}/tools/docker-entrypoint.sh 안의
# pause_after() 호출에 넘겨지는 stage 이름과 정확히 일치해야 한다 — 테스트가 "어느 시점에 멈출지"를
# 지정할 때 이 목록으로 유효성을 검증하고, 서비스별로 어떤 stage가 속하는지도 가른다
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
    # --pause-after / --pause-ready-file: 테스트 하네스(tests/runtime_stack.py)가 부트스트랩 도중
    # 특정 단계에서 멈춰 세우기 위해서만 쓰는 내부 플래그 — help=argparse.SUPPRESS로 --help 출력에서 숨겨
    # 일반 사용자에게는 존재를 드러내지 않는다
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
    # ComposeProject.run과 달리 이건 `docker compose ...`가 아니라 순수 `docker ...` CLI를 직접 호출하는
    # 얇은 래퍼 — 아래에서 개별 컨테이너를 이름으로 조회·삭제하는 건 compose 단위 개념이 아니기 때문
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
    # 부트스트랩은 고정된 이름("<프로젝트>-<서비스>-bootstrap")의 1회성 컨테이너로 실행된다(run_bootstrap 참고).
    # 이전 실행이 비정상 종료해 그 이름의 컨테이너가 남아있으면 같은 이름으로 다시 실행할 수 없으므로,
    # 지우기 전에 라벨을 확인해 "정말 우리가 이전에 만든 그 컨테이너"인지부터 검증한다 —
    # 이름이 우연히 같을 뿐인 남의 컨테이너를 실수로 강제 삭제하는 사고를 막기 위함
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
    # 여기서 만들어지는 --volume/--env 인자가 곧 entrypoint 스크립트의 pause_after()가 기대하는
    # CONTAINER_STACK_PAUSE_AFTER / CONTAINER_STACK_PAUSE_READY_FILE 환경변수와, 컨테이너가 준비 파일을
    # 써넣는 /run/container-stack-test 마운트 지점이다 — 호스트 쪽 테스트 코드는 이 볼륨을 통해
    # 컨테이너 "안"에서 파일이 생성되는 순간을 밖에서 감지해 그 시점의 상태를 검사할 수 있다
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
    # `compose run --rm --no-deps ... service bootstrap`: 평소 띄우는 장수 서비스가 아니라,
    # entrypoint 스크립트에 "bootstrap" 인자를 넘겨 초기화 코드 경로만 실행하는 1회성 컨테이너를 만든다.
    # --rm으로 끝나면 자동 삭제, --no-deps로 의존 서비스는 함께 띄우지 않는다.
    # input_data=payload가 그대로 컨테이너의 표준입력이 되어, entrypoint의 bootstrap()이 읽는 비밀번호 줄이 된다
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
    # `up --wait`: 컨테이너가 뜨는 것만이 아니라 healthcheck가 healthy로 바뀔 때까지 Compose가 알아서 기다려준다 —
    # 직접 폴링 루프를 짤 필요 없이 Compose 자체 기능에 위임
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
    # 이미 떠 있으면(재기동 케이스) 부트스트랩을 다시 돌리지 않는다 — 볼륨이 이미 초기화된 경우의
    # 멱등 처리는 entrypoint의 bootstrap() 안(마커 파일 확인)에도 한 겹 더 있지만, 여기서는 애초에
    # 실행 중인 서비스를 건드리지 않도록 한 단계 앞서 걸러낸다
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
    # 장수 wordpress/nginx 컨테이너를 먼저 멈춰서, 부트스트랩용 1회성 wordpress 컨테이너가 같은 공유 볼륨
    # (wp-config, wp-content 등)을 건드리는 동안 실제 서비스 프로세스가 동시에 그 파일들을 쓰지 않게 한다
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
    # nullcontext(): "아무 것도 하지 않는" 컨텍스트 매니저 — acquire_lock=False일 때도 아래 with 블록을
    # if/else로 분기하지 않고 그대로 재사용하기 위한 표준 라이브러리 유틸리티.
    # (테스트 코드가 더 큰 잠금을 이미 잡고 있을 때 이중으로 잠그지 않으려고 acquire_lock=False를 쓴다)
    lock = project_operation_lock(project.project) if acquire_lock else nullcontext()
    with lock:
        # secrets를 호출부가 이미 갖고 있으면(테스트 등) 그대로 쓰고, 없으면 파일에서 새로 읽는다 —
        # 매번 파일 I/O를 강제하지 않고 값을 주입할 수 있게 열어둔 구조
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


# 이 모듈이 다른 파일에서 import될 때는 실행되지 않고, `python3 start_stack.py`처럼 직접 실행될 때만
# main()이 호출되게 하는 파이썬의 표준 진입점 관용구. SystemExit(main())은 main()의 반환값(0/1)을
# 그대로 프로세스 종료 코드로 넘긴다
if __name__ == "__main__":
    raise SystemExit(main())
