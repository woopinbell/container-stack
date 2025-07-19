#!/bin/sh
set -eu

file_env() {
    var="$1"
    file_var="${var}_FILE"
    value="${2:-}"
    eval current="\${$var:-}"
    eval file_path="\${$file_var:-}"

    if [ -n "$current" ] && [ -n "$file_path" ]; then
        echo "$var and $file_var are mutually exclusive" >&2
        exit 1
    fi
    if [ -n "$file_path" ]; then
        value="$(cat "$file_path")"
    elif [ -n "$current" ]; then
        value="$current"
    fi
    export "$var=$value"
    unset "$file_var"
}

require_name() {
    case "$2" in
        *[!A-Za-z0-9_]*|"")
            echo "$1 must contain only letters, numbers, and underscores" >&2
            exit 1
            ;;
    esac
}

sql_escape() {
    printf "%s" "$1" | sed "s/'/''/g"
}

file_env MYSQL_ROOT_PASSWORD
file_env MYSQL_PASSWORD

: "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
: "${MYSQL_USER:?MYSQL_USER is required}"
: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is required}"

require_name MYSQL_DATABASE "$MYSQL_DATABASE"
require_name MYSQL_USER "$MYSQL_USER"

install -d -m 0755 -o mysql -g mysql /run/mysqld /var/lib/mysql

if [ ! -d /var/lib/mysql/mysql ]; then
    mariadb-install-db --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null

    mariadbd --user=mysql --datadir=/var/lib/mysql --skip-networking \
        --socket=/run/mysqld/mysqld.sock &
    pid="$!"

    for _ in $(seq 1 60); do
        if mysqladmin --socket=/run/mysqld/mysqld.sock ping --silent; then
            break
        fi
        sleep 1
    done

    root_password="$(sql_escape "$MYSQL_ROOT_PASSWORD")"
    user_password="$(sql_escape "$MYSQL_PASSWORD")"

    mariadb --socket=/run/mysqld/mysqld.sock <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${root_password}';
DELETE FROM mysql.user WHERE User='';
DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost');
DROP DATABASE IF EXISTS test;
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'%' IDENTIFIED BY '${user_password}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL

    mysqladmin --socket=/run/mysqld/mysqld.sock -uroot -p"$MYSQL_ROOT_PASSWORD" shutdown
    wait "$pid"
fi

exec "$@"
