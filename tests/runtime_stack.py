#!/usr/bin/env python3
"""격리된 Compose 프로젝트에서 컨테이너 스택의 실제 동작을 검사합니다."""

# [INTV:ARCH] tests/validate_stack.py가 "소스 코드가 그렇게 생겼는지"만 보는 정적 검사라면, 이 파일은
# 실제로 이미지를 빌드하고 컨테이너를 띄워 동작을 검사하는 런타임 시나리오다(CI가 이 파일을 6가지
# 시나리오 인자로 6번 나눠 호출한다 — .github/workflows/container-stack.yml 참고). RuntimeStack
# 하나가 완전히 격리된 Compose 프로젝트 하나(고유한 프로젝트 이름·포트·비밀 파일·.env)를 표현하고,
# tools/*.py를 라이브러리로 import해서 함수를 직접 부르는 대신 실제 사용자처럼 `python3 tools/xxx.py`를
# 서브프로세스로 실행해 검사한다 — CLI 인터페이스 자체가 문서화된 대로 동작하는지까지 확인하는
# 블랙박스 통합 테스트를 지향하는 설계다. 시나리오는 6가지(main() 하단 참고): bootstrap(부트스트랩
# 도중 강제종료 후 복구), e2e(정상 경로 종단 검증), persistence(재기동 뒤 데이터 보존),
# backup-restore(백업/복원과 그 실패·중단 처리), rotation(비밀값 회전과 롤백), operations(리소스 제한·
# 네트워크 격리·진단 도구 등 운영 기능).
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROCESS_TIMEOUT_SECONDS = 120
CONTROL_TIMEOUT_SECONDS = 600
BUILD_TIMEOUT_SECONDS = 1200
BACKUP_TOOL_TIMEOUT_SECONDS = 1200
PORT_RETRY_LIMIT = 3
PORT_CONFLICT_MARKERS = (
    "address already in use",
    "bind: address already in use",
    "port is already allocated",
    "failed to bind host port",
)


class StackError(RuntimeError):
    pass


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise StackError(f"필요한 명령을 찾을 수 없습니다: {name}")


def reserve_port() -> int:
    # [INTV:EDGE] 포트 0으로 바인드하면 커널이 현재 비어있는 임시 포트를 하나 골라준다 — 여러
    # 시나리오를 동시에 돌려도 서로 다른 HTTPS 포트를 쓰게 되어 충돌하지 않는다(다만 소켓을
    # 곧바로 닫으므로 그 사이 다른 프로세스가 같은 포트를 채갈 여지는 남는다 — 아래 start()의
    # 재시도 로직이 그 경우를 처리).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


# [INTV:ARCH] write_private/replace_private: tools/stack_runtime.py, tools/rotate_secrets.py에서
# 이미 쓰인 것과 같은 "0600 권한으로 새로 만들기" / "임시 파일에 쓰고 원자적으로 교체하기" 패턴을
# 테스트 코드 쪽에서 독립적으로 재구현한 것 — 테스트가 만드는 가짜 비밀 파일·.env도 운영 코드가
# 기대하는 것과 같은 권한 모델을 지켜야 검증이 의미가 있기 때문.
def write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.write("\n")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise StackError(f"비공개 파일 권한이 0600이 아닙니다: {path}")


def replace_private(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class RuntimeStack:
    def __init__(
        self,
        *,
        keep: bool,
        diagnostics_dir: Path | None,
        project_record_dir: Path | None = None,
        credential_values: dict[str, str] | None = None,
        image_prefix: str | None = None,
        owns_images: bool = True,
    ) -> None:
        self.keep = keep
        self.diagnostics_dir = diagnostics_dir
        self.project_record_dir = project_record_dir
        self.temp = Path(tempfile.mkdtemp(prefix="container-stack-e2e-"))
        self.temp.chmod(0o700)
        # 이 이름 형식(container-stack-<PID>-<임의 hex 6자>)은 tools/cleanup_test_resources.py의
        # PROJECT_PATTERN 정규식과 정확히 맞아떨어져야 한다 — 검증이 비정상 종료해 자원을 못 지워도,
        # 그 도구가 나중에 "이건 테스트가 만든 프로젝트다"라고 식별해 회수할 수 있게 하기 위함
        self.project = f"container-stack-{os.getpid()}-{secrets.token_hex(3)}"
        self.domain = "stack.test"
        self.port = reserve_port()
        self.env_file = self.temp / ".env"
        self.started = False
        self.image_prefix = image_prefix or f"{self.project}-image"
        self.owns_images = owns_images
        self.credential_values = credential_values or {
            "db_root_password.txt": f"root#-{secrets.token_urlsafe(24)}",
            "db_password.txt": f"db#-{secrets.token_urlsafe(24)}",
            "wp_admin_password.txt": f"admin-{secrets.token_urlsafe(24)}",
            "wp_user_password.txt": f"user-{secrets.token_urlsafe(24)}",
        }
        try:
            self._record_project()
            self._prepare_environment()
        except Exception:
            shutil.rmtree(self.temp, ignore_errors=True)
            raise

    def _record_project(self) -> None:
        # tools/verify_stack.py가 넘겨주는 기록 디렉터리에 프로젝트 이름을 파일 하나로 남겨둔다 —
        # 이 시나리오가 도중에 죽어 자기 손으로 자원을 못 치워도, 그 디렉터리를 근거로
        # cleanup_test_resources.py가 정확히 이 프로젝트만 찾아 정리할 수 있게 하는 안전망
        if self.project_record_dir is None:
            return
        directory = self.project_record_dir
        if directory.is_symlink():
            raise StackError(f"프로젝트 기록 경로가 심볼릭 링크입니다: {directory}")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
            raise StackError(f"프로젝트 기록 경로 권한이 안전하지 않습니다: {directory}")
        write_private(directory / self.project, self.project)

    def _prepare_environment(self) -> None:
        for filename, value in self.credential_values.items():
            write_private(self.temp / filename, value)

        self.environment_values = {
            "DOMAIN_NAME": self.domain,
            "WORDPRESS_URL": f"https://{self.domain}:{self.port}",
            "HTTPS_BIND_ADDRESS": "127.0.0.1",
            "HTTPS_PORT": str(self.port),
            "STACK_IMAGE_PREFIX": self.image_prefix,
            "STACK_IMAGE_TAG": "local",
            "MYSQL_DATABASE": "wordpress",
            "MYSQL_USER": "wpuser",
            "WORDPRESS_TITLE": "Container Stack E2E",
            "WORDPRESS_ADMIN_USER": "administrator",
            "WORDPRESS_ADMIN_EMAIL": "administrator@example.test",
            "WORDPRESS_USER": "author",
            "WORDPRESS_USER_EMAIL": "author@example.test",
            "DB_ROOT_PASSWORD_FILE": str(self.temp / "db_root_password.txt"),
            "DB_PASSWORD_FILE": str(self.temp / "db_password.txt"),
            "WP_ADMIN_PASSWORD_FILE": str(self.temp / "wp_admin_password.txt"),
            "WP_USER_PASSWORD_FILE": str(self.temp / "wp_user_password.txt"),
        }
        self._write_environment(create=True)

    def _write_environment(self, *, create: bool = False) -> None:
        content = "".join(
            f"{key}={value}\n" for key, value in self.environment_values.items()
        )
        if create:
            write_private(self.env_file, content.rstrip("\n"))
        else:
            replace_private(self.env_file, content)

    def _select_new_port(self) -> None:
        self.port = reserve_port()
        self.environment_values["HTTPS_PORT"] = str(self.port)
        self.environment_values["WORDPRESS_URL"] = (
            f"https://{self.domain}:{self.port}"
        )
        self._write_environment()

    # compose_command/run_compose는 tools/stack_runtime.py의 ComposeProject와 거의 같은 모양이지만
    # 일부러 그걸 import하지 않고 독자적으로 다시 구현했다 — 이 테스트가 운영 도구의 "내부 구현"이 아니라
    # "명령행 동작"을 검증 대상으로 삼기 때문에, 검증 코드 자체가 그 구현에 의존하지 않게 분리해 둔 것
    def compose_command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
            "--file",
            str(COMPOSE_FILE),
            *arguments,
        ]

    def run_compose(
        self,
        *arguments: str,
        input_data: str | None = None,
        capture: bool = False,
        check: bool = True,
        timeout: int = CONTROL_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.compose_command(*arguments),
                cwd=ROOT,
                input=input_data,
                check=check,
                text=True,
                capture_output=capture,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            operation = arguments[0] if arguments else "command"
            raise StackError(
                f"Compose {operation} 명령이 {timeout}초 안에 끝나지 않았습니다"
            ) from error

    def _start_command(
        self,
        action: str,
        *,
        build: bool = False,
        pause_after: str | None = None,
        pause_ready_file: Path | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            str(ROOT / "tools" / "start_stack.py"),
            action,
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--wait-timeout",
            "300",
        ]
        if build:
            command.append("--build")
        if pause_after is not None and pause_ready_file is not None:
            command.extend(("--pause-after", pause_after))
            command.extend(("--pause-ready-file", str(pause_ready_file)))
        return command

    def _run_start(
        self,
        action: str,
        *,
        build: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                self._start_command(action, build=build),
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=BUILD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise StackError("스택 기동이 제한 시간 안에 끝나지 않았습니다") from error
        if check and result.returncode != 0:
            raise StackError(
                f"스택 기동이 실패했습니다: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def start(self) -> None:
        self.started = True
        # reserve_port()로 고른 포트가 실제 docker compose up 시점에는 이미 다른 프로세스가 차지했을 수
        # 있다(예약과 사용 사이의 경합) — 에러 메시지에 "포트가 이미 쓰이는 중" 계열 문구가 있을 때만
        # 새 포트를 골라 재시도하고, 그 외의 실패는 재시도 없이 바로 올려서 진짜 버그를 감추지 않는다
        for attempt in range(PORT_RETRY_LIMIT):
            result = self._run_start("start", build=True, check=False)
            if result.returncode == 0:
                return
            output = (result.stdout + result.stderr).lower()
            if (
                attempt + 1 >= PORT_RETRY_LIMIT
                or not any(marker in output for marker in PORT_CONFLICT_MARKERS)
            ):
                raise StackError(
                    f"스택 기동이 실패했습니다: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            self.run_compose(
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "20",
                check=False,
            )
            self._select_new_port()

    def wordpress(self, *arguments: str, capture: bool = False) -> str:
        result = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "wp",
            "--allow-root",
            "--path=/var/www/html",
            *arguments,
            capture=capture,
        )
        return result.stdout.strip() if capture else ""

    def project_resources(self) -> dict[str, set[str]]:
        resources: dict[str, set[str]] = {}
        for kind in ("container", "volume", "network"):
            command = ["docker", kind, "ls"]
            if kind == "container":
                command.append("--all")
            command.extend(
                (
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                    "--format",
                    "{{.Names}}" if kind == "container" else "{{.Name}}",
                )
            )
            result = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            identifiers = {line for line in result.stdout.splitlines() if line}
            if identifiers:
                resources[kind] = identifiers
        return resources

    def project_volumes(self) -> set[str]:
        return self.project_resources().get("volume", set())

    def verify_services_running(self) -> None:
        result = self.run_compose(
            "ps", "--status", "running", "--services", capture=True
        )
        running = {line for line in result.stdout.splitlines() if line}
        expected = {"mariadb", "wordpress", "nginx"}
        if running != expected or self.fetch("/healthz").strip() != "ok":
            raise StackError(
                f"관리 작업 뒤 서비스가 모두 복구되지 않았습니다: {sorted(running)}"
            )

    # tools/rotate_secrets.py의 verify_runtime_secret_boundary()와 같은 항목(예전 시크릿 마운트 부재,
    # nginx에서 wp-config 격리, 컨테이너 선언 환경변수·프로세스 환경·프로세스 인자에 비밀 이름/값 부재)을
    # 이 테스트 하네스 나름대로 다시 구현한 것 — 회전 시나리오뿐 아니라 e2e·bootstrap 등 다른 모든
    # 시나리오에서도 재사용하기 위함. 추가로 wp-config.php "내용"까지 직접 읽어 DB 비밀번호는 있어야 하고
    # 나머지 비밀값은 없어야 함을 확인하고, `docker compose logs` 전체에도 비밀값이 없는지까지 검사한다
    def assert_runtime_secret_boundary(
        self, expected_values: dict[str, str] | None = None
    ) -> None:
        values = expected_values or self.credential_values
        forbidden_names = (
            "MYSQL_ROOT_PASSWORD",
            "MYSQL_PASSWORD",
            "WORDPRESS_DB_PASSWORD",
            "WORDPRESS_ADMIN_PASSWORD",
            "WORDPRESS_USER_PASSWORD",
        )
        observed = ""
        for service in ("mariadb", "wordpress", "nginx"):
            inspected = self.inspect_service(service)
            mounts = inspected.get("Mounts")
            if not isinstance(mounts, list):
                raise StackError(f"{service} 마운트 정보를 읽지 못했습니다")
            destinations = {
                str(mount.get("Destination", ""))
                for mount in mounts
                if isinstance(mount, dict)
            }
            if any(
                destination == "/run/secrets"
                or destination.startswith("/run/secrets/")
                for destination in destinations
            ):
                raise StackError(f"{service} 런타임에 비밀 파일이 마운트되었습니다")
            if service == "wordpress" and "/var/www/config" not in destinations:
                raise StackError("WordPress 설정 전용 볼륨이 마운트되지 않았습니다")
            if service == "nginx":
                if "/var/www/config" in destinations:
                    raise StackError("nginx가 WordPress 설정 전용 볼륨을 볼 수 있습니다")
                hidden_config = self.run_compose(
                    "exec",
                    "--no-TTY",
                    "nginx",
                    "sh",
                    "-ceu",
                    "test -L /var/www/html/wp-config.php; "
                    "test ! -e /var/www/html/wp-config.php; "
                    "test ! -e /var/www/config/wp-config.php",
                    capture=True,
                    check=False,
                )
                if hidden_config.returncode != 0:
                    raise StackError("nginx에서 WordPress DB 설정 파일이 격리되지 않았습니다")

            config = inspected.get("Config")
            if not isinstance(config, dict):
                raise StackError(f"{service} 실행 환경을 읽지 못했습니다")
            environment = config.get("Env") or []
            if not isinstance(environment, list):
                raise StackError(f"{service} 실행 환경 형식이 올바르지 않습니다")
            config_text = "\n".join(str(item) for item in environment)
            if any(name in config_text for name in forbidden_names):
                raise StackError(f"{service} 런타임 환경에 비밀번호 변수가 남았습니다")
            observed += config_text

            process_environment = self.run_compose(
                "exec",
                "--no-TTY",
                service,
                "sh",
                "-ceu",
                "for path in /proc/[0-9]*/environ; do "
                "test -r \"$path\" || continue; "
                "tr '\\000' '\\n' <\"$path\" || true; "
                "done",
                capture=True,
            ).stdout
            if any(name in process_environment for name in forbidden_names):
                raise StackError(f"{service} 프로세스 환경에 비밀번호 변수가 남았습니다")
            observed += process_environment

            container_id = str(inspected.get("Id", ""))
            process_arguments = subprocess.run(
                ["docker", "top", container_id, "-eo", "pid,args"],
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            ).stdout
            observed += process_arguments

        for value in values.values():
            if value and value in observed:
                raise StackError("런타임 환경이나 프로세스 인자에 비밀값이 남았습니다")

        wordpress_config = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "cat",
            "/var/www/config/wp-config.php",
            capture=True,
        ).stdout
        if values["db_password.txt"] not in wordpress_config:
            raise StackError("wp-config.php에 애플리케이션 DB 자격증명이 없습니다")
        for filename in (
            "db_root_password.txt",
            "wp_admin_password.txt",
            "wp_user_password.txt",
        ):
            if values[filename] in wordpress_config:
                raise StackError(f"wp-config.php에 불필요한 비밀값이 남았습니다: {filename}")

        logs = self.run_compose("logs", "--no-color", capture=True, check=False)
        log_output = logs.stdout + logs.stderr
        for value in values.values():
            if value and value in log_output:
                raise StackError("Compose 로그에 비밀값이 남았습니다")

    def inspect_service(self, service: str) -> dict[str, object]:
        container_id = self.run_compose(
            "ps", "--quiet", service, capture=True
        ).stdout.strip()
        if not container_id or "\n" in container_id:
            raise StackError(f"{service} 컨테이너를 하나로 식별하지 못했습니다")
        result = subprocess.run(
            ["docker", "inspect", container_id],
            check=True,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        inspected = json.loads(result.stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise StackError(f"{service} 컨테이너 검사 결과가 예상과 다릅니다")
        return inspected[0]

    def fetch(self, path: str) -> str:
        # self.domain("stack.test")은 실제 DNS에 등록된 도메인이 아니다 — curl의 --resolve로
        # "이 호스트:포트로의 접속은 무조건 127.0.0.1로 보내라"고 강제해, 진짜 도메인 없이도
        # nginx의 자체 서명 인증서(CN=DOMAIN_NAME)와 WORDPRESS_URL이 기대하는 호스트명으로 접근할 수 있게 한다.
        # --insecure는 바로 그 자체 서명 인증서라 브라우저 신뢰 체인 검증을 통과할 수 없기 때문에 끄는 것
        url = f"https://{self.domain}:{self.port}{path}"
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--insecure",
                "--noproxy",
                "*",
                "--resolve",
                f"{self.domain}:{self.port}:127.0.0.1",
                url,
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        return result.stdout

    # [INTV:ARCH] CI가 e2e 시나리오로 호출하는 진입점 — "정상 경로가 처음부터 끝까지 실제로 동작
    # 하는가"를 확인하는, 6개 시나리오 중 가장 기본적인 것. 순서: 포트 충돌 복구 확인 -> 레거시
    # 설정 마이그레이션 확인 -> 비밀값이 런타임에 새어나가지 않는지 확인 -> 고정해둔 WordPress/
    # WP-CLI 버전이 실제 실행 버전과 일치하는지 -> /healthz 응답 -> 글을 하나 실제로 작성하고 그
    # 내용이 HTTPS로 다시 읽히는지까지(가장 사용자에 가까운 종단 검증).
    def verify_e2e(self) -> None:
        # [INTV:EDGE] 예약해둔 포트를 일부러 다른 소켓으로 미리 점유해, start()의 포트 충돌 감지·
        # 재시도 경로가 실제로 동작하는지부터 확인한다 — "테스트 대상 기능을 위한 테스트"가 시나리오
        # 맨 앞에 끼워져 있는 셈.
        blocked_port = self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", blocked_port))
            listener.listen()
            self.start()
        if self.port == blocked_port:
            raise StackError("HTTPS 포트 충돌 뒤 새 포트를 선택하지 않았습니다")
        self._verify_legacy_config_migration()
        self.assert_runtime_secret_boundary()
        if self.wordpress("core", "version", capture=True) != "6.7.1":
            raise StackError("고정한 WordPress 코어 버전과 실행 버전이 다릅니다")
        if "WP-CLI 2.11.0" not in self.wordpress("cli", "version", capture=True):
            raise StackError("고정한 WP-CLI 버전과 실행 버전이 다릅니다")
        if self.fetch("/healthz").strip() != "ok":
            raise StackError("nginx 상태 응답이 예상과 다릅니다")

        nonce = secrets.token_hex(8)
        title = f"종단 검증 {nonce}"
        content = f"nginx-fpm-wordpress-mariadb-{nonce}"
        post_id = self.wordpress(
            "post",
            "create",
            f"--post_title={title}",
            f"--post_content={content}",
            "--post_status=publish",
            "--porcelain",
            capture=True,
        )
        if not post_id.isdigit():
            raise StackError(f"WordPress가 유효한 글 번호를 반환하지 않았습니다: {post_id!r}")
        page = self.fetch(f"/?p={post_id}")
        if title not in page or content not in page:
            raise StackError("HTTPS 응답에서 방금 저장한 글을 찾지 못했습니다")

        database_value = self.wordpress(
            "db",
            "query",
            f"SELECT post_content FROM wp_posts WHERE ID={post_id}",
            "--skip-column-names",
            capture=True,
        )
        if content not in database_value:
            raise StackError("MariaDB 조회 결과가 WordPress 입력과 다릅니다")
        print(f"isolated end-to-end check passed: project={self.project} port={self.port}")

    # srcs/requirements/wordpress/tools/docker-entrypoint.sh의 prepare_config_location()이 다루는
    # "예전 레이아웃(웹 루트에 wp-config.php 실물 파일)에서 새 레이아웃(별도 볼륨 + 심볼릭 링크)으로
    # 마이그레이션"하는 경로를 실제로 재현해 검증한다 — 1회성 컨테이너 안에서 직접 파일을 예전 모습으로
    # 만들어놓은 뒤, application 부트스트랩을 다시 돌려 그 마이그레이션 로직이 제대로 수렴시키는지 확인
    def _verify_legacy_config_migration(self) -> None:
        self.run_compose("stop", "nginx", "wordpress")
        self.run_compose(
            "run",
            "--rm",
            "--no-TTY",
            "--no-deps",
            "--entrypoint",
            "sh",
            "wordpress",
            "-ceu",
            "cp -p /var/www/config/wp-config.php /var/www/html/.wp-config.legacy; "
            "rm -f /var/www/html/wp-config.php /var/www/config/wp-config.php; "
            "mv /var/www/html/.wp-config.legacy /var/www/html/wp-config.php",
        )
        self._run_start("application")
        migrated = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "sh",
            "-ceu",
            "test -L /var/www/html/wp-config.php; "
            "test \"$(readlink /var/www/html/wp-config.php)\" = "
            "/var/www/config/wp-config.php; "
            "test -f /var/www/config/wp-config.php; "
            "test \"$(stat -c %a /var/www/config/wp-config.php)\" = 600",
            capture=True,
            check=False,
        )
        if migrated.returncode != 0:
            raise StackError("기존 WordPress 설정을 전용 볼륨으로 옮기지 못했습니다")

    def _verify_persistent_values(
        self, *, post_id: str, title: str, content: str, filename: str, file_value: str
    ) -> None:
        page = self.fetch(f"/?p={post_id}")
        if title not in page or content not in page:
            raise StackError("재기동 뒤 게시물 내용이 보존되지 않았습니다")
        option = self.wordpress(
            "option", "get", "container_stack_persistence", capture=True
        )
        if option != content:
            raise StackError("재기동 뒤 WordPress 옵션 값이 보존되지 않았습니다")
        if self.fetch(f"/wp-content/uploads/{filename}") != file_value:
            raise StackError("재기동 뒤 업로드 파일이 보존되지 않았습니다")

    # [INTV:ARCH] CI가 persistence 시나리오로 호출하는 진입점 — 컨테이너를 "다시 띄웠을 때도" 데이터가
    # 남아있는지 검증한다(단순히 떠 있는 동안 잘 동작하는지와는 다른 질문). 글/옵션값/업로드 파일을
    # 만들어두고 스택을 내렸다 올린 뒤(아래에서 볼륨 개수 확인 후 재시작), 그 값들이 그대로 읽히는지
    # 확인하는 흐름 — 이름 있는 볼륨(named volume)에 실제로 마운트가 걸려 있지 않으면 컨테이너
    # 재생성 시 데이터가 통째로 날아가는 실수를 잡아낸다.
    def verify_persistence(self) -> None:
        self.start()
        nonce = secrets.token_hex(8)
        title = f"영속성 검증 {nonce}"
        content = f"persistent-database-{nonce}"
        filename = f"persistence-{nonce}.txt"
        file_value = f"persistent-volume-{nonce}\n"
        post_id = self.wordpress(
            "post",
            "create",
            f"--post_title={title}",
            f"--post_content={content}",
            "--post_status=publish",
            "--porcelain",
            capture=True,
        )
        self.wordpress("option", "update", "container_stack_persistence", content)
        php_value = file_value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        php_file = filename.replace('"', '\\"')
        self.wordpress(
            "eval",
            "wp_mkdir_p(WP_CONTENT_DIR . '/uploads'); "
            f'file_put_contents(WP_CONTENT_DIR . "/uploads/{php_file}", "{php_value}");',
        )
        # docker-compose.yml의 volumes: 아래 선언된 이름 있는 볼륨 개수(mariadb_data, wordpress_data,
        # wordpress_config)와 정확히 일치해야 한다
        initial_volumes = self.project_volumes()
        if len(initial_volumes) != 3:
            raise StackError(f"예상한 영구 볼륨 세 개를 찾지 못했습니다: {initial_volumes}")
        self._verify_persistent_values(
            post_id=post_id,
            title=title,
            content=content,
            filename=filename,
            file_value=file_value,
        )

        # 1단계: 컨테이너 프로세스만 재시작(restart) — 볼륨은 애초에 안 바뀌므로 데이터가 보존되는 게 당연
        self.run_compose("restart", "mariadb", "wordpress", "nginx")
        self.run_compose("up", "--detach", "--wait", "--wait-timeout", "240")
        self._verify_persistent_values(
            post_id=post_id,
            title=title,
            content=content,
            filename=filename,
            file_value=file_value,
        )

        # 2단계: 컨테이너 자체를 내렸다가(down) 다시 만든다(up) — 이번엔 이름 있는 볼륨은 그대로 재사용되고
        # 컨테이너만 새로 생성되는 것이 Compose의 계약이므로, 그 계약이 실제로 지켜지는지를 볼륨 식별자
        # 집합이 이전과 같은지(같은 볼륨을 재사용) 비교해 확인한다
        self.run_compose("down", "--remove-orphans", "--timeout", "20")
        self.run_compose("up", "--detach", "--wait", "--wait-timeout", "240")
        recreated_volumes = self.project_volumes()
        if recreated_volumes != initial_volumes:
            raise StackError("서비스 재생성 과정에서 영구 볼륨이 교체되었습니다")
        self._verify_persistent_values(
            post_id=post_id,
            title=title,
            content=content,
            filename=filename,
            file_value=file_value,
        )
        print(f"restart and recreation persistence passed: project={self.project}")

    def _backup_tool(
        self,
        operation: str,
        project: "RuntimeStack",
        path: Path,
        *,
        fail_after: str | None = None,
        environment: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # subprocess.run이 아니라 Popen + communicate(timeout=...)를 직접 쓰는 이유는, 시간 초과 시
        # subprocess.run처럼 곧바로 강제 종료(kill)하는 대신 _terminate_process로 SIGTERM부터 보내
        # 점진적으로 종료를 시도할 여지를 남기기 위함 — 백업/복원 도구가 자기 신호 핸들러로 정리할
        # 기회를 준 뒤에야 최후 수단으로 죽인다
        path_option = "--output" if operation == "backup" else "--input"
        command = [
            sys.executable,
            str(ROOT / "tools" / "stack_backup.py"),
            operation,
            "--project",
            project.project,
            "--env-file",
            str(project.env_file),
            path_option,
            str(path),
        ]
        if fail_after is not None:
            command.extend(("--fail-after", fail_after))
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = process.communicate(timeout=BACKUP_TOOL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = self._terminate_process(process)
            raise StackError(
                f"{operation} 도구가 {BACKUP_TOOL_TIMEOUT_SECONDS}초 안에 끝나지 않았습니다: "
                f"{stderr.strip() or stdout.strip()}"
            ) from error
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check and result.returncode != 0:
            raise StackError(
                f"{operation} 도구가 실패했습니다: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    # SIGTERM(정상 종료 요청) → 대기 → 그래도 안 죽으면 SIGKILL(강제 종료) → 그래도 파이프가 안 닫히면
    # 파이프를 직접 닫고 wait — 단계적으로 강도를 높여가는 표준적인 프로세스 종료 관용구
    def _terminate_process(
        self, process: subprocess.Popen[str]
    ) -> tuple[str, str]:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            return process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                return process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
                return "", "종료된 자식 프로세스가 출력 파이프를 닫지 않았습니다"

    # 아래 여러 _interrupt_*/_verify_pause_signal_race 메서드가 공유하는 폴링 루프 — 도구가
    # --pause-after/--pause-ready-file로 특정 단계에서 멈추면 그 사실을 파일 생성으로 알리는데(운영
    # 도구들의 pause_for_test와 짝을 이루는 패턴), 그 파일이 생길 때까지 기다렸다가 그 시점에 프로세스를
    # 신호로 찔러본다. time.monotonic()을 쓰는 이유는 시스템 시각이 도중에 바뀌어도(NTP 보정 등)
    # 경과 시간 계산이 흔들리지 않기 때문
    def _wait_for_ready_file(
        self,
        process: subprocess.Popen[str],
        ready_file: Path,
        description: str,
    ) -> None:
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while not ready_file.exists():
            if process.poll() is not None:
                stdout, stderr = self._terminate_process(process)
                raise StackError(
                    f"{description} 준비 전에 프로세스가 끝났습니다: "
                    f"{stderr.strip() or stdout.strip()}"
                )
            if time.monotonic() >= deadline:
                stdout, stderr = self._terminate_process(process)
                ready_file.unlink(missing_ok=True)
                raise StackError(
                    f"{description} 준비를 {PROCESS_TIMEOUT_SECONDS}초 안에 확인하지 못했습니다: "
                    f"{stderr.strip() or stdout.strip()}"
                )
            time.sleep(0.1)

    # stack_backup.py의 project_operation_lock/pause_for_test(잠금을 건 채로 테스트를 위해 일시정지하는
    # 공용 유틸리티)가 "정지해 있는 동안 종료 신호가 오는" 경합 상황에서도 준비 파일을 안전하게 지우고
    # 실패로 끝나는지를 검증한다. python -c로 즉석에서 만든 짧은 스크립트를 서브프로세스로 띄우는 이유는
    # 신호를 받는 대상이 "이 테스트 프로세스 자신"이 아니라 "독립된 별도 프로세스"여야 하기 때문 —
    # 신호 처리는 프로세스 단위라 같은 프로세스 안에서는 이 경합을 재현할 수 없다. 12번 반복하며
    # SIGINT/SIGTERM을 번갈아 보내는 것은 타이밍에 좌우되는 경합을 한 번의 시도로는 못 잡을 수 있어서다
    def _verify_pause_signal_race(self) -> None:
        script = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "from stack_backup import BackupError, operation_signal_handlers, pause_for_test",
                "try:",
                "    with operation_signal_handlers():",
                "        pause_for_test('race', 'race', Path(sys.argv[2]))",
                "except BackupError as error:",
                "    print(error, file=sys.stderr)",
                "    raise SystemExit(1)",
            )
        )
        for index in range(12):
            ready_file = self.temp / f"pause-race-{index}.ready"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT / "tools"),
                    str(ready_file),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current_signal = signal.SIGINT if index % 2 == 0 else signal.SIGTERM
            try:
                self._wait_for_ready_file(
                    process, ready_file, "신호 경합 준비 파일"
                )
                process.send_signal(current_signal)
                stdout, stderr = process.communicate(
                    timeout=PROCESS_TIMEOUT_SECONDS
                )
            finally:
                if process.poll() is None:
                    self._terminate_process(process)
            if (
                process.returncode != 1
                or current_signal.name not in stderr
                or ready_file.exists()
            ):
                raise StackError(
                    "관리 작업 준비 파일의 신호 경합 검사가 실패했습니다: "
                    f"{stderr.strip() or stdout.strip()}"
                )

    # entrypoint 스크립트의 pause_after(stage)가 걸어둔 지점까지 부트스트랩을 진행시킨 뒤, 그 순간
    # 컨테이너를 SIGKILL로 "정전처럼" 갑자기 죽인다 — 정상 종료 신호(TERM 등)로 죽이면 entrypoint의
    # trap이 정리할 기회를 가지므로, 그 기회조차 없는 가장 가혹한 실패를 재현하는 것이 목적.
    # 죽이기 전 라벨을 확인하는 것은 tools/start_stack.py의 remove_stale_bootstrap과 같은 이유 —
    # 이름이 같은 엉뚱한 컨테이너를 잘못 죽이지 않기 위함
    def _interrupt_bootstrap(
        self, *, action: str, service: str, stage: str
    ) -> None:
        ready_file = self.temp / f"bootstrap-{service}-{stage}.ready"
        command = self._start_command(
            action,
            pause_after=stage,
            pause_ready_file=ready_file,
        )
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self._wait_for_ready_file(
                process,
                ready_file,
                f"{service} {stage} 초기화 단계",
            )
            container_name = f"{self.project}-{service}-bootstrap"
            inspection = subprocess.run(
                ["docker", "container", "inspect", container_name],
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            containers = json.loads(inspection.stdout)
            labels = containers[0]["Config"]["Labels"]
            if (
                labels.get("com.docker.compose.project") != self.project
                or labels.get("com.container-stack.bootstrap") != service
            ):
                raise StackError("초기화 컨테이너의 소유권 라벨이 예상과 다릅니다")
            container_id = str(containers[0]["Id"])
            killed = subprocess.run(
                ["docker", "kill", "--signal", "KILL", container_id],
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            if killed.returncode != 0:
                state = containers[0].get("State", {})
                raise StackError(
                    f"{service} {stage} 초기화 컨테이너를 강제 종료하지 못했습니다 "
                    f"(state={state}): "
                    f"{killed.stderr.strip() or killed.stdout.strip()}"
                )
            stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                self._terminate_process(process)
            ready_file.unlink(missing_ok=True)
        if process.returncode == 0:
            raise StackError(
                f"{service} {stage} 강제 종료가 실패로 전달되지 않았습니다: "
                f"{stderr.strip() or stdout.strip()}"
            )

    def _clear_wordpress_volume(self) -> None:
        self.run_compose("stop", "nginx", "wordpress", check=False)
        self.run_compose(
            "run",
            "--rm",
            "--no-TTY",
            "--no-deps",
            "--entrypoint",
            "sh",
            "wordpress",
            "-ceu",
            "find /var/www/html -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
            "find /var/www/config -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
        )

    # [INTV:ARCH] CI가 bootstrap 시나리오로 호출하는 진입점 — 6개 시나리오 중 타임아웃이 가장 긴
    # 이유가 여기 있다. mariadb/wordpress entrypoint 스크립트가 정의한 모든 pause_after 단계 이름을
    # 하나씩 순회하며 "그 단계 직후 SIGKILL로 죽었다가, 부트스트랩을 처음부터 다시 실행했을 때
    # 올바른 최종 상태로 수렴하는가"를 확인한다 — 부트스트랩 스크립트의 멱등성(같은 단계에서 몇 번을
    # 다시 죽고 되살아나도 결과가 같아야 함)을 단계별로 촘촘하게 검증하는 것이 이 시나리오의 핵심.
    def verify_bootstrap_recovery(self) -> None:
        self.started = True
        self.run_compose(
            "build",
            "mariadb",
            "wordpress",
            "nginx",
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        database_stages = (
            "system-tables",
            "temporary-server",
            "database-state",
            "database-marker",
            "database-publish",
        )
        for index, stage in enumerate(database_stages):
            if index:
                self.run_compose(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "20",
                )
            self._interrupt_bootstrap(
                action="database",
                service="mariadb",
                stage=stage,
            )
            self._run_start("database")
            state = self.run_compose(
                "exec",
                "--no-TTY",
                "mariadb",
                "sh",
                "-ceu",
                "test -f /var/lib/mysql-volume/data/.container-stack-initialized; "
                "test ! -e /var/lib/mysql-volume/.container-stack-bootstrap",
                capture=True,
                check=False,
            )
            if state.returncode != 0:
                raise StackError(f"MariaDB {stage} 재실행 뒤 상태가 수렴하지 않았습니다")

        application_stages = (
            "core-files",
            "wordpress-config",
            "wordpress-core",
            "wordpress-users",
            "wordpress-marker",
        )
        for index, stage in enumerate(application_stages):
            if index:
                self._clear_wordpress_volume()
            self._interrupt_bootstrap(
                action="application",
                service="wordpress",
                stage=stage,
            )
            self._run_start("application")
            state = self.run_compose(
                "exec",
                "--no-TTY",
                "wordpress",
                "sh",
                "-ceu",
                "test -f /var/www/html/.container-stack-initialized; "
                "test -L /var/www/html/wp-config.php; "
                "test -f /var/www/config/wp-config.php; "
                "test -z \"$(find /var/www/html -type f "
                "\\( -name '*.bootstrap.*' -o -name '*.tmp.*' \\) -print -quit)\"; "
                "test -z \"$(find /var/www/config -type f "
                "\\( -name '*.bootstrap.*' -o -name '*.tmp.*' \\) -print -quit)\"",
                capture=True,
                check=False,
            )
            if state.returncode != 0:
                raise StackError(
                    f"WordPress {stage} 재실행 뒤 상태가 수렴하지 않았습니다"
                )
            if not self._wordpress_password_works(
                "admin", self.credential_values["wp_admin_password.txt"]
            ) or not self._wordpress_password_works(
                "user", self.credential_values["wp_user_password.txt"]
            ):
                raise StackError(
                    f"WordPress {stage} 재실행 뒤 사용자 인증이 복구되지 않았습니다"
                )

        self.assert_runtime_secret_boundary()
        self.verify_services_running()
        print("bootstrap SIGKILL recovery and secret boundary passed")

    def _interrupt_backup_tool(
        self,
        operation: str,
        project: "RuntimeStack",
        path: Path,
        *,
        pause_after: str,
        signum: signal.Signals,
    ) -> subprocess.CompletedProcess[str]:
        # _interrupt_bootstrap과 같은 발상을 백업/복원 도구에 적용한 것 — 다만 이번엔 컨테이너가 아니라
        # 이 도구(stack_backup.py) 프로세스 자체를 지정한 신호로 중단시켜, 그 신호 핸들러가 임시 파일을
        # 안전하게 정리하고 실패로 보고하는지를 검증한다
        ready_file = project.temp / f"{operation}-{pause_after}.ready"
        path_option = "--output" if operation == "backup" else "--input"
        command = [
            sys.executable,
            str(ROOT / "tools" / "stack_backup.py"),
            operation,
            "--project",
            project.project,
            "--env-file",
            str(project.env_file),
            path_option,
            str(path),
            "--pause-after",
            pause_after,
            "--pause-ready-file",
            str(ready_file),
        ]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self._wait_for_ready_file(
                process, ready_file, f"{operation} {pause_after} 일시정지"
            )
            process.send_signal(signum)
            try:
                stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                stdout, stderr = self._terminate_process(process)
                raise StackError(
                    f"{operation} 신호 정리가 {PROCESS_TIMEOUT_SECONDS}초 안에 끝나지 않았습니다"
                ) from error
        finally:
            if process.poll() is None:
                self._terminate_process(process)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if (
            result.returncode != 1
            or signum.name not in result.stderr
            or ready_file.exists()
        ):
            raise StackError(
                f"{operation} 도구가 {signum.name} 중단을 안전하게 처리하지 못했습니다: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    # project_operation_lock은 /tmp 아래 UID 기준 디렉터리에 잠금 파일을 두므로, 두 프로세스의 TMPDIR
    # 환경변수를 일부러 서로 다르게 설정해도(각자 다른 임시 디렉터리를 쓰더라도) 같은 프로젝트 이름이면
    # 여전히 같은 잠금을 공유해 서로를 막아서는지를 확인한다 — 잠금이 TMPDIR 같은 프로세스별 환경이 아니라
    # 프로젝트 이름 자체에 묶여 있다는 것을 증명하는 검사. 잠금을 쥔 프로세스는 즉석 스크립트로 만들어
    # 준비 파일을 쓴 뒤 그대로 오래 대기시켜 놓고, 그동안 실제 backup 명령이 잠금 획득에 실패하는지 확인한다
    def _verify_shared_operation_lock(self) -> None:
        first_tmp = self.temp / "lock-tmp-first"
        second_tmp = self.temp / "lock-tmp-second"
        first_tmp.mkdir(mode=0o700)
        second_tmp.mkdir(mode=0o700)
        ready_file = self.temp / "operation-lock.ready"
        holder_script = "\n".join(
            (
                "import os, signal, sys, time",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "from stack_backup import project_operation_lock",
                "ready = Path(sys.argv[3])",
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
                "try:",
                "    with project_operation_lock(sys.argv[2]):",
                "        descriptor = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
                "        os.write(descriptor, b'locked\\n')",
                "        os.fsync(descriptor)",
                "        os.close(descriptor)",
                "        time.sleep(3600)",
                "finally:",
                "    ready.unlink(missing_ok=True)",
            )
        )
        holder_environment = os.environ.copy()
        holder_environment["TMPDIR"] = str(first_tmp)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                holder_script,
                str(ROOT / "tools"),
                self.project,
                str(ready_file),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=holder_environment,
        )
        try:
            self._wait_for_ready_file(holder, ready_file, "공유 관리 잠금")
            contender_environment = os.environ.copy()
            contender_environment["TMPDIR"] = str(second_tmp)
            contested_output = self.temp / "contested-backup"
            result = self._backup_tool(
                "backup",
                self,
                contested_output,
                environment=contender_environment,
                check=False,
            )
            if (
                result.returncode == 0
                or "다른 관리 작업이 실행 중입니다" not in result.stderr
                or contested_output.exists()
                or list(self.temp.glob(".contested-backup.tmp-*"))
            ):
                raise StackError("서로 다른 TMPDIR의 관리 작업이 같은 잠금을 공유하지 않았습니다")
            self.verify_services_running()
        finally:
            self._terminate_process(holder)
            ready_file.unlink(missing_ok=True)

    # 복원 대상 프로젝트 이름으로 미리 컨테이너·볼륨·네트워크를 하나씩 만들어두고, 그 상태에서 복원을
    # 시도했을 때 "정지된 컨테이너 하나만 있어도, 자원 종류가 무엇이든" 기존 것을 지우지 않고 거부하는지
    # 확인한다 — 복원이 실수로 무관한 기존 자원을 밀어버리지 않는다는 안전장치를 자원 종류별로 반복 검증
    def _verify_restore_resource_refusal(
        self, backup: Path, restored: "RuntimeStack"
    ) -> None:
        labelled_name = f"{restored.project}-stopped"
        create = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                labelled_name,
                "--label",
                f"com.docker.compose.project={restored.project}",
                f"{self.image_prefix}-mariadb:local",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        container_id = create.stdout.strip()
        try:
            refused = self._backup_tool(
                "restore", restored, backup, check=False
            )
            inspect = subprocess.run(
                ["docker", "container", "inspect", container_id],
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            if (
                refused.returncode == 0
                or "비어 있지 않습니다" not in refused.stderr
                or inspect.returncode != 0
            ):
                raise StackError("복원이 정지 컨테이너를 보존하며 거부되지 않았습니다")
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                check=False,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )

        collisions = (
            ("container", f"{restored.project}-wordpress-1"),
            ("volume", f"{restored.project}_mariadb_data"),
            ("network", f"{restored.project}_backend"),
        )
        for kind, name in collisions:
            if kind == "container":
                command = [
                    "docker",
                    "container",
                    "create",
                    "--name",
                    name,
                    f"{self.image_prefix}-wordpress:local",
                ]
            else:
                command = ["docker", kind, "create", name]
            subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            try:
                refused = self._backup_tool(
                    "restore", restored, backup, check=False
                )
                inspect = subprocess.run(
                    ["docker", kind, "inspect", name],
                    text=True,
                    capture_output=True,
                    timeout=PROCESS_TIMEOUT_SECONDS,
                )
                if (
                    refused.returncode == 0
                    or "비어 있지 않습니다" not in refused.stderr
                    or inspect.returncode != 0
                ):
                    raise StackError(
                        f"복원이 기존 {kind} 자원을 보존하며 거부되지 않았습니다"
                    )
            finally:
                subprocess.run(
                    ["docker", kind, "rm", name],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=PROCESS_TIMEOUT_SECONDS,
                )

    # [INTV:ARCH] CI가 backup-restore 시나리오로 호출하는 진입점 — 백업/복원 도구를 정상 경로뿐
    # 아니라 "기존 출력 보존", "dangling 심볼릭 링크 거부", "중간 실패 주입 후 정리", "신호로 중단 후
    # 정리", "대용량 파일·대용량 DB 값의 무결성", "이미 자원이 있는 대상에 대한 복원 거부"까지 한
    # 번에 훑는다. 아래에서 32MiB 랜덤 파일과 4MiB짜리 DB 값을 일부러 만들어 체크섬/길이로 비교하는
    # 것은, 작은 텍스트만으로는 안 드러나는 스트리밍·버퍼 처리 버그(예: 큰 데이터가 중간에 잘리는
    # 문제)까지 잡기 위함.
    def verify_backup_restore(self) -> None:
        self.start()
        self._verify_pause_signal_race()
        nonce = secrets.token_hex(8)
        title = f"복원 검증 {nonce}"
        content = f"backup-database-{nonce}"
        filename = f"backup-{nonce}.txt"
        file_value = f"backup-volume-{nonce}\n"
        post_id = self.wordpress(
            "post",
            "create",
            f"--post_title={title}",
            f"--post_content={content}",
            "--post_status=publish",
            "--porcelain",
            capture=True,
        )
        self.wordpress("option", "update", "container_stack_persistence", content)
        self.wordpress(
            "eval",
            "wp_mkdir_p(WP_CONTENT_DIR . '/uploads'); "
            f'file_put_contents(WP_CONTENT_DIR . "/uploads/{filename}", "{file_value}");',
        )
        large_filename = f"backup-large-{nonce}.bin"
        self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "sh",
            "-ceu",
            "umask 077; head -c 33554432 /dev/urandom >\"$1\"",
            "large-backup-fixture",
            f"/var/www/html/wp-content/uploads/{large_filename}",
            capture=True,
        )
        large_hash = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "sha256sum",
            f"/var/www/html/wp-content/uploads/{large_filename}",
            capture=True,
        ).stdout.split()[0]
        table_name = f"container_stack_large_{nonce}"
        self.wordpress(
            "db",
            "query",
            f"CREATE TABLE {table_name} (payload LONGTEXT NOT NULL); "
            f"INSERT INTO {table_name} VALUES (REPEAT('x', 4194304));",
        )
        self._verify_shared_operation_lock()
        existing_backup = self.temp / "existing-backup"
        existing_backup.mkdir(mode=0o700)
        write_private(existing_backup / "sentinel.txt", "preserve")
        existing_snapshot = (existing_backup / "sentinel.txt").read_bytes()
        existing_result = self._backup_tool(
            "backup", self, existing_backup, check=False
        )
        if (
            existing_result.returncode == 0
            or "이미 존재합니다" not in existing_result.stderr
            or (existing_backup / "sentinel.txt").read_bytes() != existing_snapshot
            or set(path.name for path in existing_backup.iterdir()) != {"sentinel.txt"}
        ):
            raise StackError("백업 도구가 기존 출력 디렉터리를 안전하게 보존하지 않았습니다")

        dangling_backup = self.temp / "backup-link"
        missing_target = self.temp / "missing-backup-target"
        dangling_backup.symlink_to(missing_target, target_is_directory=True)
        dangling_result = self._backup_tool(
            "backup", self, dangling_backup, check=False
        )
        if (
            dangling_result.returncode == 0
            or "이미 존재합니다" not in dangling_result.stderr
            or not dangling_backup.is_symlink()
            or missing_target.exists()
        ):
            raise StackError("백업 도구가 dangling symlink 출력 경로를 거부하지 않았습니다")

        failed_backup = self.temp / "failed-backup"
        failed_result = self._backup_tool(
            "backup",
            self,
            failed_backup,
            fail_after="database-dump",
            check=False,
        )
        if (
            failed_result.returncode == 0
            or "실패 주입: database-dump" not in failed_result.stderr
            or failed_backup.exists()
            or list(self.temp.glob(".failed-backup.tmp-*"))
        ):
            raise StackError("실패한 백업이 임시 파일을 남겼거나 서비스를 복구하지 못했습니다")
        self.verify_services_running()

        backup = self.temp / "backup"
        self._interrupt_backup_tool(
            "backup",
            self,
            backup,
            pause_after="backup-stop",
            signum=signal.SIGTERM,
        )
        if (
            backup.exists()
            or list(self.temp.glob(".backup.tmp-*"))
        ):
            raise StackError("SIGTERM으로 중단한 백업이 출력·임시 파일을 정리하거나 서비스를 복구하지 못했습니다")
        self.verify_services_running()
        self._backup_tool("backup", self, backup)
        if (backup.stat().st_mode & 0o077) != 0:
            raise StackError("백업 디렉터리가 소유자 외 사용자에게 열려 있습니다")
        for filename_in_backup in ("database.sql", "wordpress.tar.gz", "manifest.json"):
            if ((backup / filename_in_backup).stat().st_mode & 0o077) != 0:
                raise StackError(f"백업 파일 권한이 안전하지 않습니다: {filename_in_backup}")

        restored = RuntimeStack(
            keep=False,
            diagnostics_dir=self.diagnostics_dir,
            project_record_dir=self.project_record_dir,
            credential_values=dict(self.credential_values),
        )
        restored.started = True
        restored_failed = True
        try:
            self._verify_restore_resource_refusal(backup, restored)
            unsafe_backup = self.temp / "unsafe-backup"
            unsafe_backup.mkdir(mode=0o700)
            for safe_name in ("manifest.json", "wordpress.tar.gz"):
                shutil.copyfile(backup / safe_name, unsafe_backup / safe_name)
                (unsafe_backup / safe_name).chmod(0o600)
            (unsafe_backup / "database.sql").symlink_to(backup / "database.sql")
            unsafe_result = self._backup_tool(
                "restore", restored, unsafe_backup, check=False
            )
            if (
                unsafe_result.returncode == 0
                or "안전하게 열 수 없습니다" not in unsafe_result.stderr
                or restored.project_resources()
            ):
                raise StackError("복원 도구가 백업 내부의 심볼릭 링크를 거부하지 않았습니다")

            failed_restore = self._backup_tool(
                "restore",
                restored,
                backup,
                fail_after="database-restore",
                check=False,
            )
            if (
                failed_restore.returncode == 0
                or "실패 주입: database-restore" not in failed_restore.stderr
                or restored.project_resources()
            ):
                raise StackError("실패한 복원이 프로젝트 자원을 정리하지 못했습니다")

            self._interrupt_backup_tool(
                "restore",
                restored,
                backup,
                pause_after="database-restore",
                signum=signal.SIGINT,
            )
            remaining_resources = restored.project_resources()
            if remaining_resources:
                raise StackError(
                    "SIGINT로 중단한 복원이 프로젝트 자원을 남겼습니다: "
                    f"{remaining_resources}"
                )
            self._backup_tool("restore", restored, backup)
            restored._verify_persistent_values(
                post_id=post_id,
                title=title,
                content=content,
                filename=filename,
                file_value=file_value,
            )
            restored_large_hash = restored.run_compose(
                "exec",
                "--no-TTY",
                "wordpress",
                "sha256sum",
                f"/var/www/html/wp-content/uploads/{large_filename}",
                capture=True,
            ).stdout.split()[0]
            if restored_large_hash != large_hash:
                raise StackError("큰 WordPress 파일의 복원 체크섬이 다릅니다")
            restored_length = restored.wordpress(
                "db",
                "query",
                f"SELECT LENGTH(payload) FROM {table_name};",
                "--skip-column-names",
                capture=True,
            )
            if restored_length != "4194304":
                raise StackError("큰 MariaDB 값이 온전히 복원되지 않았습니다")
            repeated = self._backup_tool("restore", restored, backup, check=False)
            if repeated.returncode == 0 or "비어 있지 않습니다" not in repeated.stderr:
                raise StackError("복원 도구가 사용 중인 프로젝트를 거부하지 않았습니다")
            restored_failed = False
        finally:
            cleanup_failures = restored.close(failed=restored_failed)
            if cleanup_failures:
                detail = "; ".join(cleanup_failures)
                if restored_failed:
                    print(
                        f"복원 검증 정리 중 추가 오류가 발생했습니다: {detail}",
                        file=sys.stderr,
                    )
                else:
                    raise StackError(f"복원 검증 자원을 정리하지 못했습니다: {detail}")
        print("backup path safety, failure cleanup, fresh restore, and refusal passed")

    def _new_secret_set(self, name: str, prefix: str) -> tuple[Path, dict[str, str]]:
        directory = self.temp / name
        directory.mkdir(mode=0o700)
        values = {
            "db_root_password.txt": f"{prefix}-root#-{secrets.token_urlsafe(24)}",
            "db_password.txt": f"{prefix}-db#-{secrets.token_urlsafe(24)}",
            "wp_admin_password.txt": f"{prefix}-admin-{secrets.token_urlsafe(24)}",
            "wp_user_password.txt": f"{prefix}-user-{secrets.token_urlsafe(24)}",
        }
        for filename, value in values.items():
            write_private(directory / filename, value)
        return directory, values

    def _rotation_command(
        self,
        directory: Path,
        *,
        fail_after: str | None = None,
        pause_after: str | None = None,
        pause_ready_file: Path | None = None,
        rollback_ready_file: Path | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            str(ROOT / "tools" / "rotate_secrets.py"),
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--new-secrets-dir",
            str(directory),
        ]
        if fail_after is not None:
            command.extend(("--fail-after", fail_after))
        if pause_after is not None and pause_ready_file is not None:
            command.extend(("--pause-after", pause_after))
            command.extend(("--pause-ready-file", str(pause_ready_file)))
        if rollback_ready_file is not None:
            command.extend(("--rollback-ready-file", str(rollback_ready_file)))
        return command

    def _rotation_tool(
        self, directory: Path, *, fail_after: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = self._rotation_command(directory, fail_after=fail_after)
        try:
            return subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired as error:
            raise StackError("자격증명 회전 도구가 제한 시간 안에 끝나지 않았습니다") from error

    # tools/rotate_secrets.py의 신호 처리 설계(_rotate의 signal_state["deferred"] 참고)를 정확히 겨냥한
    # 이중 신호 시나리오다: 먼저 호스트 비밀 파일까지 다 바꾼 시점(host-files)에서 SIGTERM을 보내
    # 롤백을 시작시키고, 롤백이 "진행 중"이라는 두 번째 준비 파일이 생기는 순간을 노려 SIGINT를 또
    # 보낸다 — 롤백 도중에 또 신호가 와도 롤백을 끊지 않고 끝까지 마친 뒤 "지연 처리했다"고 보고하는지 확인
    def _interrupt_rotation_tool(
        self,
        directory: Path,
    ) -> subprocess.CompletedProcess[str]:
        pause_ready_file = self.temp / "rotation-host-files.ready"
        rollback_ready_file = self.temp / "rotation-rollback.ready"
        command = self._rotation_command(
            directory,
            pause_after="host-files",
            pause_ready_file=pause_ready_file,
            rollback_ready_file=rollback_ready_file,
        )
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self._wait_for_ready_file(
                process,
                pause_ready_file,
                "자격증명 회전 변경 완료",
            )
            process.send_signal(signal.SIGTERM)
            self._wait_for_ready_file(
                process,
                rollback_ready_file,
                "자격증명 회전 롤백 시작",
            )
            process.send_signal(signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=600)
            except subprocess.TimeoutExpired as error:
                stdout, stderr = self._terminate_process(process)
                raise StackError("신호로 중단한 자격증명 회전이 제한 시간 안에 끝나지 않았습니다") from error
        finally:
            if process.poll() is None:
                self._terminate_process(process)
            pause_ready_file.unlink(missing_ok=True)
            rollback_ready_file.unlink(missing_ok=True)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if (
            result.returncode != 1
            or "SIGTERM" not in result.stderr
            or "롤백 완료" not in result.stderr
            or "추가 종료 신호 지연 처리" not in result.stderr
        ):
            raise StackError(
                "종료 신호가 자격증명 회전 롤백을 중단했습니다: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    # 아래 세 _*_works/_matches 메서드는 tools/rotate_secrets.py 안의 동명 검증 로직과 같은 것을 검사하지만,
    # 그 모듈을 import하지 않고 docker compose exec로 매번 새로 실행한다 — 회전 도구 자신의 코드가 아니라
    # "회전이 끝난 뒤 실제 컨테이너 상태"를 기준으로 독립적으로 재확인하기 위함(순환 검증 방지)
    def _sql_password_works(self, kind: str, password: str) -> bool:
        if kind == "root":
            service = "mariadb"
            command = (
                "mariadb --defaults-extra-file=\"$auth\" "
                "--socket=/run/mysqld/mysqld.sock -uroot --execute='SELECT 1'"
            )
        else:
            service = "wordpress"
            command = (
                "mariadb --defaults-extra-file=\"$auth\" -hmariadb "
                "-u\"$MYSQL_USER\" \"$MYSQL_DATABASE\" --execute='SELECT 1'"
            )
        result = self.run_compose(
            "exec",
            "--no-TTY",
            service,
            "sh",
            "-ceu",
            "umask 077; auth=\"$(mktemp /run/container-stack-test.XXXXXX)\"; "
            "trap 'rm -f -- \"$auth\"' EXIT HUP INT TERM; "
            "IFS= read -r password; "
            "printf '[client]\\npassword=\"%s\"\\n' \"$password\" >\"$auth\"; "
            + command,
            input_data=password + "\n",
            capture=True,
            check=False,
        )
        return result.returncode == 0

    def _wordpress_password_works(self, kind: str, password: str) -> bool:
        code = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
require '/var/www/html/wp-load.php';
$login = getenv($payload['kind'] === 'admin' ? 'WORDPRESS_ADMIN_USER' : 'WORDPRESS_USER');
$account = get_user_by('login', $login);
if (!$account) { exit(1); }
clean_user_cache($account->ID);
$account = get_user_by('login', $login);
if (!$account || !wp_check_password($payload['password'], $account->user_pass, $account->ID)) { exit(1); }
"""
        result = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "php",
            "-r",
            code,
            input_data=json.dumps({"kind": kind, "password": password}),
            capture=True,
            check=False,
        )
        return result.returncode == 0

    def _wordpress_config_matches(self, password: str) -> bool:
        code = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
$text = file_get_contents('/var/www/html/wp-config.php');
$pattern = "/define\\(\\s*['\"]DB_PASSWORD['\"]\\s*,\\s*['\"]([^'\"]*)['\"]\\s*\\);/";
if ($text === false || !preg_match($pattern, $text, $matches) || !hash_equals($payload['password'], $matches[1])) { exit(1); }
"""
        result = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "php",
            "-r",
            code,
            input_data=json.dumps({"password": password}),
            capture=True,
            check=False,
        )
        return result.returncode == 0

    # rotate_secrets.py의 atomic_secret_write/root_sql/app_sql이 만드는 임시 파일 이름 패턴
    # (container-stack-root.*, .wp-config.rotate.* 등)이 회전이 끝난 뒤에도 호스트나 컨테이너 안에
    # 남아있지 않은지 확인 — "정리를 깜빡한 임시 파일"이 곧 비밀값이 남아도는 경로가 될 수 있어서 검사한다
    def _assert_no_rotation_temporary_files(self) -> None:
        if list(self.temp.glob(".*.txt.*")):
            raise StackError("호스트에 자격증명 임시 파일이 남았습니다")
        checks = (
            (
                "mariadb",
                "test -z \"$(find /run -maxdepth 1 -type f "
                "\\( -name 'container-stack-root.*' -o -name 'container-stack-test.*' \\) "
                "-print -quit)\"",
            ),
            (
                "wordpress",
                "test -z \"$(find /run -maxdepth 1 -type f "
                "\\( -name 'container-stack-app.*' -o -name 'container-stack-test.*' \\) "
                "-print -quit)\"; "
                "test -z \"$(find /var/www/config -maxdepth 1 -type f "
                "-name '.wp-config.rotate.*' -print -quit)\"",
            ),
        )
        for service, script in checks:
            result = self.run_compose(
                "exec",
                "--no-TTY",
                service,
                "sh",
                "-ceu",
                script,
                capture=True,
                check=False,
            )
            if result.returncode != 0:
                raise StackError(f"{service} 컨테이너에 자격증명 임시 파일이 남았습니다")

    # 회전 시나리오 전체가 반복해서 재사용하는 "지금이 정확히 이 상태여야 한다"는 검증 묶음 —
    # 호스트 비밀 파일 값·권한, DB root/app 비밀번호, wp-config, WordPress 계정 비밀번호가 모두
    # expected와 일치하고 rejected와는 불일치해야 한다(양방향 검증). 실제로 글을 쓰고 HTTPS로 읽어
    # 스택이 여전히 기능하는지까지 확인한 뒤, 임시 파일 잔존 여부와 런타임 경계까지 마무리로 검사한다
    def _assert_rotation_state(
        self,
        expected: dict[str, str],
        rejected: dict[str, str],
    ) -> None:
        for filename, value in expected.items():
            path = self.temp / filename
            if path.read_text(encoding="utf-8").rstrip("\n") != value:
                raise StackError(f"호스트 비밀 파일 값이 예상과 다릅니다: {filename}")
            info = path.stat()
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise StackError(f"호스트 비밀 파일 권한이나 소유자가 다릅니다: {filename}")

        root = expected["db_root_password.txt"]
        app = expected["db_password.txt"]
        if not self._sql_password_works("root", root):
            raise StackError("예상한 MariaDB root 비밀번호가 동작하지 않습니다")
        if self._sql_password_works("root", rejected["db_root_password.txt"]):
            raise StackError("폐기한 MariaDB root 비밀번호가 여전히 동작합니다")
        if not self._sql_password_works("app", app):
            raise StackError("예상한 MariaDB 애플리케이션 비밀번호가 동작하지 않습니다")
        if self._sql_password_works("app", rejected["db_password.txt"]):
            raise StackError("폐기한 MariaDB 애플리케이션 비밀번호가 여전히 동작합니다")
        if not self._wordpress_config_matches(app):
            raise StackError("wp-config.php가 예상한 DB 비밀번호를 사용하지 않습니다")

        for kind, filename in (
            ("admin", "wp_admin_password.txt"),
            ("user", "wp_user_password.txt"),
        ):
            if not self._wordpress_password_works(kind, expected[filename]):
                raise StackError(f"예상한 WordPress {kind} 비밀번호가 동작하지 않습니다")
            if self._wordpress_password_works(kind, rejected[filename]):
                raise StackError(f"폐기한 WordPress {kind} 비밀번호가 여전히 동작합니다")

        if self.fetch("/healthz").strip() != "ok":
            raise StackError("자격증명 상태 검증 뒤 HTTPS 상태 확인이 실패했습니다")
        nonce = secrets.token_hex(8)
        content = f"rotation-state-{nonce}"
        post_id = self.wordpress(
            "post",
            "create",
            f"--post_title=회전 상태 {nonce}",
            f"--post_content={content}",
            "--post_status=publish",
            "--porcelain",
            capture=True,
        )
        if content not in self.fetch(f"/?p={post_id}"):
            raise StackError("자격증명 상태 검증 뒤 WordPress 쓰기·읽기가 실패했습니다")
        self._assert_no_rotation_temporary_files()
        self.assert_runtime_secret_boundary(expected)

    # [INTV:ARCH] [INTV:FLOW] CI가 rotation 시나리오로 호출하는 진입점.
    # - [FLOW] 1. 정상 회전 한 번 -> 2. rotate_secrets.py의 FAILURE_STAGES 각 단계마다 실패를
    #   주입해 그때마다 롤백이 이전 상태로 완전히 되돌리는지 확인 -> 3. 신호로 중단시킨 회전도
    #   롤백되는지 확인 -> 4. 롤백 직후 "같은 입력 파일"로 다시 회전을 시도했을 때 이번엔
    #   성공하는지(재시도 가능성) 확인 -> 5. 이 시나리오 동안 등장했던 모든 비밀값 집합이 Compose
    #   로그 어디에도 남지 않았는지 마지막에 총정리.
    # [INTV:EDGE] 입력 비밀 파일들의 스냅샷(assert_input_unchanged)을 매번 비교하는 것은, 회전
    # 도구가 실패하든 성공하든 "새 비밀값이 적힌 원본 디렉터리 자체"는 절대 건드리지 않아야 한다는
    # 계약을 확인하기 위함.
    def verify_secret_rotation(self) -> None:
        self.start()
        initial_values = dict(self.credential_values)

        def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
            return {
                path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in directory.iterdir()
            }

        def assert_input_unchanged(
            directory: Path, expected: dict[str, tuple[bytes, int]]
        ) -> None:
            if snapshot(directory) != expected:
                raise StackError(f"회전 입력 파일이 변경되었습니다: {directory.name}")

        def assert_no_secret_output(
            result: subprocess.CompletedProcess[str],
            *secret_sets: dict[str, str],
        ) -> None:
            output = result.stdout + result.stderr
            for secret_set in secret_sets:
                for value in secret_set.values():
                    if value in output:
                        raise StackError("회전 도구 출력에 비밀값이 포함되었습니다")

        first_dir, first_values = self._new_secret_set("rotation-first", "first")
        first_snapshot = snapshot(first_dir)
        first = self._rotation_tool(first_dir)
        if first.returncode != 0:
            raise StackError(f"정상 회전이 실패했습니다: {first.stderr}")
        assert_no_secret_output(first, initial_values, first_values)
        assert_input_unchanged(first_dir, first_snapshot)
        self._assert_rotation_state(first_values, initial_values)

        tested_values: list[dict[str, str]] = [initial_values, first_values]
        retry_dir: Path | None = None
        retry_values: dict[str, str] | None = None
        retry_snapshot: dict[str, tuple[bytes, int]] | None = None
        for index, failure_stage in enumerate(
            (
                "admin-user-command",
                "config-command",
                "app-password-command",
                "root-password-command",
                "host-file",
                "recreate-wordpress-removed",
            ),
            start=1,
        ):
            candidate_dir, candidate_values = self._new_secret_set(
                f"rotation-failure-{index}", f"failure-{index}"
            )
            candidate_snapshot = snapshot(candidate_dir)
            injected = self._rotation_tool(
                candidate_dir, fail_after=failure_stage
            )
            if injected.returncode == 0 or "롤백 완료" not in injected.stderr:
                raise StackError(
                    f"{failure_stage} 실패 주입 뒤 롤백 결과를 확인하지 못했습니다: "
                    f"{injected.stderr}"
                )
            assert_no_secret_output(injected, first_values, candidate_values)
            assert_input_unchanged(candidate_dir, candidate_snapshot)
            self._assert_rotation_state(first_values, candidate_values)
            tested_values.append(candidate_values)
            retry_dir = candidate_dir
            retry_values = candidate_values
            retry_snapshot = candidate_snapshot

        signal_dir, signal_values = self._new_secret_set(
            "rotation-signal", "signal"
        )
        signal_snapshot = snapshot(signal_dir)
        interrupted = self._interrupt_rotation_tool(signal_dir)
        assert_no_secret_output(interrupted, first_values, signal_values)
        assert_input_unchanged(signal_dir, signal_snapshot)
        self._assert_rotation_state(first_values, signal_values)
        tested_values.append(signal_values)
        retry_dir = signal_dir
        retry_values = signal_values
        retry_snapshot = signal_snapshot

        if retry_dir is None or retry_values is None or retry_snapshot is None:
            raise StackError("재시도할 회전 입력을 만들지 못했습니다")
        retried = self._rotation_tool(retry_dir)
        if retried.returncode != 0:
            raise StackError(f"롤백 직후 같은 입력으로 재시도하지 못했습니다: {retried.stderr}")
        assert_no_secret_output(retried, first_values, retry_values)
        assert_input_unchanged(retry_dir, retry_snapshot)
        self._assert_rotation_state(retry_values, first_values)

        logs = self.run_compose("logs", "--no-color", capture=True, check=False)
        log_output = logs.stdout + logs.stderr
        for secret_set in tested_values:
            for value in secret_set.values():
                if value in log_output:
                    raise StackError("Compose 로그에 자격증명 값이 포함되었습니다")
        print("secret rotation, ambiguous failures, rollback, and retry passed")

    # [INTV:ARCH] CI가 operations 시나리오로 호출하는, 6개 중 마지막 진입점 — 세 갈래로 나뉜다:
    # (1) docker inspect로 얻은 실제 컨테이너의 자원 제한·로그 정책·보안 옵션·네트워크 소속을
    # docker-compose.yml에 선언된 값(값 자체는 이 함수 안 expected에 다시 못박아 둠)과 하나하나
    # 비교, (2) `make fclean`이 DESTROY_CONFIRM 없이는 절대 실행되지 않는지 확인(Makefile의
    # 안전장치 검증), (3) tools/diagnose_stack.py가 비밀값을 실제로 가리는지, 못 읽는 비밀 파일이
    # 있으면 아예 중단하는지, 기존 결과·심볼릭 링크 출력 경로를 거부하는지를 확인.
    # [INTV:TRAP] expected 딕셔너리에 memory/nano_cpus/pids 등을 docker-compose.yml과 별개로
    # 다시 하드코딩해두는 방식은, compose 파일에서 자원 제한 값을 바꾸면 이 테스트도 반드시 함께
    # 고쳐야 하는 강한 결합을 만든다 — compose 파일 값을 파싱해서 자동으로 비교하는 대신 값을
    # 직접 못박아 둔 이유는, "docker inspect가 compose 파일에 적힌 값을 실제로 그대로 반영했는지"
    # 자체를 검증하려는 것이라 compose 파일을 다시 파싱해 비교 기준으로 삼으면 그 목적 자체가
    # 순환 논리가 되기 때문이다.
    def verify_operations(self) -> None:
        self.start()
        expected = {
            "nginx": {
                "memory": 128 * 1024 * 1024,
                "nano_cpus": 500_000_000,
                "pids": 64,
                "signal": "SIGQUIT",
                "timeout": 15,
                "networks": {f"{self.project}_frontend"},
            },
            "wordpress": {
                "memory": 512 * 1024 * 1024,
                "nano_cpus": 1_000_000_000,
                "pids": 256,
                "signal": "SIGQUIT",
                "timeout": 30,
                "networks": {
                    f"{self.project}_frontend",
                    f"{self.project}_backend",
                },
            },
            "mariadb": {
                "memory": 512 * 1024 * 1024,
                "nano_cpus": 1_000_000_000,
                "pids": 256,
                "signal": "SIGTERM",
                "timeout": 60,
                "networks": {f"{self.project}_backend"},
            },
        }
        container_ids: dict[str, str] = {}
        for service, policy in expected.items():
            inspected = self.inspect_service(service)
            container_ids[service] = str(inspected["Id"])
            host = inspected["HostConfig"]
            config = inspected["Config"]
            actual = {
                "memory": host["Memory"],
                "nano_cpus": host["NanoCpus"],
                "pids": host["PidsLimit"],
                "signal": config["StopSignal"],
                "timeout": config["StopTimeout"],
                "networks": set(inspected["NetworkSettings"]["Networks"]),
            }
            if actual != policy:
                raise StackError(
                    f"{service} 실행 정책이 Compose 설정과 다릅니다: {actual!r}"
                )
            log_config = host["LogConfig"]
            if log_config["Type"] != "json-file" or log_config["Config"] != {
                "max-file": "3",
                "max-size": "10m",
            }:
                raise StackError(f"{service} 로그 회전 정책이 적용되지 않았습니다")
            if "no-new-privileges:true" not in (host["SecurityOpt"] or []):
                raise StackError(f"{service} 권한 상승 차단 정책이 적용되지 않았습니다")
            expected_nofile = {
                "nginx": (1024, 4096),
                "wordpress": (1024, 4096),
                "mariadb": (4096, 65536),
            }[service]
            nofile = next(
                (item for item in host["Ulimits"] if item["Name"] == "nofile"), None
            )
            if nofile is None or (
                nofile["Soft"], nofile["Hard"]
            ) != expected_nofile:
                raise StackError(f"{service} 파일 디스크립터 제한이 적용되지 않았습니다")

        network_policies = {
            "frontend": (False, {container_ids["nginx"], container_ids["wordpress"]}),
            "backend": (True, {container_ids["wordpress"], container_ids["mariadb"]}),
        }
        for name, (expected_internal, expected_members) in network_policies.items():
            network = subprocess.run(
                ["docker", "network", "inspect", f"{self.project}_{name}"],
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            inspected_network = json.loads(network.stdout)[0]
            actual_members = set((inspected_network.get("Containers") or {}).keys())
            if inspected_network.get("Internal") is not expected_internal:
                raise StackError(f"{name} 네트워크의 내부망 정책이 예상과 다릅니다")
            if actual_members != expected_members:
                raise StackError(f"{name} 네트워크의 연결 서비스가 예상과 다릅니다")

        # DESTROY_CONFIRM을 일부러 넘기지 않고 fclean을 실행 — Makefile의 안전장치(fclean 타겟 참고)가
        # 실수로 볼륨·이미지를 지우는 것을 막아주는지, 그리고 그 시도 자체가 이미 떠 있는 스택에
        # 아무 영향도 주지 않는지(바로 아래 healthz 재확인)를 함께 검증
        refused = subprocess.run(
            [
                "make",
                "--silent",
                "fclean",
                f"PROJECT_NAME={self.project}",
                f"ENV_FILE={self.env_file}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if refused.returncode != 2 or "DESTROY_CONFIRM" not in refused.stderr:
            raise StackError("fclean이 명시적인 프로젝트 이름 확인 없이 실행될 수 있습니다")
        if self.fetch("/healthz").strip() != "ok":
            raise StackError("삭제 거부 뒤 실행 중인 스택이 손상되었습니다")

        # 비밀값을 쿼리 문자열로 흘려보내 nginx 접근 로그 등에 실제로 새어 들어가게 만든 뒤,
        # 진단 도구가 그런 로그까지 포함해서 값을 가려내는지(가려내지 못하면 아래 combined 검사에서 걸림)
        # 확인하기 위한 사전 준비
        log_secret = self.credential_values["wp_user_password.txt"]
        self.fetch(f"/?diagnostic_token={log_secret}")
        # 비밀 파일 하나를 일부러 못 읽게(권한 0) 만들어, 진단 도구가 "가릴 값 자체를 못 읽으면"
        # 부분적으로라도 진행하는 대신 아예 아무 결과도 남기지 않고 중단하는지 확인
        unreadable_secret = self.temp / "wp_user_password.txt"
        unreadable_output = self.temp / "unreadable-secret-diagnostics"
        unreadable_command = [
            sys.executable,
            str(ROOT / "tools" / "diagnose_stack.py"),
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--output",
            str(unreadable_output),
        ]
        unreadable_secret.chmod(0)
        try:
            refused_unredacted = subprocess.run(
                unreadable_command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
        finally:
            unreadable_secret.chmod(0o600)
        if (
            refused_unredacted.returncode != 2
            or "가릴 비밀값을 읽을 수 없습니다" not in refused_unredacted.stderr
            or unreadable_output.exists()
        ):
            raise StackError("진단 도구가 읽지 못한 비밀값을 제외한 채 계속 실행했습니다")

        diagnostics = self.temp / "operations-diagnostics"
        diagnostic_command = [
            sys.executable,
            str(ROOT / "tools" / "diagnose_stack.py"),
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--output",
            str(diagnostics),
        ]
        subprocess.run(
            diagnostic_command,
            cwd=ROOT,
            check=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        expected_files = {
            "versions.txt",
            "compose-ps.txt",
            "compose-logs.txt",
            "compose-model.txt",
            "container-state.txt",
        }
        if {path.name for path in diagnostics.iterdir()} != expected_files:
            raise StackError("진단 자료 파일 구성이 예상과 다릅니다")
        if stat.S_IMODE(diagnostics.stat().st_mode) != 0o700:
            raise StackError("진단 디렉터리 권한이 0700이 아닙니다")
        combined = ""
        for path in diagnostics.iterdir():
            if not path.is_file() or path.is_symlink():
                raise StackError(f"진단 결과에 일반 파일이 아닌 항목이 있습니다: {path}")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise StackError(f"진단 파일 권한이 0600이 아닙니다: {path}")
            combined += path.read_text(encoding="utf-8")
        leaked = [
            value for value in self.credential_values.values() if value in combined
        ]
        if leaked:
            raise StackError("진단 자료에 비밀값이 남아 있습니다")
        if "<redacted>" not in combined:
            raise StackError("진단 자료의 실제 비밀값 제거를 확인하지 못했습니다")
        for filename in self.credential_values:
            if str(self.temp / filename) in combined:
                raise StackError("진단 자료에 비밀 파일 경로가 남아 있습니다")
        original = {
            path.name: path.read_bytes() for path in diagnostics.iterdir()
        }
        repeated = subprocess.run(
            diagnostic_command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if repeated.returncode != 2 or "이미 존재합니다" not in repeated.stderr:
            raise StackError("진단 도구가 기존 출력 경로 덮어쓰기를 거부하지 않았습니다")
        if original != {
            path.name: path.read_bytes() for path in diagnostics.iterdir()
        }:
            raise StackError("진단 도구의 덮어쓰기 거부 뒤 기존 결과가 변경되었습니다")

        dangling_target = self.temp / "missing-diagnostics-target"
        symlink_output = self.temp / "operations-diagnostics-link"
        symlink_output.symlink_to(dangling_target)
        symlink_command = [*diagnostic_command[:-1], str(symlink_output)]
        refused_symlink = subprocess.run(
            symlink_command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if refused_symlink.returncode != 2 or "이미 존재합니다" not in refused_symlink.stderr:
            raise StackError("진단 도구가 dangling symlink 출력 경로를 거부하지 않았습니다")
        if not symlink_output.is_symlink() or dangling_target.exists():
            raise StackError("진단 도구의 symlink 거부 과정에서 출력 경로가 변경되었습니다")
        print("runtime limits, network isolation, and private diagnostics passed")

    def collect_diagnostics(self) -> Path:
        if self.diagnostics_dir is None:
            destination = Path(tempfile.gettempdir()) / (
                f"container-stack-diagnostics-{self.project}-{secrets.token_hex(3)}"
            )
        else:
            destination = self.diagnostics_dir / self.project
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "diagnose_stack.py"),
                "--project",
                self.project,
                "--env-file",
                str(self.env_file),
                "--output",
                str(destination),
            ],
            cwd=ROOT,
            check=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        print(f"진단 자료: {destination}", file=sys.stderr)
        return destination

    def _remove_test_images(self) -> None:
        failures: list[str] = []
        for service in ("nginx", "wordpress", "mariadb"):
            image = f"{self.image_prefix}-{service}:local"
            result = subprocess.run(
                ["docker", "image", "rm", image],
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            if result.returncode != 0 and "No such image" not in result.stderr:
                failures.append(
                    f"{image}: {result.stderr.strip() or result.stdout.strip()}"
                )
        if failures:
            raise StackError(
                "검사용 이미지 태그를 정리하지 못했습니다: " + "; ".join(failures)
            )

    # 회전 도구의 rollback_rotation()과 같은 "최선 노력" 정리 방식 — 각 정리 단계를 개별 try/except로
    # 감싸 하나가 실패해도 나머지 정리는 계속 시도하고, 실패 내역을 모아 반환한다. --keep 플래그가
    # 켜져 있으면 정리를 아예 건너뛰고 프로젝트를 그대로 남겨, 실패 원인을 사람이 직접 컨테이너에
    # 들어가 조사할 수 있게 한다(디버깅 편의)
    def close(self, *, failed: bool) -> list[str]:
        failures: list[str] = []
        if failed:
            try:
                self.collect_diagnostics()
            except (OSError, StackError, subprocess.SubprocessError) as error:
                failures.append(f"진단 자료 저장: {error}")
        if self.keep:
            print(
                f"검사용 프로젝트를 유지합니다: {self.project} ({self.temp})",
                file=sys.stderr,
            )
            return failures

        try:
            if self.started:
                result = self.run_compose(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "20",
                    capture=True,
                    check=False,
                )
                if result.returncode != 0:
                    failures.append(
                        "Compose 자원 정리: "
                        + (result.stderr.strip() or result.stdout.strip())
                    )
        except (OSError, StackError, subprocess.SubprocessError) as error:
            failures.append(f"Compose 자원 정리: {error}")

        try:
            if self.owns_images:
                self._remove_test_images()
        except (OSError, StackError, subprocess.SubprocessError) as error:
            failures.append(f"이미지 태그 정리: {error}")

        try:
            shutil.rmtree(self.temp)
        except OSError as error:
            failures.append(f"임시 비밀 파일 정리: {error}")
        return failures


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="격리된 컨테이너 스택 검증")
    parser.add_argument(
        "scenario",
        choices=(
            "bootstrap",
            "e2e",
            "persistence",
            "backup-restore",
            "rotation",
            "operations",
        ),
    )
    parser.add_argument("--keep", action="store_true", help="검사 뒤 프로젝트를 유지합니다")
    parser.add_argument("--diagnostics-dir", type=Path)
    parser.add_argument("--project-record-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        require_command("docker")
        require_command("curl")
        require_command("make")
        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        stack = RuntimeStack(
            keep=args.keep,
            diagnostics_dir=args.diagnostics_dir,
            project_record_dir=args.project_record_dir,
        )
    except (OSError, StackError, subprocess.SubprocessError) as error:
        print(f"검증 환경을 준비하지 못했습니다: {error}", file=sys.stderr)
        return 2

    # [INTV:TRAP] failed는 "정상적으로 끝까지 성공했는가"의 반대 표시로 시작해(True), try 블록이
    # 예외 없이 끝까지 돌면 그제서야 False로 내려간다 — finally의 close(failed=failed)가 이 값으로
    # "실패했을 때만 진단 자료를 남긴다"는 위 close()의 분기를 결정한다. 재구현 시 이 변수를
    # False로 시작하면, try 블록 도중 예외가 나서 except로 빠지고 failed=False를 대입하는 코드
    # 라인 자체를 못 지나가는 경우에도 여전히 "실패 아님" 상태로 남아 진단 자료가 하나도 안
    # 남는(정작 필요할 때 비어있는) 흔한 실수가 된다.
    failed = True
    result = 0
    try:
        if args.scenario == "bootstrap":
            stack.verify_bootstrap_recovery()
        elif args.scenario == "e2e":
            stack.verify_e2e()
        elif args.scenario == "persistence":
            stack.verify_persistence()
        elif args.scenario == "backup-restore":
            stack.verify_backup_restore()
        elif args.scenario == "rotation":
            stack.verify_secret_rotation()
        else:
            stack.verify_operations()
        failed = False
    except (OSError, StackError, subprocess.SubprocessError) as error:
        print(f"{args.scenario} 검증 실패: {error}", file=sys.stderr)
        result = 1
    finally:
        cleanup_failures = stack.close(failed=failed)
        if cleanup_failures:
            print(
                "검증 정리 중 오류가 발생했습니다: " + "; ".join(cleanup_failures),
                file=sys.stderr,
            )
            if result == 0:
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
