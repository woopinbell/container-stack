#!/usr/bin/env python3
"""실행 중인 스택의 DB·WordPress 자격증명을 검증 가능한 절차로 회전합니다."""

# [INTV:ARCH] Saga 패턴: "비밀값 회전"은 다섯 군데(호스트의 비밀 파일 4개, MariaDB root 계정, MariaDB
# 애플리케이션 계정, wp-config.php의 DB 비밀번호, WordPress 관리자·작성자 계정 비밀번호)를 스택을 내리지
# 않고 순서대로 갈아치우는 분산 트랜잭션이다. 하나의 원자적 커밋이 불가능한 여러 시스템에 걸친 변경이라,
# 각 단계마다 실패를 흉내 낼 수 있는 테스트 훅(FAILURE_STAGES/maybe_fail)과, 실패 시 역순으로 이전
# 값으로 되돌리는 보상 트랜잭션(rollback_rotation())을 함께 갖추고 있다.
# - [TRAP] 재구현 시 "성공 아니면 전부 실패"라는 단일 트랜잭션 사고방식으로 접근하면 안 된다. 다섯
#   시스템 중 일부만 바뀐 중간 상태가 실제로 존재할 수 있다는 전제로, 각 단계·롤백 모두 멱등하거나
#   최소한 재시도 가능하게 설계해야 한다.
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

from stack_backup import (
    BackupError,
    ComposeProject,
    DEFAULT_COMPOSE_FILE,
    QUERY_TIMEOUT_SECONDS,
    pause_for_test,
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
# [INTV:ARCH] 이 이름들은 아래 _rotate()가 진행하는 실제 절차의 각 단계와 1:1로 대응한다 — CLI의
# --fail-after에 이 중 하나를 지정하면 그 단계 "직후"에 인위적으로 실패를 일으켜, 회전이 정확히 그
# 시점에서 죽었을 때도 rollback_rotation()이 이전 상태로 되돌릴 수 있는지를 검증할 수 있다(테스트
# 전용, --help에는 숨김 — argparse.SUPPRESS 참고).
FAILURE_STAGES = (
    "admin-user-command",
    "users",
    "config-command",
    "config",
    "app-password-command",
    "app-password",
    "root-password-command",
    "root-password",
    "host-file",
    "host-files",
    "recreate-wordpress-removed",
    "recreate",
)
PAUSE_STAGES = ("host-files",)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class RotationError(RuntimeError):
    pass


def read_secret(path: Path, *, require_owner: bool) -> str:
    # [INTV:EDGE] stack_runtime.py의 read_private_secret()과 같은 방어 기법(O_NOFOLLOW/O_NONBLOCK로
    # 심볼릭 링크·특수 파일 회피, fstat으로 "연 디스크립터 자체"를 검사, 1바이트 더 읽어 크기 제한
    # 확인) — 여기서는 소유자 검사를 require_owner로 켜고 끌 수 있게만 다르다.
    # - [TRAP] os.stat(path)처럼 경로로 다시 검사하면 open()과 stat() 사이에 파일이 교체되는
    #   TOCTOU(time-of-check to time-of-use) 레이스가 생긴다. 반드시 이미 연 디스크립터를 fstat할 것.
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
    # [INTV:EDGE] 최종 목적지(path)와 "같은 디렉터리"에 임시 파일을 만든다 — 그래야 os.replace가
    # 파일시스템 경계를 넘지 않는 rename이 되어 원자적으로 처리된다(디렉터리가 다르면 복사+삭제로
    # 풀릴 수 있어 원자성이 깨진다). entrypoint 셸 스크립트들의 "같은 디렉터리에 임시 파일 -> mv"
    # 패턴을 파이썬으로 재현한 것.
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # [INTV:TRAP] os.replace: 목적지가 이미 존재해도 그 자리에서 원자적으로 덮어쓴다(os.rename은
        # 플랫폼에 따라 기존 파일이 있으면 실패할 수 있어 이 용도엔 os.replace를 써야 한다).
        os.replace(temporary, path)
        # [INTV:EDGE] 파일 내용을 fsync한 것과 별개로, "이 이름이 이 파일을 가리킨다"는 디렉터리
        # 항목 자체도 디스크에 반영돼야 한다 — 디렉터리를 열어 그 디스크립터를 fsync하는 것이 그
        # 방법이다 (entrypoint 스크립트들의 `sync -f`와 같은 목적을 os 모듈로 수행).
        # - [TRAP] 파일 fsync만 하고 디렉터리 fsync를 빼먹으면, rename 자체는 됐지만 그 사실을 담은
        #   디렉터리 엔트리가 아직 디스크에 안 쓰인 상태에서 정전이 나 rename이 유실될 수 있다.
        directory = os.open(path.parent, os.O_RDONLY | DIRECTORY | NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        # 정상 경로라면 os.replace가 이미 temporary를 옮겨버려 존재하지 않는다 — 여기 도달하는 건
        # 그 전에 예외가 나 임시 파일이 아직 남아있는 실패 경로일 때뿐.
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
    # [INTV:EDGE] SQL 문자열 리터럴 이스케이프: 작은따옴표를 두 개로 이어붙이는 표준 방식으로 값 안의
    # '가 문자열 경계를 깨고 나가지 못하게 막는다. 파라미터 바인딩이 아니라 SQL 텍스트를 직접 조립해야
    # 하는 상황(아래 root_sql이 mariadb CLI로 원문 SQL을 그대로 흘려보냄)이라 수동 이스케이프가 필요하다.
    # - [TRAP] 비밀번호 후보 문자 집합에 '가 포함되어 있어(PASSWORD_PATTERN 참고) 이 이스케이프를
    #   빼먹으면 비밀번호 값 자체로 SQL 인젝션이 가능해진다.
    return "'" + value.replace("'", "''") + "'"


def root_sql(
    project: ComposeProject,
    root_password: str,
    sql: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    # [INTV:ARCH] 표준입력 하나에 "비밀번호 한 줄 + 그 뒤로 이어지는 SQL 원문"을 함께 실어 보낸다.
    # 컨테이너 안 셸은 `IFS= read -r password`로 첫 줄(비밀번호)만 소비해 옵션 파일을 만들고, 그 뒤
    # 남은 표준입력은 셸이 건드리지 않은 채 마지막 명령인 mariadb로 그대로 흘러들어가 mariadb가 그것을
    # 비대화형 SQL 입력으로 실행한다 — 파이프 하나로 두 프로그램에 각자 다른 데이터를 넘기는 방식.
    # [INTV:EDGE] 옵션 파일(--defaults-extra-file)에 비밀번호를 적어 넘기는 이유는 entrypoint
    # 스크립트와 동일 — 커맨드라인 인자(`ps`로 노출됨)에 비밀번호가 남지 않게 하기 위함.
    # - [TRAP] 이 구조를 재구현하며 read 순서를 헷갈리면(예: SQL을 먼저 보냄), 셸이 SQL 텍스트의 첫
    #   줄을 비밀번호로 오인해 옵션 파일에 SQL 구문 조각을 적어 넣는 조용한 실패가 생긴다.
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


# [INTV:ARCH] 아래 네 PHP_* 상수는 `php -r <코드>`로 wordpress 컨테이너 안에서 그대로 실행되는 짧은
# PHP 스크립트다. 셸이 아니라 PHP를 쓰는 이유는 wp-config.php 수정이나 WordPress 내부 함수
# (wp_set_password 등) 호출이 WordPress 코드베이스(PHP) 안에서만 가능하기 때문이다.
# [INTV:EDGE] 모두 stream_get_contents(STDIN)으로 JSON 페이로드를 표준입력으로 받는다 — 값(특히
# 비밀번호)을 `-r` 코드 문자열이나 커맨드라인 인자에 직접 박아 넣지 않기 위함.
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
# [INTV:TRAP] 위 realpath 비교: PHP의 tempnam()은 지정한 디렉터리에 쓸 수 없으면 조용히 시스템 임시
# 디렉터리로 대신 만들어버릴 수 있다 — 그렇게 되면 뒤이은 rename이 더 이상 원자적 교체가 아니게
# 되므로, 실제로 같은 디렉터리에 만들어졌는지를 먼저 확인해 그 함정을 걸러낸다.
# [INTV:ARCH] fail_after_write/exit(9): 파일 교체가 "이미 커밋된 뒤" 실패를 일으키는 테스트 훅 —
# 롤백 로직이 "일부는 이미 반영됐고 이후 단계만 실패한" 상황도 올바르게 복구하는지 검증하기 위함.


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
# [INTV:ARCH] wp_check_password는 wordpress entrypoint의 verify_user_password()가 부트스트랩 직후
# 검증할 때 쓰는 것과 같은 WordPress 내장 함수 — 저장된 해시와 평문 비밀번호를 비교해준다.


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
        # [INTV:ARCH] 롤백 도중에는 wordpress 서비스 컨테이너 자체가 이미 강제 재생성되었거나 제거됐을
        # 수 있어(아래 rollback_rotation/_rotate 참고), 살아있는 서비스에 exec하는 대신 같은 이미지로
        # 1회성 컨테이너를 새로 띄워 PHP 코드만 실행한다 — 공유 볼륨(wp-config, wp-content)은 특정
        # 컨테이너 인스턴스가 아니라 볼륨 자체에 있으므로 이 방식으로도 같은 상태를 다룰 수 있다.
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
    # [INTV:PERF] [INTV:EDGE] 두 계정(app/root)의 비밀번호 변경을 한 SQL 배치(세미콜론으로 이어붙인
    # 여러 문장)로 묶어 한 번의 접속으로 실행한다 — 두 번의 별도 exec 호출 사이에 한쪽만 바뀐 상태가
    # 노출되는 창(window)을 줄인다.
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
        # [INTV:ARCH] SIGNAL: MariaDB/MySQL이 SQL 배치 도중 사용자 정의 에러를 강제로 일으키는 구문 —
        # 앞선 ALTER 문들은 이미 커밋된 뒤에 이 배치 자체는 실패로 보고되게 만드는 테스트 훅.
        statements.append(
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected rotation failure'"
        )
    root_sql(project, root_password, ";\n".join(statements) + ";")


def maybe_fail(stage: str | None, current: str) -> None:
    # FAILURE_STAGES에 대응하는 실제 발동 지점 — 요청된 실패 단계 이름이 지금 막 끝낸 단계 이름과
    # 같으면 그 자리에서 실패를 발생시킨다.
    if stage == current:
        raise RotationError(f"실패 주입: {current}")


def publish_test_marker(path: Path, value: str) -> Path:
    marker = path.expanduser()
    if not marker.is_absolute():
        marker = Path.cwd() / marker
    # [INTV:EDGE] O_EXCL: 파일이 이미 존재하면 실패 — 이전 실행이 남긴 낡은 준비 파일을 새 것으로
    # 착각하지 않게 막는다.
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, (value + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return marker


def app_sql(
    project: ComposeProject,
    database_user: str,
    password: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    # [INTV:ARCH] root_sql과 같은 보안 패턴(비밀번호를 옵션 파일로)이지만, 이번엔 mariadb가 아니라
    # wordpress 컨테이너 "안에서" mariadb 서비스로 접속을 시도한다 — 자격증명 자체의 유효성만이 아니라
    # 워드프레스가 실제로 쓰는 것과 같은 네트워크 경로(-hmariadb)로도 인증이 되는지까지 확인하기 위함.
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
    # [INTV:ARCH] 심층 방어(defense in depth) 검증: "설정상 그렇게 되어 있어야 한다"가 아니라 "실제로
    # 떠 있는 컨테이너가 정말 그런 상태인지"를 docker inspect/top으로 직접 확인한다 — Compose 파일의
    # 의도가 런타임에도 실제로 지켜지는지 검증하는 마지막 방어선.
    # - [FLOW] 확인 항목: 1. 예전 방식(/run/secrets 마운트)이 어디에도 없는지 -> 2. nginx가
    #   wp-config.php 관련 볼륨/경로를 전혀 볼 수 없는지 -> 3. 컨테이너에 선언된 환경변수 "이름" 중
    #   비밀번호 계열이 없는지 -> 4. 컨테이너 안 모든 프로세스의 /proc/*/environ에도 없는지 ->
    #   5. 모든 프로세스의 커맨드라인 인자(docker top)에도 실제 비밀 "값"이 노출되지 않았는지
    # - [TRAP] 5번 항목을 빼먹으면, root_sql/app_sql이 비밀번호를 인자 대신 임시 옵션 파일로 넘기는
    #   설계(구현 의도)가 실제로 지켜지는지 아무도 검증하지 않는 셈이 된다 — "이름"만 검사하고 "값"
    #   노출은 검사하지 않는 게 이 패턴에서 가장 흔히 빠지는 구멍이다.
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
        # [INTV:EDGE] 지금까지 모아온 모든 텍스트(환경변수·프로세스 인자 등)를 대상으로, 실제 비밀
        # "값"이 글자 그대로 한 번이라도 등장하는지 마지막으로 검사 — 이름이 아니라 값 자체가 새어
        # 나갔는지 확인하는 최종 관문.
        if value and value in observed:
            raise RotationError("런타임 환경이나 프로세스 인자에 비밀값이 남았습니다")


def verify_rotation(
    project: ComposeProject,
    database_user: str,
    secrets: dict[str, str],
    rejected: dict[str, str] | None = None,
) -> None:
    # [INTV:EDGE] secrets가 "지금 유효해야 하는" 값들의 전체 기능 검증(경계 확인 + DB 로그인 +
    # wp-config 일치 + WP 계정 비밀번호 일치). rejected가 주어지면 그 반대도 함께 확인한다.
    # - [TRAP] 새 비밀번호가 통과하는 것만 확인하고 옛 비밀번호가 "거부"되는지를 확인하지 않으면,
    #   "새 비밀번호도 되고 옛 비밀번호도 여전히 되는"(즉 회전이 실제로는 실패한) 상태를 성공으로
    #   오판할 수 있다.
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
    # [INTV:EDGE] 롤백 시점에는 root 비밀번호 변경이 이미 반영됐는지 아직 안 됐는지 알 수 없다 —
    # 새 비밀번호와 이전 비밀번호를 순서대로 시도해 "지금 실제로 통하는" 쪽을 찾아낸다.
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
    # [INTV:ARCH] Saga 보상 트랜잭션: 다섯 시스템을 역순으로 되돌리는 최선 노력(best-effort) 복구 —
    # 한 단계가 실패해도 errors에 기록만 하고 나머지 단계는 계속 시도한다.
    # - [TRAP] 첫 실패에서 즉시 중단하도록 재구현하면, 오히려 "일부만 되돌려진" 더 나쁜 상태로 끝날
    #   수 있다. 가능한 한 모든 단계를 시도한 뒤 마지막에 실제로 복구됐는지 재검증(verify_rotation)
    #   하는 순서를 지킬 것 — 개별 단계의 성공 여부가 아니라 최종 상태가 진짜 판정 기준이다.
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


def _rotate(
    project: ComposeProject,
    new_secret_dir: Path,
    failure_stage: str | None,
    pause_stage: str | None,
    pause_ready_file: Path | None,
    rollback_ready_file: Path | None,
    signal_state: dict[str, bool],
) -> None:
    config = compose_config(project)
    paths = current_secret_paths(config, project.compose_file.parent)
    canonical_paths = [path.resolve(strict=True) for path in paths.values()]
    if len(set(canonical_paths)) != len(canonical_paths):
        raise RotationError("Compose 비밀값 파일 경로는 서로 달라야 합니다")
    current = {name: read_secret(path, require_owner=True) for name, path in paths.items()}
    directory = new_secret_dir.expanduser()
    try:
        directory_info = os.lstat(directory)
    except OSError as error:
        raise RotationError("새 비밀값 경로를 확인할 수 없습니다") from error
    if not stat.S_ISDIR(directory_info.st_mode):
        raise RotationError("새 비밀값 경로는 일반 디렉터리여야 합니다")
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) & 0o077:
        raise RotationError("새 비밀값 디렉터리는 현재 사용자만 접근할 수 있어야 합니다")
    replacement = {
        name: read_secret(directory / filename, require_owner=True)
        for name, filename in SECRET_FILES.items()
    }
    if any(current[name] == replacement[name] for name in SECRET_FILES):
        raise RotationError("모든 비밀값은 기존 값과 달라야 합니다")

    mariadb_environment = service_environment(config, "mariadb")
    database_user = mariadb_environment.get("MYSQL_USER", "")
    # [INTV:EDGE] database_user는 뒤에서 app_sql()이 이스케이프 없이 셸 명령 문자열에
    # f"-u{database_user}"로 직접 끼워 넣는다 — 이 정규식 검증이 그 자리의 유일한 방어선이므로,
    # sql_literal 같은 사후 이스케이프 대신 "허용 문자 집합 자체를 제한"하는 입력 검증(allowlist)을 쓴다.
    # - [TRAP] 이 정규식 검증을 빼먹고 database_user를 셸 문자열에 그대로 끼워 넣으면, Compose 설정의
    #   MYSQL_USER 값을 통한 셸 인젝션이 가능해진다 — 신뢰 경계를 넘는 값은 조립 지점이 아니라
    #   진입 지점에서 검증해야 한다는 원칙.
    if not NAME_PATTERN.fullmatch(database_user):
        raise RotationError("MYSQL_USER 형식이 안전하지 않습니다")
    wordpress_environment = service_environment(config, "wordpress")
    admin_user = wordpress_environment.get("WORDPRESS_ADMIN_USER", "")
    regular_user = wordpress_environment.get("WORDPRESS_USER", "")
    if not admin_user or not regular_user or admin_user == regular_user:
        raise RotationError("WordPress 관리자와 일반 사용자는 서로 다른 계정이어야 합니다")

    # [INTV:EDGE] 아무것도 바꾸기 전에 "현재 상태가 정말 예상과 같은지"부터 검증 — 비밀 파일과 실제
    # 서비스 상태가 이미 어긋나 있다면 회전을 시작하지 않고 여기서 멈춘다 (사전 조건 검증).
    verify_rotation(project, database_user, current, replacement)
    # [INTV:ARCH] blocked: nginx를 내렸다가 다시 올려야 하는 책임이 아직 안 끝났는지 추적하는 플래그.
    # - [TRAP] 정상 경로 끝, 또는 실패 후 롤백 성공 시에만 False로 바뀐다는 조건을 놓치고 재구현하면,
    #   예외가 나는 경로 중 일부에서 nginx가 영영 내려간 채로 남을 수 있다. finally에서 "그 외의 모든
    #   경우"에 nginx를 다시 올리는 게 핵심(이미 살아있어도 --detach만으로는 문제 없다).
    blocked = False
    try:
        blocked = True
        project.run("stop", "nginx")
        set_wordpress_user(
            project,
            "admin",
            replacement["wp_admin_password"],
            fail_after_write=failure_stage == "admin-user-command",
        )
        set_wordpress_user(project, "user", replacement["wp_user_password"])
        maybe_fail(failure_stage, "users")
        set_wordpress_db_config(
            project,
            replacement["db_password"],
            fail_after_write=failure_stage == "config-command",
        )
        maybe_fail(failure_stage, "config")
        alter_database_passwords(
            project,
            current["db_root_password"],
            database_user,
            app_password=replacement["db_password"],
            fail_after_write=failure_stage == "app-password-command",
        )
        maybe_fail(failure_stage, "app-password")
        alter_database_passwords(
            project,
            current["db_root_password"],
            database_user,
            new_root_password=replacement["db_root_password"],
            fail_after_write=failure_stage == "root-password-command",
        )
        maybe_fail(failure_stage, "root-password")
        for index, (name, path) in enumerate(paths.items()):
            atomic_secret_write(path, replacement[name])
            if index == 0:
                maybe_fail(failure_stage, "host-file")
        maybe_fail(failure_stage, "host-files")
        pause_for_test(pause_stage, "host-files", pause_ready_file)
        if failure_stage == "recreate-wordpress-removed":
            project.run("rm", "--stop", "--force", "wordpress")
            raise RotationError("실패 주입: recreate-wordpress-removed")
        project.run(
            "up", "--detach", "--force-recreate", "--wait", "--wait-timeout", "300"
        )
        maybe_fail(failure_stage, "recreate")
        verify_rotation(project, database_user, replacement, current)
        for name, path in paths.items():
            if read_secret(path, require_owner=True) != replacement[name]:
                raise RotationError(f"호스트 비밀 파일 회전 검증 실패: {path.name}")
        blocked = False
    # [INTV:EDGE] Exception이 아니라 BaseException을 잡는다 — Ctrl+C(KeyboardInterrupt), SystemExit
    # 등 보통은 그냥 전파시킬 신호성 예외까지도, 다섯 시스템 중 일부만 바뀐 채로 방치하지 않기 위해
    # 반드시 롤백 절차로 흘려보낸다.
    # - [TRAP] 여기서 Exception만 잡도록 재구현하면, 회전 도중 사용자가 Ctrl+C를 누르는 순간
    #   KeyboardInterrupt가 이 except를 우회해 그대로 위로 전파되어 롤백이 전혀 실행되지 않는다.
    except BaseException as original_error:
        signal_state["rollback_active"] = True
        marker: Path | None = None
        try:
            if rollback_ready_file is not None:
                marker = publish_test_marker(rollback_ready_file, "rollback")
            rollback_errors, recovered = rollback_rotation(
                project, paths, current, replacement, database_user
            )
        finally:
            if marker is not None:
                marker.unlink(missing_ok=True)
        if recovered:
            blocked = False
        if recovered:
            detail = "롤백 완료"
            if rollback_errors:
                detail += "; 중간 보상 오류: " + "; ".join(rollback_errors)
        else:
            detail = "롤백 불완전: " + "; ".join(rollback_errors)
        if signal_state["deferred"]:
            detail += "; 롤백 중 추가 종료 신호 지연 처리"
        raise RotationError(f"회전 실패 ({original_error}); {detail}") from original_error
    finally:
        if blocked:
            project.run("up", "--detach", check=False)
    print("비밀값 회전과 재검증을 완료했습니다")


def rotate(
    project: ComposeProject,
    new_secret_dir: Path,
    failure_stage: str | None,
    pause_stage: str | None = None,
    pause_ready_file: Path | None = None,
    rollback_ready_file: Path | None = None,
) -> None:
    previous_handlers: dict[signal.Signals, object] = {}
    signal_state = {"rollback_active": False, "deferred": False}

    # [INTV:ARCH] signal_state는 이 클로저(interrupt)와 바깥의 _rotate()가 함께 들여다보고 갱신하는
    # 공유 딕셔너리 — 딕셔너리 "내용물"을 바꾸는 것이라 파이썬의 nonlocal 선언 없이도 중첩 함수에서
    # 자유롭게 값을 읽고 쓸 수 있다 (가변 객체를 통한 클로저 상태 공유).
    # [INTV:EDGE] 롤백이 이미 시작된 뒤에 또 신호가 오면 즉시 중단시키지 않고 "나중에 알려줄 사실"로만
    # 기록해 두어, 복구 절차 자체가 중간에 끊기지 않게 한다.
    # - [TRAP] rollback_active 체크 없이 매번 즉시 raise하도록 재구현하면, 롤백 도중 사용자가 다시
    #   Ctrl+C를 누르는 순간 보상 트랜잭션 자체가 중단되어 다섯 시스템이 뒤섞인 상태로 영구히 남을
    #   위험이 있다.
    def interrupt(signum: int, _frame: object) -> None:
        if signal_state["rollback_active"]:
            signal_state["deferred"] = True
            return
        signal_name = signal.Signals(signum).name
        raise RotationError(f"{signal_name} 신호로 회전이 중단되었습니다")

    # [INTV:EDGE] signal.signal()은 이전에 등록돼 있던 핸들러를 반환한다 — 그 값을 저장해 두었다가
    # finally에서 원래대로 복원해, 이 함수가 끝난 뒤에는 평소의 신호 처리로 돌아가게 한다.
    # - [TRAP] 복원을 빼먹으면 회전 함수 호출이 끝난 뒤에도 이 프로세스 전역의 SIGINT/SIGTERM 핸들러가
    #   바뀐 채로 남아, 이후 코드의 Ctrl+C 동작이 예상과 달라지는 전역 부작용이 생긴다.
    for current_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[current_signal] = signal.signal(current_signal, interrupt)
    try:
        with project_operation_lock(project.project):
            _rotate(
                project,
                new_secret_dir,
                failure_stage,
                pause_stage,
                pause_ready_file,
                rollback_ready_file,
                signal_state,
            )
    finally:
        for current_signal, previous in previous_handlers.items():
            signal.signal(current_signal, previous)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="컨테이너 스택 비밀값 회전")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--new-secrets-dir", type=Path, required=True)
    parser.add_argument("--fail-after", choices=FAILURE_STAGES, help=argparse.SUPPRESS)
    parser.add_argument("--pause-after", choices=PAUSE_STAGES, help=argparse.SUPPRESS)
    parser.add_argument("--pause-ready-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--rollback-ready-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if shutil.which("docker") is None:
        print("docker 명령을 찾을 수 없습니다", file=sys.stderr)
        return 2
    try:
        # [INTV:TRAP] "(a is None) != (b is None)": 불리언 두 개를 !=로 비교하면 배타적 논리합(XOR)처럼
        # 동작한다 — 둘 중 정확히 하나만 None일 때(즉 하나만 지정됐을 때) 참이 되어 에러로 처리.
        if (args.pause_after is None) != (args.pause_ready_file is None):
            raise RotationError("일시정지 단계와 준비 파일을 함께 지정해야 합니다")
        if args.rollback_ready_file is not None and args.pause_after is None:
            raise RotationError("롤백 준비 파일은 일시정지 검사와 함께 지정해야 합니다")
        project = ComposeProject(args.project, args.env_file, args.compose_file)
        rotate(
            project,
            args.new_secrets_dir,
            args.fail_after,
            args.pause_after,
            args.pause_ready_file,
            args.rollback_ready_file,
        )
        return 0
    except (BackupError, RotationError, OSError, subprocess.SubprocessError) as error:
        print(f"비밀값 회전 실패: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
