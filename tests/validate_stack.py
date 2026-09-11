#!/usr/bin/env python3
# 이 파일은 컨테이너를 실제로 띄우지 않는 "정적" 검증이다 — docker-compose.yml, Dockerfile, 셸 스크립트,
# 다른 파이썬 도구들의 소스 코드를 텍스트/정규식으로 훑어서 "이전에 내린 설계 결정이 그대로 남아있는지"를
# 확인한다. 예를 들어 특정 베이스 이미지 다이제스트, 체크섬 값, 심지어 다른 파일의 함수 이름까지 문자열로
# 못박아두고 검사하는데, 이는 리팩터링 도중 의도적으로 넣어둔 방어 로직이나 고정값이 조용히 사라지는 것을
# 잡아내기 위한 회귀 테스트 기법이다(실제 동작 검증은 tests/runtime_stack.py가 담당)
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
    # stat.S_IXUSR — "소유자 실행" 권한 비트 하나만 뜻하는 상수. 비트 AND 결과가 0이면 그 비트가 꺼져 있다는 뜻
    if not mode & stat.S_IXUSR:
        fail(f"{path} must be executable")


def validate_source_only() -> None:
    # 계획 문서·메모·과거 산출물 등이 최종 제출물에 섞여 들어가지 않았는지 확인 — 코드 동작과는 무관한
    # 저장소 구조/제출 규칙을 강제하는 검사
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
    # (?ms): re.MULTILINE(^가 각 줄의 시작에 매치)과 re.DOTALL(.이 줄바꿈도 포함)을 정규식 안에 인라인으로 켜는 문법.
    # 뒤의 (?=...)는 "실제로 소비하지 않고 그 지점에 다음 패턴이 있는지만 확인"하는 전방탐색(lookahead) —
    # "nginx:" 항목 전체를, 다음 서비스 키(들여쓰기 2칸 + 소문자)가 나오기 직전까지만 잘라내는 데 쓰인다
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
    if "container_name:" in text:
        fail("compose services must use project-scoped generated container names")
    for pattern in (r"WORDPRESS_URL:", r"STACK_IMAGE_PREFIX", r"STACK_IMAGE_TAG"):
        if not re.search(pattern, text):
            fail(f"compose does not match {pattern!r}")
    for required in (
        "cpus:",
        "mem_limit:",
        "pids_limit:",
        "no-new-privileges:true",
        "driver: json-file",
        'max-size: "10m"',
        'max-file: "3"',
        "stop_grace_period:",
    ):
        # 서비스마다 따로 정규식을 쓰는 대신, "이 문자열이 파일 전체에서 정확히 3번(nginx/mariadb/wordpress 각각
        # 한 번씩) 나오는지"로 세 서비스 모두 같은 정책을 갖췄는지 한 번에 확인하는 기법
        if text.count(required) != 3:
            fail(f"all three services must set the runtime policy: {required}")
    if not re.search(r"backend:\s+driver: bridge\s+internal: true", text):
        fail("database network must be an internal bridge")


def validate_dockerfiles() -> None:
    services = {
        "nginx": [
            r"FROM\s+debian:bookworm(?:-\d{8})?-slim|FROM\s+alpine:",
            r"apt-get install|apk add",
            r"COPY conf/nginx\.conf",
            r"EXPOSE 443",
        ],
        "mariadb": [
            r"FROM\s+debian:bookworm(?:-\d{8})?-slim|FROM\s+alpine:",
            r"mariadb-server",
            r"rm -rf /var/lib/mysql",
            r"COPY conf/50-server\.cnf",
            r"ENTRYPOINT",
        ],
        "wordpress": [
            r"FROM\s+debian:bookworm(?:-\d{8})?-slim|FROM\s+alpine:",
            r"php8\.2-fpm|php-fpm",
            r"wp-cli-\$\{WP_CLI_VERSION\}\.phar",
            r"EXPOSE 9000",
        ],
    }
    for service, patterns in services.items():
        require_file(f"srcs/requirements/{service}/.dockerignore")
        dockerfile = require_text(f"srcs/requirements/{service}/Dockerfile", patterns)
        if "bookworm-20241202-slim@sha256:1537a6a1cbc4b4fd401da800ee9480207e7dc1f23560c21259f681db56768f63" not in dockerfile:
            fail(f"{service} must pin the Debian base image digest")
        if "snapshot.debian.org/archive/debian/20241214T000000Z" not in dockerfile:
            fail(f"{service} must use the immutable Debian package snapshot")
        require_executable(f"srcs/requirements/{service}/tools/docker-entrypoint.sh")

    wordpress = require_file("srcs/requirements/wordpress/Dockerfile").read_text()
    for required in (
        "WP_CLI_VERSION=2.11.0",
        "WORDPRESS_VERSION=6.7.1",
        "a39021ac809530ea607580dbf93afbc46ba02f86b6cffd03de4b126ca53079f6",
        "33529cd638c845007e8e0d26c91d60c9c16b822c849c8deead03d0c851a26deb",
        "sha256sum -c -",
        "/usr/src/wordpress-core.sha256",
    ):
        if required not in wordpress:
            fail(f"wordpress image is missing pinned artifact data: {required}")
    entrypoint = require_file(
        "srcs/requirements/wordpress/tools/docker-entrypoint.sh"
    ).read_text()
    # "wp core download"가 없어야 한다는 건 곧 "컨테이너 기동 시점에 인터넷에서 새로 워드프레스를 받지 않는다"는
    # 뜻 — 빌드 시점에 이미 이미지 안에 검증해 넣어둔 사본을 entrypoint가 그대로 복사만 하는 설계
    # (wordpress Dockerfile/entrypoint 주석 참고)를 테스트로 못박아 둔 것
    if (
        "wp core download" in entrypoint
        or "/usr/src/wordpress-core.sha256" not in entrypoint
        or 'cp -p -- "$source" "$temporary"' not in entrypoint
    ):
        fail("WordPress must copy the verified image artifact instead of downloading at runtime")


def validate_configs() -> None:
    require_text(
        "srcs/requirements/nginx/conf/nginx.conf",
        [
            r"listen 443 ssl http2",
            r"fastcgi_pass wordpress:9000",
            r"ssl_certificate",
            r"location = /healthz",
            r"access_log /dev/stdout",
            r"error_log /dev/stderr warn",
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


# 아래 validate_tools()는 다른 도구 파일의 소스 코드 안에서 특정 함수 이름·변수명·문자열 리터럴이
# 그대로 존재하는지를 검사한다 — "동작이 같은가"가 아니라 "그 방어 로직/구조가 코드에 실제로 남아있는가"를
# 훨씬 싸고 빠르게(컨테이너를 띄우지 않고) 확인하는 방식. 그만큼 다른 파일을 리팩터링해 이름이 바뀌면
# 이 목록도 함께 갱신해야 하는 강한 결합을 감수한 설계다
def validate_tools() -> None:
    require_executable("tools/smoke_https.sh")
    require_text("tools/smoke_https.sh", [r"curl .+--connect-timeout", r"curl .+--max-time"])
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
            r"^config-strict:",
            r"config --quiet",
            r"^diagnostics:",
            r"diagnose_stack\.py",
            r"^operations-test:",
            r"runtime_stack\.py operations",
            r"DESTROY_CONFIRM",
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
        "tools/diagnose_stack.py",
        [
            r"secret_values",
            r"def redact",
            r"0o600",
            r"0o700",
            r"--no-interpolate",
            r"--tail",
            r"container_state",
            r"read_private_secret",
            r"가릴 비밀값을 읽을 수 없습니다",
        ],
    )
    require_executable("tools/diagnose_stack.py")
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
            r"def verify_operations",
            r"no-new-privileges:true",
            r"operations-diagnostics",
            r"unreadable-secret-diagnostics",
            r"missing-diagnostics-target",
        ],
    )


def validate_bootstrap_recovery() -> None:
    # entrypoint 스크립트가 "부트스트랩 도중 죽었다가 재시작"하는 경로를 실제로 갖추고 있는지, 관련 핵심
    # 식별자(마커 파일, 스테이징 디렉터리, 에러 메시지)가 여전히 소스에 남아있는지를 문자열로 확인
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


def validate_rotation_runtime_boundary() -> None:
    # 비밀 회전 기능이 예전 설계(컨테이너에 /run/secrets로 비밀 파일을 직접 마운트하던 방식)로 퇴행하지
    # 않았는지 금지어 목록으로 확인하고, 현재 설계(wp-config를 별도 볼륨에서 원자적으로 교체)에 필요한
    # 코드 조각이 남아있는지를 함께 확인
    runtime = require_file("tests/runtime_stack.py").read_text()
    forbidden = (
        "def _mounted_secret_matches",
        "/run/secrets/db_root_password",
        "/run/secrets/wp_admin_password",
    )
    for fragment in forbidden:
        if fragment in runtime:
            fail(f"rotation validation depends on an obsolete secret mount: {fragment}")
    for required in (
        "find /var/www/config -maxdepth 1 -type f",
        ".wp-config.rotate.*",
        "self.assert_runtime_secret_boundary(expected)",
    ):
        if required not in runtime:
            fail(f"rotation runtime boundary is missing {required!r}")


def main() -> None:
    validate_source_only()
    validate_compose()
    validate_dockerfiles()
    validate_configs()
    validate_env_policy()
    validate_tools()
    validate_bootstrap_recovery()
    validate_rotation_runtime_boundary()
    print("static stack validation passed")


if __name__ == "__main__":
    main()
