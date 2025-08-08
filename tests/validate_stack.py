#!/usr/bin/env python3
from pathlib import Path
import re
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "srcs" / "docker-compose.yml"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def require_file(path: str) -> Path:
    full_path = ROOT / path
    if not full_path.is_file():
        fail(f"missing required file: {path}")
    return full_path


def require_text(path: str, patterns: list[str]) -> str:
    text = require_file(path).read_text()
    for pattern in patterns:
        if not re.search(pattern, text, re.MULTILINE):
            fail(f"{path} does not match {pattern!r}")
    return text


def require_executable(path: str) -> None:
    mode = require_file(path).stat().st_mode
    if not mode & stat.S_IXUSR:
        fail(f"{path} must be executable")


def validate_source_only() -> None:
    forbidden = [
        "docs",
        "notes",
        "evidence",
        "PLAN.md",
        "FAILURE_CASES.md",
        "COMMIT_SCENARIO.md",
        "TIMELINE.md",
        "docker-compose.yml",
        "conf",
        "src",
        "include",
    ]
    for item in forbidden:
        if (ROOT / item).exists():
            fail(f"forbidden final path exists: {item}")


def validate_compose() -> None:
    text = require_text(
        "srcs/docker-compose.yml",
        [
            r"services:",
            r"^\s+nginx:",
            r"^\s+mariadb:",
            r"^\s+wordpress:",
            r"HTTPS_BIND_ADDRESS:-127\.0\.0\.1",
            r"HTTPS_PORT:-443",
            r"condition: service_healthy",
            r"healthcheck:",
            r"x-secret-files:",
            r"mariadb_data:",
            r"wordpress_data:",
            r"wordpress_config:",
        ],
    )
    if re.search(r"(^|\s)-\s*[\"']?80:", text):
        fail("nginx must not publish port 80")
    if "mysqladmin ping -h127.0.0.1 -uroot" in text:
        fail("mariadb healthcheck must not require TCP root login")
    if not re.search(
        r"test -f /var/lib/mysql-volume/data/\.container-stack-initialized.+test -S /run/mysqld/mysqld\.sock.+kill -0 1",
        text,
    ):
        fail("mariadb healthcheck must require the completed bootstrap marker")
    if "/run/secrets" in text or re.search(r"^\s+secrets:", text, re.MULTILINE):
        fail("runtime services must not mount secret files")
    if re.search(r"^\s{6}[A-Z0-9_]*PASSWORD(?:_FILE)?:", text, re.MULTILINE):
        fail("runtime service environments must not contain passwords")
    if "/var/www/config" in re.search(
        r"(?ms)^\s+nginx:.*?(?=^\s{2}[a-z])", text
    ).group(0):
        fail("nginx must not mount the WordPress configuration volume")
    if not re.search(
        r"test -f /var/www/html/\.container-stack-initialized.+REQUEST_METHOD=GET\s+SCRIPT_NAME=/ping\s+SCRIPT_FILENAME=/ping\s+cgi-fcgi",
        text,
    ):
        fail("wordpress healthcheck must require bootstrap completion before FPM ping")
    for image in ("wordpress:", "mariadb:", "nginx:"):
        if re.search(rf"image:\s*{image}", text):
            fail(f"compose must not use the official {image.rstrip(':')} image directly")


def validate_dockerfiles() -> None:
    services = {
        "nginx": [
            r"FROM\s+debian:bookworm-slim|FROM\s+alpine:",
            r"apt-get install|apk add",
            r"COPY conf/nginx\.conf",
            r"EXPOSE 443",
        ],
        "mariadb": [
            r"FROM\s+debian:bookworm-slim|FROM\s+alpine:",
            r"mariadb-server",
            r"rm -rf /var/lib/mysql",
            r"COPY conf/50-server\.cnf",
            r"ENTRYPOINT",
        ],
        "wordpress": [
            r"FROM\s+debian:bookworm-slim|FROM\s+alpine:",
            r"php8\.2-fpm|php-fpm",
            r"wp-cli\.phar",
            r"EXPOSE 9000",
        ],
    }
    for service, patterns in services.items():
        require_file(f"srcs/requirements/{service}/.dockerignore")
        require_text(f"srcs/requirements/{service}/Dockerfile", patterns)
        require_executable(f"srcs/requirements/{service}/tools/docker-entrypoint.sh")


def validate_configs() -> None:
    require_text(
        "srcs/requirements/nginx/conf/nginx.conf",
        [
            r"listen 443 ssl http2",
            r"fastcgi_pass wordpress:9000",
            r"ssl_certificate",
            r"location = /healthz",
        ],
    )
    if "http2 on;" in require_file("srcs/requirements/nginx/conf/nginx.conf").read_text():
        fail("nginx config must use Debian-compatible listen http2 syntax")
    require_text(
        "srcs/requirements/mariadb/conf/50-server.cnf",
        [r"bind-address=0\.0\.0\.0", r"character-set-server=utf8mb4"],
    )
    require_text(
        "srcs/requirements/wordpress/conf/www.conf",
        [r"listen = 0\.0\.0\.0:9000", r"ping\.path = /ping", r"clear_env = yes"],
    )


def validate_env_policy() -> None:
    env_text = require_file(".env.example").read_text()
    for key in (
        "DOMAIN_NAME",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "DB_ROOT_PASSWORD_FILE",
        "DB_PASSWORD_FILE",
        "WP_ADMIN_PASSWORD_FILE",
        "WP_USER_PASSWORD_FILE",
    ):
        if f"{key}=" not in env_text:
            fail(f".env.example is missing {key}")
    if re.search(r"PASSWORD=change-me", env_text):
        fail(".env.example must point to secret files instead of embedding passwords")


def validate_tools() -> None:
    require_executable("tools/smoke_https.sh")
    require_executable("tools/start_stack.py")
    require_file("tools/stack_runtime.py")
    require_text(
        "Makefile",
        [
            r"^up:\n\s+python3 tools/start_stack\.py start",
            r"^start-database:",
            r"^start-application:",
            r"^smoke:",
            r"tools/smoke_https\.sh",
            r"^bootstrap-test:",
            r"runtime_stack\.py bootstrap",
            r"^e2e:",
            r"runtime_stack\.py e2e",
            r"^persistence:",
            r"runtime_stack\.py persistence",
            r"^backup:",
            r"stack_backup\.py backup",
            r"^restore:",
            r"stack_backup\.py restore",
            r"^backup-restore-test:",
            r"runtime_stack\.py backup-restore",
            r"^rotate-secrets:",
            r"rotate_secrets\.py",
            r"^rotation-test:",
            r"runtime_stack\.py rotation",
        ],
    )
    require_text(
        "tools/stack_backup.py",
        [
            r"--single-transaction",
            r"sha256",
            r"ensure_fresh_project",
            r"validate_archive",
            r"os\.replace",
            r"O_NOFOLLOW",
            r"fcntl\.flock",
            r"defaults-extra-file",
            r"mktemp /run/container-stack",
            r"output\.mkdir\(mode=0o700\)",
            r"fsync_directory",
            r"cleanup_failed_restore",
            r"database-restore",
            r"input_stream",
            r"operation_signal_handlers",
            r"signal\.SIGINT",
            r"signal\.SIGTERM",
            r"project_operation_lock",
            r"Path\([\"']\/tmp[\"']\)",
            r"pause_for_test",
            r"--pause-after",
            r"--pause-ready-file",
        ],
    )
    backup_tool = require_file("tools/stack_backup.py").read_text()
    if re.search(r"-p(?:\\?['\"])?\$\(cat", backup_tool):
        fail("database client passwords must not be exposed in command arguments")
    require_text(
        "tools/rotate_secrets.py",
        [
            r"def atomic_secret_write",
            r"def verify_rotation",
            r"def maybe_fail",
            r"rollback_errors",
            r"--new-secrets-dir",
            r"project_operation_lock",
            r"tempnam",
            r"SIGNAL SQLSTATE",
            r"admin-user-command",
            r"root-password-command",
            r"host-file",
            r"find_root_password",
            r"verify_runtime_secret_boundary",
            r"O_NOFOLLOW",
            r"signal\.SIGINT",
            r"signal\.SIGTERM",
            r"one_off",
            r'"run",\s*\n\s*"--rm"',
            r'"--entrypoint",\s*\n\s*"php"',
            r"recreate-wordpress-removed",
            r'project\.run\("rm", "--stop", "--force", "wordpress"\)',
            r'"rollback_active"',
            r'"deferred"',
            r"--pause-after",
            r"--rollback-ready-file",
        ],
    )
    rotation_tool = require_file("tools/rotate_secrets.py").read_text()
    if re.search(r"auth=/tmp/container-stack-(?:root|app)\.\$\$", rotation_tool):
        fail("rotation database clients must use unpredictable private option files")
    require_text(
        "tests/runtime_stack.py",
        [
            r"--project-name",
            r"--resolve",
            r'"post",\s*\n\s*"create"',
            r"tools.+start_stack\.py",
            r'"bootstrap",\s*"e2e"',
            r"def verify_persistence",
            r"len\(initial_volumes\) != 3",
            r'command = \["docker", kind, "ls"\]',
            r'"restart"',
            r'"down", "--remove-orphans"',
            r"def verify_backup_restore",
            r"missing-backup-target",
            r"database-dump",
            r"database-restore",
            r"BACKUP_TOOL_TIMEOUT_SECONDS\s*=\s*1200",
            r"time\.monotonic\(\)",
            r"process\.kill\(\)",
            r"--pause-after",
            r"--pause-ready-file",
            r"backup-stop",
            r"signal\.SIGTERM",
            r"signal\.SIGINT",
            r"def project_resources",
            r"def verify_services_running",
            r'"ps", "--status", "running", "--services"',
            r"\(\"container\", \"volume\", \"network\"\)",
            r"command\.append\(\"--all\"\)",
            r"TMPDIR",
            r"다른 관리 작업이 실행 중입니다",
            r"def verify_secret_rotation",
            r"timeout=600",
            r"def _assert_rotation_state",
            r"admin-user-command",
            r"root-password-command",
            r"host-file",
            r"config-command",
            r"recreate-wordpress-removed",
            r"def _interrupt_rotation_tool",
            r"rotation-host-files\.ready",
            r"rotation-rollback\.ready",
            r"추가 종료 신호 지연 처리",
        ],
    )


def validate_bootstrap_recovery() -> None:
    require_text(
        "srcs/requirements/mariadb/tools/docker-entrypoint.sh",
        [
            r"\.container-stack-initialized",
            r"timed out waiting for temporary MariaDB server",
            r"staging_dir",
            r"database-publish",
            r"ALTER USER '\$\{MYSQL_USER\}'@'%'",
        ],
    )
    require_text(
        "srcs/requirements/wordpress/tools/docker-entrypoint.sh",
        [
            r"\.container-stack-initialized",
            r"timed out waiting for authenticated MariaDB access",
            r"wp core is-installed",
            r"config_dir=.*?/var/www/config",
        ],
    )


def main() -> None:
    validate_source_only()
    validate_compose()
    validate_dockerfiles()
    validate_configs()
    validate_env_policy()
    validate_tools()
    validate_bootstrap_recovery()
    print("static stack validation passed")


if __name__ == "__main__":
    main()
