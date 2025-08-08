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


PHP_CONFIG = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
$path = '/var/www/config/wp-config.php';
$text = file_get_contents($path);
if ($text === false) { fwrite(STDERR, "wp-config read failed\n"); exit(1); }
if (is_link($path) || !is_file($path)) { fwrite(STDERR, "wp-config is not a regular file\n"); exit(1); }
$replacement = "define( 'DB_PASSWORD', " . var_export($payload['db_password'], true) . " );";
$updated = preg_replace("/define\\(\\s*['\"]DB_PASSWORD['\"]\\s*,\\s*.*?\\);/", $replacement, $text, 1, $count);
if ($updated === null || $count !== 1) { fwrite(STDERR, "DB_PASSWORD definition not found\n"); exit(1); }
umask(0077);
$temporary = tempnam(dirname($path), '.wp-config.rotate.');
if ($temporary === false) { fwrite(STDERR, "temporary file creation failed\n"); exit(1); }
if (realpath(dirname($temporary)) !== realpath(dirname($path))) {
    @unlink($temporary); fwrite(STDERR, "temporary file is on another filesystem\n"); exit(1);
}
$published = false;
try {
    $written = file_put_contents($temporary, $updated, LOCK_EX);
    if ($written !== strlen($updated)) { throw new RuntimeException('wp-config write failed'); }
    if (!chmod($temporary, fileperms($path) & 0777)) { throw new RuntimeException('chmod failed'); }
    if (!chown($temporary, fileowner($path))) { throw new RuntimeException('chown failed'); }
    if (!chgrp($temporary, filegroup($path))) { throw new RuntimeException('chgrp failed'); }
    $handle = fopen($temporary, 'rb');
    if ($handle === false) { throw new RuntimeException('temporary file reopen failed'); }
    try {
        if (function_exists('fsync') && !fsync($handle)) { throw new RuntimeException('fsync failed'); }
    } finally {
        fclose($handle);
    }
    if (!rename($temporary, $path)) { throw new RuntimeException('wp-config publish failed'); }
    $published = true;
} finally {
    if (!$published) { @unlink($temporary); }
}
if (!empty($payload['fail_after_write'])) { fwrite(STDERR, "injected post-write failure\n"); exit(9); }
"""


PHP_USER = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
require '/var/www/html/wp-load.php';
$kind = $payload['kind'];
if ($kind !== 'admin' && $kind !== 'user') { fwrite(STDERR, "invalid user kind\n"); exit(1); }
$login = getenv($kind === 'admin' ? 'WORDPRESS_ADMIN_USER' : 'WORDPRESS_USER');
$account = get_user_by('login', $login);
if (!$account) { fwrite(STDERR, "WordPress user not found\n"); exit(1); }
wp_set_password($payload['password'], $account->ID);
if (!empty($payload['fail_after_write'])) { fwrite(STDERR, "injected post-write failure\n"); exit(9); }
"""


PHP_PROBE_USER = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
require '/var/www/html/wp-load.php';
$kind = $payload['kind'];
$login = getenv($kind === 'admin' ? 'WORDPRESS_ADMIN_USER' : 'WORDPRESS_USER');
clean_user_cache(get_user_by('login', $login)->ID);
$account = get_user_by('login', $login);
if (!$account || !wp_check_password($payload['password'], $account->user_pass, $account->ID)) { exit(1); }
"""


PHP_PROBE_CONFIG = r"""
$payload = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
$text = file_get_contents('/var/www/config/wp-config.php');
if ($text === false) { exit(1); }
$pattern = "/define\\(\\s*['\"]DB_PASSWORD['\"]\\s*,\\s*['\"]([^'\"]*)['\"]\\s*\\);/";
if (!preg_match($pattern, $text, $matches) || !hash_equals($payload['password'], $matches[1])) { exit(1); }
"""


def wordpress_php(
    project: ComposeProject,
    code: str,
    payload: dict[str, object],
    *,
    check: bool = True,
    one_off: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    if one_off:
        arguments = (
            "run",
            "--rm",
            "--no-TTY",
            "--no-deps",
            "--entrypoint",
            "php",
            "wordpress",
            "-r",
            code,
        )
    else:
        arguments = ("exec", "--no-TTY", "wordpress", "php", "-r", code)
    return project.run(
        *arguments,
        input_data=json.dumps(payload).encode(),
        capture=not check,
        check=check,
    )


def set_wordpress_user(
    project: ComposeProject,
    kind: str,
    password: str,
    *,
    fail_after_write: bool = False,
    one_off: bool = False,
) -> None:
    wordpress_php(
        project,
        PHP_USER,
        {
            "kind": kind,
            "password": password,
            "fail_after_write": fail_after_write,
        },
        one_off=one_off,
    )


def set_wordpress_db_config(
    project: ComposeProject,
    password: str,
    *,
    fail_after_write: bool = False,
    one_off: bool = False,
) -> None:
    wordpress_php(
        project,
        PHP_CONFIG,
        {"db_password": password, "fail_after_write": fail_after_write},
        one_off=one_off,
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


def app_sql(
    project: ComposeProject,
    database_user: str,
    password: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    payload = password.encode() + b"\n"
    return project.run(
        "exec",
        "--no-TTY",
        "wordpress",
        "sh",
        "-ceu",
        "umask 077; auth=\"$(mktemp /run/container-stack-app.XXXXXX)\"; "
        "trap 'rm -f -- \"$auth\"' EXIT HUP INT TERM; "
        "IFS= read -r password; "
        "printf '[client]\\npassword=\"%s\"\\n' \"$password\" >\"$auth\"; "
        "mariadb --defaults-extra-file=\"$auth\" -hmariadb "
        f"-u{database_user} \"$MYSQL_DATABASE\" --execute='SELECT 1'",
        input_data=payload,
        capture=True,
        check=check,
    )


def wordpress_password_matches(
    project: ComposeProject,
    kind: str,
    password: str,
) -> bool:
    return (
        wordpress_php(
            project,
            PHP_PROBE_USER,
            {"kind": kind, "password": password},
            check=False,
        ).returncode
        == 0
    )


def wordpress_config_matches(project: ComposeProject, password: str) -> bool:
    return (
        wordpress_php(
            project,
            PHP_PROBE_CONFIG,
            {"password": password},
            check=False,
        ).returncode
        == 0
    )


def verify_runtime_secret_boundary(
    project: ComposeProject, secrets: dict[str, str]
) -> None:
    forbidden_names = (
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_PASSWORD",
        "WORDPRESS_DB_PASSWORD",
        "WORDPRESS_ADMIN_PASSWORD",
        "WORDPRESS_USER_PASSWORD",
    )
    observed = ""
    for service in ("mariadb", "wordpress", "nginx"):
        container_id = project.run(
            "ps", "--quiet", service, capture=True, timeout=QUERY_TIMEOUT_SECONDS
        ).stdout.decode().strip()
        if not container_id or "\n" in container_id:
            raise RotationError(f"{service} 컨테이너를 하나로 식별하지 못했습니다")
        inspected_result = subprocess.run(
            ["docker", "inspect", container_id],
            check=True,
            text=True,
            capture_output=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        inspected = json.loads(inspected_result.stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise RotationError(f"{service} 컨테이너 정보를 읽지 못했습니다")
        container = inspected[0]
        mounts = container.get("Mounts") or []
        if any(
            isinstance(mount, dict)
            and (
                str(mount.get("Destination", "")) == "/run/secrets"
                or str(mount.get("Destination", "")).startswith("/run/secrets/")
            )
            for mount in mounts
        ):
            raise RotationError(f"{service} 런타임에 비밀 파일이 마운트되었습니다")
        destinations = {
            str(mount.get("Destination", ""))
            for mount in mounts
            if isinstance(mount, dict)
        }
        if service == "wordpress" and "/var/www/config" not in destinations:
            raise RotationError("WordPress 설정 전용 볼륨이 마운트되지 않았습니다")
        if service == "nginx":
            if "/var/www/config" in destinations:
                raise RotationError("nginx가 WordPress 설정 전용 볼륨을 볼 수 있습니다")
            hidden = project.run(
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
                timeout=QUERY_TIMEOUT_SECONDS,
            )
            if hidden.returncode != 0:
                raise RotationError(
                    "nginx에서 WordPress DB 설정 파일이 격리되지 않았습니다"
                )
        config = container.get("Config") or {}
        environment = config.get("Env") or []
        environment_text = "\n".join(str(value) for value in environment)
        if any(name in environment_text for name in forbidden_names):
            raise RotationError(f"{service} 런타임 환경에 비밀번호 변수가 남았습니다")
        observed += environment_text
        process_environment = project.run(
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
            timeout=QUERY_TIMEOUT_SECONDS,
        ).stdout.decode(errors="replace")
        if any(name in process_environment for name in forbidden_names):
            raise RotationError(f"{service} 프로세스 환경에 비밀번호 변수가 남았습니다")
        observed += process_environment
        observed += subprocess.run(
            ["docker", "top", container_id, "-eo", "pid,args"],
            check=True,
            text=True,
            capture_output=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        ).stdout
    for value in secrets.values():
        if value and value in observed:
            raise RotationError("런타임 환경이나 프로세스 인자에 비밀값이 남았습니다")


def verify_rotation(
    project: ComposeProject,
    database_user: str,
    secrets: dict[str, str],
    rejected: dict[str, str] | None = None,
) -> None:
    boundary_values = dict(secrets)
    if rejected is not None:
        boundary_values.update(
            {f"rejected_{name}": value for name, value in rejected.items()}
        )
    verify_runtime_secret_boundary(project, boundary_values)
    root_sql(project, secrets["db_root_password"], "SELECT 1;")
    app_sql(project, database_user, secrets["db_password"])
    if not wordpress_config_matches(project, secrets["db_password"]):
        raise RotationError("wp-config.php의 DB 비밀번호가 예상 상태와 다릅니다")
    for kind, secret_name in (
        ("admin", "wp_admin_password"),
        ("user", "wp_user_password"),
    ):
        if not wordpress_password_matches(project, kind, secrets[secret_name]):
            raise RotationError(f"WordPress {kind} 비밀번호가 예상 상태와 다릅니다")
    if rejected is not None:
        if root_sql(
            project, rejected["db_root_password"], "SELECT 1;", check=False
        ).returncode == 0:
            raise RotationError("거부되어야 할 DB root 비밀번호가 동작합니다")
        if app_sql(
            project,
            database_user,
            rejected["db_password"],
            check=False,
        ).returncode == 0:
            raise RotationError("거부되어야 할 DB 애플리케이션 비밀번호가 동작합니다")
        for kind, secret_name in (
            ("admin", "wp_admin_password"),
            ("user", "wp_user_password"),
        ):
            if wordpress_password_matches(project, kind, rejected[secret_name]):
                raise RotationError(f"거부되어야 할 WordPress {kind} 비밀번호가 동작합니다")


def find_root_password(
    project: ComposeProject,
    candidates: tuple[str, ...],
) -> str | None:
    attempted: set[str] = set()
    for candidate in candidates:
        if candidate in attempted:
            continue
        attempted.add(candidate)
        if root_sql(project, candidate, "SELECT 1;", check=False).returncode == 0:
            return candidate
    return None


def rollback_rotation(
    project: ComposeProject,
    paths: dict[str, Path],
    current: dict[str, str],
    replacement: dict[str, str],
    database_user: str,
) -> tuple[list[str], bool]:
    errors: list[str] = []
    result = project.run(
        "up",
        "--detach",
        "--no-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "mariadb",
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        errors.append(f"서비스 준비: {detail or 'docker compose up 실패'}")

    root_auth = find_root_password(
        project,
        (replacement["db_root_password"], current["db_root_password"]),
    )
    if root_auth is None:
        errors.append("DB 계정: 사용할 수 있는 root 자격증명을 찾지 못했습니다")
    else:
        try:
            alter_database_passwords(
                project,
                root_auth,
                database_user,
                app_password=current["db_password"],
            )
        except Exception as error:
            errors.append(f"DB 애플리케이션 계정: {error}")

    try:
        set_wordpress_db_config(
            project,
            current["db_password"],
            one_off=True,
        )
    except Exception as error:
        errors.append(f"WordPress DB 설정: {error}")

    for kind, secret_name in (
        ("admin", "wp_admin_password"),
        ("user", "wp_user_password"),
    ):
        try:
            set_wordpress_user(
                project,
                kind,
                current[secret_name],
                one_off=True,
            )
        except Exception as error:
            errors.append(f"WordPress {kind} 계정: {error}")

    if root_auth is not None:
        try:
            alter_database_passwords(
                project,
                root_auth,
                database_user,
                new_root_password=current["db_root_password"],
            )
        except Exception as error:
            errors.append(f"DB root 계정: {error}")

    for name, path in paths.items():
        try:
            atomic_secret_write(path, current[name])
        except Exception as error:
            errors.append(f"호스트 비밀 파일 {path.name}: {error}")

    recovered = False
    try:
        project.run(
            "up", "--detach", "--force-recreate", "--wait", "--wait-timeout", "300"
        )
        verify_rotation(project, database_user, current, replacement)
        for name, path in paths.items():
            if read_secret(path, require_owner=True) != current[name]:
                raise RotationError(f"호스트 비밀 파일 복구 검증 실패: {path.name}")
        recovered = True
    except Exception as error:
        errors.append(f"최종 재기동·검증: {error}")
    return errors, recovered
