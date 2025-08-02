#!/bin/sh
set -eu

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
    while :; do
        sleep 3600
    done
}

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
    mariadbd --user=mysql --datadir="$server_data_dir" --skip-networking \
        --socket="$socket" --pid-file="${run_dir}/bootstrap.pid" &
    temporary_pid="$!"

    remaining="$wait_retries"
    while [ "$remaining" -gt 0 ]; do
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
    exec "$@"
}

bootstrap() {
    : "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
    : "${MYSQL_USER:?MYSQL_USER is required}"
    require_name MYSQL_DATABASE "$MYSQL_DATABASE"
    require_name MYSQL_USER "$MYSQL_USER"
    require_positive_integer MARIADB_INIT_WAIT_RETRIES "$wait_retries"
    require_positive_integer MARIADB_INIT_WAIT_DELAY "$wait_delay"

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
        [ -d "${data_dir}/mysql" ] || fail "MariaDB data path is not a valid data directory"
        [ -f "$marker" ] && [ ! -L "$marker" ] \
            || fail "MariaDB data exists without a completion marker"
        start_temporary_server "$data_dir"
        pause_after temporary-server
        verify_database
        stop_temporary_server
        return 0
    fi

    rm -rf -- "$staging_dir"
    install -d -m 0700 -o mysql -g mysql "$staging_dir"
    mariadb-install-db --user=mysql --datadir="$staging_dir" --skip-test-db >/dev/null
    pause_after system-tables

    start_temporary_server "$staging_dir"
    pause_after temporary-server
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

trap - EXIT HUP INT TERM
runtime "$@"
