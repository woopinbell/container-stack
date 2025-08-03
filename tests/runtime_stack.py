#!/usr/bin/env python3
"""격리된 Compose 프로젝트에서 컨테이너 스택의 실제 동작을 검사합니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
CONTROL_TIMEOUT_SECONDS = 330
BUILD_TIMEOUT_SECONDS = 1200
PROCESS_TIMEOUT_SECONDS = 60
PORT_RETRY_LIMIT = 3
PORT_CONFLICT_MARKERS = (
    "address already in use",
    "bind for 127.0.0.1",
    "port is already allocated",
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
    def __init__(self, *, keep: bool, diagnostics_dir: Path | None) -> None:
        self.keep = keep
        self.diagnostics_dir = diagnostics_dir
        self.temp = Path(tempfile.mkdtemp(prefix="container-stack-e2e-"))
        self.temp.chmod(0o700)
        self.project = f"container-stack-{os.getpid()}-{secrets.token_hex(3)}"
        self.domain = "stack.test"
        self.port = reserve_port()
        self.env_file = self.temp / ".env"
        self.started = False
        self.credential_values = {
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
    parser.add_argument("scenario", choices=("bootstrap",))
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
        stack.verify_bootstrap()
        failed = False
        return 0
    except (OSError, StackError, subprocess.SubprocessError) as error:
        print(f"{args.scenario} 검증 실패: {error}", file=sys.stderr)
        return 1
    finally:
        stack.close(failed=failed)


if __name__ == "__main__":
    raise SystemExit(main())
