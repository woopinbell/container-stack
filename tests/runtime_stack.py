#!/usr/bin/env python3
"""격리된 Compose 프로젝트에서 컨테이너 스택의 실제 동작을 검사합니다."""

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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.write("\n")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise StackError(f"비밀 파일 권한이 0600이 아닙니다: {path}")


def replace_private(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    write_private(temporary, value.rstrip("\n"))
    os.replace(temporary, path)


class RuntimeStack:
    def __init__(
        self,
        *,
        keep: bool,
        diagnostics_dir: Path | None,
        credential_values: dict[str, str] | None = None,
    ) -> None:
        self.keep = keep
        self.diagnostics_dir = diagnostics_dir
        self.temp = Path(tempfile.mkdtemp(prefix="container-stack-e2e-"))
        self.temp.chmod(0o700)
        self.project = f"container-stack-{os.getpid()}-{secrets.token_hex(3)}"
        self.domain = "stack.test"
        self.port = reserve_port()
        self.env_file = self.temp / ".env"
        self.started = False
        self.credential_values = credential_values or {
            "db_root_password.txt": f"root#-{secrets.token_urlsafe(24)}",
            "db_password.txt": f"db#-{secrets.token_urlsafe(24)}",
            "wp_admin_password.txt": f"admin-{secrets.token_urlsafe(24)}",
            "wp_user_password.txt": f"user-{secrets.token_urlsafe(24)}",
        }
        try:
            self._prepare_environment()
        except Exception:
            shutil.rmtree(self.temp, ignore_errors=True)
            raise

    def _prepare_environment(self) -> None:
        for filename, value in self.credential_values.items():
            write_private(self.temp / filename, value)

        self.environment_values = {
            "DOMAIN_NAME": self.domain,
            "WORDPRESS_URL": f"https://{self.domain}:{self.port}",
            "HTTPS_BIND_ADDRESS": "127.0.0.1",
            "HTTPS_PORT": str(self.port),
            "STACK_IMAGE_PREFIX": f"{self.project}-image",
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
        capture: bool = False,
        check: bool = True,
        timeout: int = CONTROL_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.compose_command(*arguments),
                cwd=ROOT,
                check=check,
                text=True,
                capture_output=capture,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise StackError(
                f"Compose 명령이 {timeout}초 안에 끝나지 않았습니다"
            ) from error

    def _run_start(
        self,
        action: str,
        *,
        build: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
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
        try:
            result = subprocess.run(
                command,
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

    def assert_runtime_secret_boundary(self) -> None:
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
            if any(path.startswith("/run/secrets") for path in destinations):
                raise StackError(f"{service} 런타임에 비밀 파일이 마운트되었습니다")
            if service == "wordpress" and "/var/www/config" not in destinations:
                raise StackError("WordPress 설정 전용 볼륨이 마운트되지 않았습니다")
            if service == "nginx":
                if "/var/www/config" in destinations:
                    raise StackError("nginx가 WordPress 설정 전용 볼륨을 볼 수 있습니다")
                hidden = self.run_compose(
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
                if hidden.returncode != 0:
                    raise StackError("nginx에서 WordPress 설정 파일이 격리되지 않았습니다")
            config = inspected.get("Config")
            if not isinstance(config, dict):
                raise StackError(f"{service} 실행 환경을 읽지 못했습니다")
            environment = config.get("Env") or []
            config_text = "\n".join(str(item) for item in environment)
            if any(name in config_text for name in forbidden_names):
                raise StackError(f"{service} 런타임 환경에 비밀번호 변수가 남았습니다")
            observed += config_text
            container_id = str(inspected.get("Id", ""))
            observed += subprocess.run(
                ["docker", "top", container_id, "-eo", "pid,args"],
                check=True,
                text=True,
                capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            ).stdout
        for value in self.credential_values.values():
            if value in observed:
                raise StackError("런타임 환경이나 프로세스 인자에 비밀값이 남았습니다")
        config = self.run_compose(
            "exec",
            "--no-TTY",
            "wordpress",
            "cat",
            "/var/www/config/wp-config.php",
            capture=True,
        ).stdout
        if self.credential_values["db_password.txt"] not in config:
            raise StackError("wp-config.php에 DB 자격증명이 없습니다")
        for filename in (
            "db_root_password.txt",
            "wp_admin_password.txt",
            "wp_user_password.txt",
        ):
            if self.credential_values[filename] in config:
                raise StackError(f"wp-config.php에 불필요한 비밀값이 남았습니다: {filename}")

    def fetch(self, path: str) -> str:
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

    def verify_bootstrap(self) -> None:
        self.start()
        self.assert_runtime_secret_boundary()
        for service, marker in (
            ("mariadb", "/var/lib/mysql-volume/data/.container-stack-initialized"),
            ("wordpress", "/var/www/html/.container-stack-initialized"),
        ):
            result = self.run_compose(
                "exec",
                "--no-TTY",
                service,
                "test",
                "-f",
                marker,
                capture=True,
                check=False,
            )
            if result.returncode != 0:
                raise StackError(f"{service} 초기화 완료 표식이 없습니다")
        print("bootstrap completion and secret boundary passed")

    def verify_e2e(self) -> None:
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

        self.run_compose("restart", "mariadb", "wordpress", "nginx")
        self.run_compose("up", "--detach", "--wait", "--wait-timeout", "240")
        self._verify_persistent_values(
            post_id=post_id,
            title=title,
            content=content,
            filename=filename,
            file_value=file_value,
        )

        self.run_compose("down", "--remove-orphans", "--timeout", "20")
        self.run_compose("up", "--detach", "--wait", "--wait-timeout", "240")
        if self.project_volumes() != initial_volumes:
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

    def _interrupt_backup_tool(
        self,
        operation: str,
        project: "RuntimeStack",
        path: Path,
        *,
        pause_after: str,
        signum: signal.Signals,
    ) -> subprocess.CompletedProcess[str]:
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

    def verify_backup_restore(self) -> None:
        self.start()
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
            credential_values=dict(self.credential_values),
        )
        restored.started = True
        restored_failed = True
        try:
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
            repeated = self._backup_tool("restore", restored, backup, check=False)
            if repeated.returncode == 0 or "비어 있지 않습니다" not in repeated.stderr:
                raise StackError("복원 도구가 사용 중인 프로젝트를 거부하지 않았습니다")
            restored_failed = False
        finally:
            restored.close(failed=restored_failed)
        print("backup path safety, failure cleanup, fresh restore, and refusal passed")

    def collect_diagnostics(self) -> Path:
        destination = self.diagnostics_dir
        if destination is None:
            destination = Path(tempfile.mkdtemp(prefix="container-stack-diagnostics-"))
            destination.chmod(0o700)
        else:
            destination.mkdir(parents=True, exist_ok=True)
        commands = {
            "compose-ps.txt": ("ps", "--all"),
            "compose-logs.txt": ("logs", "--no-color", "--timestamps"),
            "compose-config.txt": ("config", "--no-interpolate"),
        }
        for filename, arguments in commands.items():
            result = self.run_compose(*arguments, capture=True, check=False)
            (destination / filename).write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
        print(f"진단 자료: {destination}", file=sys.stderr)
        return destination

    def close(self, *, failed: bool) -> None:
        if failed:
            try:
                self.collect_diagnostics()
            except OSError as error:
                print(f"진단 자료를 저장하지 못했습니다: {error}", file=sys.stderr)
        if self.started and not self.keep:
            self.run_compose(
                "down", "--volumes", "--remove-orphans", "--timeout", "20", check=False
            )
        if self.keep:
            print(
                f"검사용 프로젝트를 유지합니다: {self.project} ({self.temp})",
                file=sys.stderr,
            )
        else:
            shutil.rmtree(self.temp, ignore_errors=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="격리된 컨테이너 스택 검증")
    parser.add_argument(
        "scenario",
        choices=("bootstrap", "e2e", "persistence", "backup-restore"),
    )
    parser.add_argument("--keep", action="store_true", help="검사 뒤 프로젝트를 유지합니다")
    parser.add_argument("--diagnostics-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        require_command("docker")
        require_command("curl")
        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        stack = RuntimeStack(keep=args.keep, diagnostics_dir=args.diagnostics_dir)
    except (OSError, StackError, subprocess.SubprocessError) as error:
        print(f"검증 환경을 준비하지 못했습니다: {error}", file=sys.stderr)
        return 2

    failed = True
    try:
        if args.scenario == "bootstrap":
            stack.verify_bootstrap()
        elif args.scenario == "persistence":
            stack.verify_persistence()
        elif args.scenario == "e2e":
            stack.verify_e2e()
        else:
            stack.verify_backup_restore()
        failed = False
        return 0
    except (OSError, StackError, subprocess.SubprocessError) as error:
        print(f"{args.scenario} 검증 실패: {error}", file=sys.stderr)
        return 1
    finally:
        stack.close(failed=failed)


if __name__ == "__main__":
    raise SystemExit(main())
