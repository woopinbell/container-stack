#!/bin/sh
# [INTV:ARCH] set -e: 명령 실패(0이 아닌 종료 코드) 시 스크립트 즉시 중단. set -u: 정의되지 않은 변수
# 참조를 에러로 처리(오타로 빈 변수를 쓰는 실수를 조기에 잡음).
# - [FLOW] 이 스크립트는 두 역할을 겸한다 — "bootstrap" 인자로 한 번 호출되어 DB를 초기화하고, 이후
#   평소 시작 시에는 인자 없이 호출되어 상태만 점검한 뒤 실제 mariadbd로 넘어간다(맨 아래 분기 참고).
set -eu

# [INTV:ARCH] "${VAR:-기본값}" — VAR가 비어있거나 없으면 기본값을 쓰되 VAR 자체는 바꾸지 않는 셸
# 파라미터 확장 문법.
volume_dir="${MARIADB_VOLUME_DIR:-/var/lib/mysql-volume}"
data_dir="${volume_dir}/data"
staging_dir="${volume_dir}/.container-stack-bootstrap"
run_dir="${MARIADB_RUN_DIR:-/run/mysqld}"
socket="${run_dir}/mysqld.sock"
marker="${data_dir}/.container-stack-initialized"
wait_retries="${MARIADB_INIT_WAIT_RETRIES:-60}"
wait_delay="${MARIADB_INIT_WAIT_DELAY:-1}"
temporary_pid=""
root_option_file=""
app_option_file=""

fail() {
    echo "$*" >&2
    exit 1
}

require_name() {
    # [INTV:ARCH] case의 글롭 패턴 매칭으로 입력을 검증하는 POSIX 셸 관용구(정규식 대신 사용) —
    # "허용 문자 집합의 부정(!...)"에 하나라도 걸리거나 빈 문자열이면 매칭되어 실패 처리된다.
    case "$2" in
        *[!A-Za-z0-9_]*|"")
            fail "$1 must contain only letters, numbers, and underscores"
            ;;
    esac
}

require_password() {
    case "$2" in
        *[!A-Za-z0-9_.~!@#%^+=,-]*|"")
            fail "$1 has an invalid format"
            ;;
    esac
    # "${#변수}" — 문자열 길이를 구하는 셸 파라미터 확장
    length="${#2}"
    if [ "$length" -lt 24 ] || [ "$length" -gt 128 ]; then
        fail "$1 must contain 24 to 128 characters"
    fi
}

require_positive_integer() {
    case "$2" in
        *[!0-9]*|"") fail "$1 must be a positive integer" ;;
    esac
    [ "$2" -gt 0 ] || fail "$1 must be a positive integer"
}

# [INTV:ARCH] 테스트 하네스(tests/, tools/)가 부트스트랩의 특정 단계 직후에 컨테이너를 멈춰 세우고 그
# 시점의 상태를 검사할 수 있게 해주는 훅. 지정한 단계(CONTAINER_STACK_PAUSE_AFTER)에 도달하면 "준비됐다"는
# 파일을 만들고 그대로 무한 대기에 들어간다 — 테스트가 이 파일 생성을 신호로 감지한 뒤 컨테이너 안을 들여다본다.
pause_after() {
    stage="$1"
    [ "${CONTAINER_STACK_PAUSE_AFTER:-}" = "$stage" ] || return 0
    ready_name="${CONTAINER_STACK_PAUSE_READY_FILE:-ready}"
    case "$ready_name" in
        ""|.|..|*/*) fail "invalid pause ready filename" ;;
    esac
    install -d -m 0700 /run/container-stack-test
    ready="/run/container-stack-test/${ready_name}"
    (umask 077; printf '%s\n' "$stage" >"$ready")
    # [INTV:TRAP] 무한 sleep 루프 — 프로세스를 종료시키지 않으면서 이후 로직이 진행되지 못하게 그
    # 자리에 묶어두는 관용적 방법. sleep 대신 exit로 재구현하면 테스트가 컨테이너를 살아있는 상태로
    # 들여다볼 기회 자체가 사라진다.
    while :; do
        sleep 3600
    done
}

# [INTV:EDGE] 부트스트랩 중 띄워둔 임시 mariadbd가 아직 살아있다면 정리하고, 자격증명이 담긴 옵션
# 파일도 지운다. trap으로 등록되어 스크립트가 정상 종료든 실패든 항상 실행된다(아래 trap cleanup EXIT).
# - [TRAP] cleanup을 trap 없이 성공 경로 끝에서만 호출하면, 중간에 실패해서 스크립트가 죽었을 때 임시
#   서버 프로세스와 비밀번호가 담긴 옵션 파일이 그대로 남는다.
cleanup() {
    if [ -n "$temporary_pid" ] && kill -0 "$temporary_pid" 2>/dev/null; then
        kill -TERM "$temporary_pid" 2>/dev/null || true
        wait "$temporary_pid" 2>/dev/null || true
    fi
    temporary_pid=""
    [ -z "$root_option_file" ] || rm -f -- "$root_option_file"
    [ -z "$app_option_file" ] || rm -f -- "$app_option_file"
}

start_temporary_server() {
    server_data_dir="$1"
    rm -f -- "$socket"
    # [INTV:EDGE] --skip-networking: TCP를 아예 열지 않고 로컬 소켓으로만 접속을 받는다 — 계정/권한
    # 설정이 끝나기 전인 부트스트랩 단계에서 네트워크로 접근 가능한 상태를 만들지 않기 위한 보안 조치.
    mariadbd --user=mysql --datadir="$server_data_dir" --skip-networking \
        --socket="$socket" --pid-file="${run_dir}/bootstrap.pid" &
    # "$!" — 방금 백그라운드로 띄운(&) 마지막 명령의 PID를 담는 셸 특수 변수
    temporary_pid="$!"

    remaining="$wait_retries"
    while [ "$remaining" -gt 0 ]; do
        # [INTV:PERF] 고정 시간만큼 무작정 기다리는 대신, 서버가 실제로 응답할 때까지 짧은 간격으로 폴링.
        if mysqladmin --socket="$socket" ping --silent >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$temporary_pid" 2>/dev/null; then
            wait "$temporary_pid" || true
            temporary_pid=""
            fail "temporary MariaDB server exited during bootstrap"
        fi
        remaining=$((remaining - 1))
        [ "$remaining" -gt 0 ] || fail "timed out waiting for temporary MariaDB server"
        sleep "$wait_delay"
    done
}

stop_temporary_server() {
    mysqladmin --defaults-extra-file="$root_option_file" shutdown
    wait "$temporary_pid"
    temporary_pid=""
}

write_option_file() {
    # [INTV:EDGE] 비밀번호를 mysql/mariadb 커맨드라인 인자로 바로 넘기면 `ps aux` 등으로 다른
    # 프로세스에서도 보인다. 그 대신 --defaults-extra-file로 읽는 옵션 파일에 적어두고, umask 077로
    # 생성 순간부터 소유자 외에는 못 읽게 만들어 비밀번호 노출 창구를 없앤다.
    # - [TRAP] umask를 옵션 파일 생성 "이후"에 적용하면 이미 파일이 기본 권한(예: 644)으로 생성된
    #   짧은 순간이 생겨, 그 사이 다른 프로세스가 읽을 수 있는 레이스가 발생한다.
    target="$1"
    user="$2"
    password="$3"
    (
        umask 077
        printf '[client]\nuser=%s\npassword="%s"\nsocket=%s\n' \
            "$user" "$password" "$socket" >"$target"
    )
}

verify_database() {
    mariadb --defaults-extra-file="$root_option_file" --batch --skip-column-names \
        --execute="SELECT COUNT(*) FROM mysql.user WHERE User='${MYSQL_USER}' AND Host='%'" \
        | grep -qx 1
    mariadb --defaults-extra-file="$app_option_file" "$MYSQL_DATABASE" \
        --batch --skip-column-names --execute='SELECT 1' | grep -qx 1
}

runtime() {
    [ -d "${data_dir}/mysql" ] || fail "MariaDB data is not bootstrapped; run tools/start_stack.py"
    [ -f "$marker" ] && [ ! -L "$marker" ] \
        || fail "MariaDB completion marker is missing; rerun bootstrap"
    # [INTV:EDGE] exec "$@" — 현재 셸 프로세스를 새 프로세스로 "교체"한다(자식을 새로 fork하지 않음).
    # - [TRAP] exec 없이 서브셸/백그라운드로 mariadbd를 띄우면 셸이 PID 1로 남아, docker stop의
    #   SIGTERM을 셸이 대신 받아버려 mariadbd에게 정상 종료 신호가 전달되지 않는다.
    exec "$@"
}

bootstrap() {
    : "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
    : "${MYSQL_USER:?MYSQL_USER is required}"
    require_name MYSQL_DATABASE "$MYSQL_DATABASE"
    require_name MYSQL_USER "$MYSQL_USER"
    require_positive_integer MARIADB_INIT_WAIT_RETRIES "$wait_retries"
    require_positive_integer MARIADB_INIT_WAIT_DELAY "$wait_delay"

    # [INTV:EDGE] 비밀번호를 환경변수가 아니라 표준입력으로 두 줄 받는다 — 환경변수로 넘기면 docker
    # inspect나 /proc/<pid>/environ으로 다른 곳에서도 읽힐 수 있어서, 노출 범위가 좁은 stdin을 택했다.
    IFS= read -r root_password || fail "missing root password on standard input"
    IFS= read -r app_password || fail "missing application password on standard input"
    if IFS= read -r _unexpected; then
        fail "unexpected extra bootstrap input"
    fi
    require_password MYSQL_ROOT_PASSWORD "$root_password"
    require_password MYSQL_PASSWORD "$app_password"

    install -d -m 0755 -o mysql -g mysql "$run_dir" "$volume_dir"
    root_option_file="$(mktemp "${run_dir}/root-client.XXXXXX")"
    app_option_file="$(mktemp "${run_dir}/app-client.XXXXXX")"
    chmod 0600 "$root_option_file" "$app_option_file"
    write_option_file "$root_option_file" root "$root_password"
    write_option_file "$app_option_file" "$MYSQL_USER" "$app_password"

    if [ -e "$data_dir" ]; then
        # [INTV:FLOW] 이미 초기화된 볼륨으로 재기동하는 경우 — 새로 만들지 않고 기존 계정/DB가 여전히
        # 유효한지만 검증한다.
        [ -d "${data_dir}/mysql" ] || fail "MariaDB data path is not a valid data directory"
        [ -f "$marker" ] && [ ! -L "$marker" ] \
            || fail "MariaDB data exists without a completion marker"
        start_temporary_server "$data_dir"
        pause_after temporary-server
        verify_database
        stop_temporary_server
        return 0
    fi

    # [INTV:EDGE] 아래부터는 최초 초기화 경로. 최종 위치(data_dir)에 바로 만들지 않고 임시 스테이징
    # 디렉터리에서 전체 과정을 마친 뒤 마지막에 한 번에 옮긴다(파일 하단 mv 참고) — 중간에 컨테이너가
    # 죽어도 data_dir에는 "전부 완료됐거나 아예 없거나" 둘 중 하나만 남도록 하는 원자적 발행 패턴.
    # - [TRAP] 스테이징 없이 data_dir에서 직접 초기화를 진행하면, 부트스트랩 도중 컨테이너가 죽었을 때
    #   "절반만 초기화된 데이터 디렉터리"가 남아 재시작 시 판단이 애매한 상태가 된다.
    rm -rf -- "$staging_dir"
    install -d -m 0700 -o mysql -g mysql "$staging_dir"
    mariadb-install-db --user=mysql --datadir="$staging_dir" --skip-test-db >/dev/null
    pause_after system-tables

    start_temporary_server "$staging_dir"
    pause_after temporary-server
    # [INTV:EDGE] <<SQL ... SQL — 따옴표 없는 heredoc이라 안의 ${root_password} 같은 변수가 셸에 의해
    # 그대로 치환된다. NO_BACKSLASH_ESCAPES를 먼저 켜는 이유는, 치환될 비밀번호에 우연히 백슬래시가
    # 들어있어도 SQL 문자열 리터럴 안에서 이스케이프로 잘못 해석되지 않게 막기 위함(구문 오염 방지).
    mariadb --socket="$socket" -uroot <<SQL
SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';
ALTER USER 'root'@'localhost' IDENTIFIED BY '${root_password}';
DELETE FROM mysql.user WHERE User='';
DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost');
DROP DATABASE IF EXISTS test;
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'%' IDENTIFIED BY '${app_password}';
ALTER USER '${MYSQL_USER}'@'%' IDENTIFIED BY '${app_password}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL
    pause_after database-state
    verify_database
    stop_temporary_server

    # [INTV:EDGE] 마커 파일을 스테이징 디렉터리 "안"에 먼저 만들고 fsync(sync -f)로 디스크에 확실히
    # 반영한 뒤에야 디렉터리 자체를 최종 위치로 rename(mv)한다 — POSIX에서 같은 파일시스템 내 rename은
    # 원자적이라, 정전/강제종료가 나도 data_dir는 "마커까지 포함해 완전한 상태"로만 나타나거나 아예
    # 나타나지 않는다.
    # - [TRAP] sync -f 없이 바로 mv하면, 파일시스템 캐시에만 있던 내용이 실제 디스크에 반영되기 전에
    #   정전이 나서 rename 이후에도 데이터가 유실될 수 있다 (rename의 원자성은 fsync와 별개 보장이다).
    staging_marker="${staging_dir}/.container-stack-initialized"
    (umask 077; : >"$staging_marker")
    chown mysql:mysql "$staging_marker"
    sync -f "$staging_marker"
    sync -f "$staging_dir"
    pause_after database-marker
    mv -- "$staging_dir" "$data_dir"
    sync -f "$volume_dir"
    pause_after database-publish
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${1:-}" = "bootstrap" ]; then
    shift
    [ "$#" -eq 0 ] || fail "bootstrap does not accept arguments"
    bootstrap
    exit 0
fi

# [INTV:TRAP] 여기부터는 실제 서버로 넘어가는 경로 — 위에서 건 trap들은 부트스트랩 단계의 임시 자원
# 정리를 위한 것이었으므로, exec로 프로세스를 완전히 갈아치우기 전에 반드시 해제해야 한다.
# - [TRAP] trap 해제를 빼먹으면, exec 이후 mariadbd가 받아야 할 신호(TERM 등)를 이 스크립트의 trap
#   핸들러가 대신 가로채 잘못된 종료 코드로 반응하게 된다.
trap - EXIT HUP INT TERM
runtime "$@"
