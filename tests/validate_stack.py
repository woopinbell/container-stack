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
            r"\"443:443\"",
            r"condition: service_healthy",
            r"healthcheck:",
            r"secrets:",
            r"mariadb_data:",
            r"wordpress_data:",
        ],
    )
    if re.search(r"(^|\s)-\s*[\"']?80:", text):
        fail("nginx must not publish port 80")
    if "mysqladmin ping -h127.0.0.1 -uroot" in text:
        fail("mariadb healthcheck must not require TCP root login")
    if not re.search(
        r"mysqladmin\s+--socket=/run/mysqld/mysqld\.sock\s+-uroot\s+-p.+\s+ping\s+--silent",
        text,
    ):
        fail("mariadb healthcheck must use the local socket as root")
    if not re.search(
        r"REQUEST_METHOD=GET\s+SCRIPT_NAME=/ping\s+SCRIPT_FILENAME=/ping\s+cgi-fcgi",
        text,
    ):
        fail("wordpress healthcheck must call the FPM ping endpoint as a GET request")
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
            r"rm -rf /var/lib/mysql/\*",
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
        [r"listen = 0\.0\.0\.0:9000", r"ping\.path = /ping"],
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


def main() -> None:
    validate_source_only()
    validate_compose()
    validate_dockerfiles()
    validate_configs()
    validate_env_policy()
    print("static stack validation passed")


if __name__ == "__main__":
    main()
