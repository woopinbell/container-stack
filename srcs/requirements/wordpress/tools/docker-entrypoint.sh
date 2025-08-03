#!/bin/sh
set -eu

wordpress_dir="${WORDPRESS_DATA_DIR:-/var/www/html}"
config_dir="${WORDPRESS_CONFIG_DIR:-/var/www/config}"
config_path="${config_dir}/wp-config.php"
config_link="${wordpress_dir}/wp-config.php"
marker="${wordpress_dir}/.container-stack-initialized"
wait_retries="${WORDPRESS_DB_WAIT_RETRIES:-60}"
wait_delay="${WORDPRESS_DB_WAIT_DELAY:-2}"
db_option_file=""

fail() {
    echo "$*" >&2
    exit 1
}

require_name() {
    case "$2" in
        *[!A-Za-z0-9_]*|"") fail "$1 has an invalid format" ;;
    esac
}

require_password() {
    case "$2" in
        *[!A-Za-z0-9_.~!@#%^+=,-]*|"") fail "$1 has an invalid format" ;;
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

require_runtime_value() {
    case "$2" in
        ""|*[!A-Za-z0-9._:/-]*) fail "$1 has an invalid format" ;;
    esac
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
    [ -z "$db_option_file" ] || rm -f -- "$db_option_file"
}

wait_for_database() {
    remaining="$wait_retries"
    while [ "$remaining" -gt 0 ]; do
        if mariadb --defaults-extra-file="$db_option_file" "$MYSQL_DATABASE" \
            --batch --skip-column-names --execute='SELECT 1' >/dev/null 2>&1; then
            return 0
        fi
        remaining=$((remaining - 1))
        [ "$remaining" -gt 0 ] || fail "timed out waiting for authenticated MariaDB access"
        sleep "$wait_delay"
    done
}

install_core_files() {
    if find "$wordpress_dir" -path "${wordpress_dir}/wp-content" -prune \
        -o -path "$config_link" -prune \
        -o -type l -print | grep -q .; then
        fail "WordPress core path contains a symbolic link"
    fi
    if [ ! -f "${wordpress_dir}/wp-includes/version.php" ]; then
        wp core download --allow-root --path="$wordpress_dir"
    fi
    [ -f "${wordpress_dir}/wp-includes/version.php" ] \
        || fail "WordPress core files are incomplete"
}

install_content_files() {
    :
}

publish_config_link() {
    temporary="${wordpress_dir}/.wp-config-link.$$"
    rm -f -- "$temporary"
    ln -s "$config_path" "$temporary"
    mv -f -- "$temporary" "$config_link"
    sync -f "$wordpress_dir"
}

prepare_config_location() {
    install -d -m 0700 -o www-data -g www-data "$config_dir"
    if [ -L "$config_link" ]; then
        [ "$(readlink "$config_link")" = "$config_path" ] \
            || fail "WordPress configuration link has an unexpected target"
        if [ -e "$config_path" ]; then
            [ -f "$config_path" ] && [ ! -L "$config_path" ] \
                || fail "WordPress configuration is not a regular file"
        fi
        return 0
    fi

    if [ -e "$config_link" ]; then
        [ -f "$config_link" ] \
            || fail "WordPress configuration path is not a regular file"
        if [ -e "$config_path" ]; then
            [ -f "$config_path" ] && [ ! -L "$config_path" ] \
                || fail "WordPress configuration is not a regular file"
            cmp -s "$config_link" "$config_path" \
                || fail "WordPress configuration locations disagree"
        else
            temporary="${config_dir}/.wp-config.migrate.$$"
            rm -f -- "$temporary"
            cp -p -- "$config_link" "$temporary"
            chmod 0600 "$temporary"
            chown www-data:www-data "$temporary"
            sync -f "$temporary"
            mv -- "$temporary" "$config_path"
            sync -f "$config_dir"
        fi
        publish_config_link
        return 0
    fi

    if [ -e "$config_path" ]; then
        [ -f "$config_path" ] && [ ! -L "$config_path" ] \
            || fail "WordPress configuration is not a regular file"
        publish_config_link
    fi
}

write_wordpress_config() {
    target="$config_path"
    temporary="${config_dir}/.wp-config.bootstrap.$$"
    salts="$(od -An -N128 -tx1 /dev/urandom | tr -d ' \n')"
    (
        umask 077
        {
            printf '%s\n' '<?php'
            printf "define('DB_NAME', '%s');\n" "$MYSQL_DATABASE"
            printf "define('DB_USER', '%s');\n" "$MYSQL_USER"
            printf "define('DB_PASSWORD', '%s');\n" "$db_password"
            printf "define('DB_HOST', '%s');\n" "$WORDPRESS_DB_HOST"
            printf '%s\n' \
                "define('DB_CHARSET', 'utf8mb4');" \
                "define('DB_COLLATE', '');" \
                "define('AUTH_KEY', '${salts}01');" \
                "define('SECURE_AUTH_KEY', '${salts}02');" \
                "define('LOGGED_IN_KEY', '${salts}03');" \
                "define('NONCE_KEY', '${salts}04');" \
                "define('AUTH_SALT', '${salts}05');" \
                "define('SECURE_AUTH_SALT', '${salts}06');" \
                "define('LOGGED_IN_SALT', '${salts}07');" \
                "define('NONCE_SALT', '${salts}08');" \
                "define('FS_METHOD', 'direct');"
            printf "define('WP_HOME', '%s');\n" "$WORDPRESS_URL"
            printf "define('WP_SITEURL', '%s');\n" "$WORDPRESS_URL"
            printf '%s\n' \
                "\$table_prefix = 'wp_';" \
                "if (!defined('ABSPATH')) { define('ABSPATH', __DIR__ . '/'); }" \
                "require_once ABSPATH . 'wp-settings.php';"
        } >"$temporary"
    )
    chown www-data:www-data "$temporary"
    sync -f "$temporary"
    mv -f -- "$temporary" "$target"
    sync -f "$config_dir"
    publish_config_link
}

config_value() {
    name="$1"
    kind="${2:-constant}"
    wp config get "$name" --allow-root --path="$wordpress_dir" --type="$kind" 2>/dev/null
}

validate_wordpress_config() {
    target="$config_path"
    [ -L "$config_link" ] || return 1
    [ "$(readlink "$config_link")" = "$config_path" ] || return 1
    [ -f "$target" ] && [ ! -L "$target" ] || return 1
    php -l "$target" >/dev/null 2>&1 || return 1
    actual_db_name="$(config_value DB_NAME)" || return 1
    actual_db_user="$(config_value DB_USER)" || return 1
    actual_db_password="$(config_value DB_PASSWORD)" || return 1
    actual_db_host="$(config_value DB_HOST)" || return 1
    actual_table_prefix="$(config_value table_prefix variable)" || return 1
    [ -n "$actual_table_prefix" ] || return 1
    [ "$actual_db_name" = "$MYSQL_DATABASE" ] || return 2
    [ "$actual_db_user" = "$MYSQL_USER" ] || return 2
    [ "$actual_db_password" = "$db_password" ] || return 2
    [ "$actual_db_host" = "$WORDPRESS_DB_HOST" ] || return 2
}

update_config_urls() {
    updater=/run/container-stack-update-config.php
    cat >"$updater" <<'PHP'
<?php
$path = getenv('CONTAINER_STACK_CONFIG_PATH');
$url = getenv('CONTAINER_STACK_WORDPRESS_URL');
$text = file_get_contents($path);
if ($text === false || is_link($path) || !is_file($path)) {
    fwrite(STDERR, "WordPress configuration read failed\n");
    exit(1);
}
foreach (['WP_HOME', 'WP_SITEURL'] as $name) {
    $pattern = "/define\\(\\s*['\"]" . preg_quote($name, '/') . "['\"]\\s*,\\s*.*?\\);/";
    $replacement = "define('" . $name . "', " . var_export($url, true) . ");";
    $text = preg_replace($pattern, $replacement, $text, 1, $count);
    if ($text === null || $count !== 1) {
        fwrite(STDERR, "WordPress URL setting is missing: " . $name . "\n");
        exit(1);
    }
}
umask(0077);
$temporary = tempnam(dirname($path), '.wp-config.url.');
if ($temporary === false) {
    fwrite(STDERR, "WordPress configuration temporary file failed\n");
    exit(1);
}
$published = false;
try {
    $written = file_put_contents($temporary, $text, LOCK_EX);
    if ($written !== strlen($text)) {
        throw new RuntimeException('WordPress configuration write failed');
    }
    if (!chmod($temporary, 0600)
        || !chown($temporary, fileowner($path))
        || !chgrp($temporary, filegroup($path))) {
        throw new RuntimeException('WordPress configuration ownership failed');
    }
    $handle = fopen($temporary, 'rb');
    if ($handle === false) {
        throw new RuntimeException('WordPress configuration reopen failed');
    }
    try {
        if (function_exists('fsync') && !fsync($handle)) {
            throw new RuntimeException('WordPress configuration fsync failed');
        }
    } finally {
        fclose($handle);
    }
    if (!rename($temporary, $path)) {
        throw new RuntimeException('WordPress configuration publish failed');
    }
    $published = true;
} finally {
    if (!$published) {
        @unlink($temporary);
    }
}
PHP
    chmod 0600 "$updater"
    if ! CONTAINER_STACK_CONFIG_PATH="$config_path" \
        CONTAINER_STACK_WORDPRESS_URL="$WORDPRESS_URL" \
        php "$updater"; then
        rm -f -- "$updater"
        fail "WordPress URL configuration update failed"
    fi
    rm -f -- "$updater"
}

converge_wordpress_config() {
    prepare_config_location
    config_status=0
    validate_wordpress_config || config_status="$?"
    case "$config_status" in
        0)
            ;;
        1)
            if [ -f "$marker" ]; then
                fail "completed WordPress configuration is invalid"
            fi
            write_wordpress_config
            validate_wordpress_config \
                || fail "generated WordPress configuration is invalid"
            ;;
        2)
            fail "WordPress database credentials differ; use the secret rotation command"
            ;;
        *)
            fail "cannot validate WordPress configuration"
            ;;
    esac
    update_config_urls
    chmod 0600 "$config_path"
    chown www-data:www-data "$config_path"
    sync -f "$config_path"
    sync -f "$config_dir"
}

install_wordpress() {
    if wp core is-installed --allow-root --path="$wordpress_dir" >/dev/null 2>&1; then
        return 0
    fi
    command_log="$(mktemp /run/wp-core-install.XXXXXX)"
    chmod 0600 "$command_log"
    if ! printf '%s\n' "$admin_password" \
        | wp core install --allow-root --path="$wordpress_dir" \
            --url="$WORDPRESS_URL" \
            --title="$WORDPRESS_TITLE" \
            --admin_user="$WORDPRESS_ADMIN_USER" \
            --admin_email="$WORDPRESS_ADMIN_EMAIL" \
            --prompt=admin_password \
            --skip-email >"$command_log" 2>&1; then
        rm -f -- "$command_log"
        fail "WordPress core installation failed"
    fi
    rm -f -- "$command_log"
}

ensure_author() {
    if wp user get "$WORDPRESS_USER" --allow-root --path="$wordpress_dir" >/dev/null 2>&1; then
        return 0
    fi
    command_log="$(mktemp /run/wp-user-create.XXXXXX)"
    chmod 0600 "$command_log"
    if ! printf '%s\n' "$user_password" \
        | wp user create "$WORDPRESS_USER" "$WORDPRESS_USER_EMAIL" \
            --allow-root --path="$wordpress_dir" --role=author \
            --prompt=user_pass >"$command_log" 2>&1; then
        rm -f -- "$command_log"
        fail "WordPress author creation failed"
    fi
    rm -f -- "$command_log"
}

verify_user_password() {
    login="$1"
    password="$2"
    verifier=/run/container-stack-verify-password.php
    cat >"$verifier" <<'PHP'
<?php
$password = rtrim(stream_get_contents(STDIN), "\r\n");
$login = getenv('CONTAINER_STACK_VERIFY_USER');
$account = get_user_by('login', $login);
if (!$account || !wp_check_password($password, $account->user_pass, $account->ID)) {
    exit(1);
}
PHP
    chmod 0600 "$verifier"
    if ! printf '%s\n' "$password" \
        | CONTAINER_STACK_VERIFY_USER="$login" \
            wp eval-file "$verifier" --allow-root --path="$wordpress_dir" >/dev/null; then
        rm -f -- "$verifier"
        fail "WordPress account password verification failed: $login"
    fi
    rm -f -- "$verifier"
}

runtime() {
    [ -f "${wordpress_dir}/wp-includes/version.php" ] \
        || fail "WordPress core is not bootstrapped; run tools/start_stack.py"
    [ -L "$config_link" ] \
        && [ "$(readlink "$config_link")" = "$config_path" ] \
        && [ -f "$config_path" ] && [ ! -L "$config_path" ] \
        || fail "WordPress configuration is missing or exposed in the web volume"
    [ -f "$marker" ] && [ ! -L "$marker" ] \
        || fail "WordPress completion marker is missing; rerun bootstrap"
    install -d -m 0755 -o www-data -g www-data /run/php
    exec "$@"
}

bootstrap() {
    : "${WORDPRESS_DB_HOST:=mariadb}"
    : "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
    : "${MYSQL_USER:?MYSQL_USER is required}"
    : "${DOMAIN_NAME:?DOMAIN_NAME is required}"
    : "${WORDPRESS_URL:?WORDPRESS_URL is required}"
    : "${WORDPRESS_TITLE:?WORDPRESS_TITLE is required}"
    : "${WORDPRESS_ADMIN_USER:?WORDPRESS_ADMIN_USER is required}"
    : "${WORDPRESS_ADMIN_EMAIL:?WORDPRESS_ADMIN_EMAIL is required}"
    : "${WORDPRESS_USER:?WORDPRESS_USER is required}"
    : "${WORDPRESS_USER_EMAIL:?WORDPRESS_USER_EMAIL is required}"
    require_name MYSQL_DATABASE "$MYSQL_DATABASE"
    require_name MYSQL_USER "$MYSQL_USER"
    require_name WORDPRESS_ADMIN_USER "$WORDPRESS_ADMIN_USER"
    require_name WORDPRESS_USER "$WORDPRESS_USER"
    require_runtime_value WORDPRESS_DB_HOST "$WORDPRESS_DB_HOST"
    require_runtime_value WORDPRESS_URL "$WORDPRESS_URL"
    case "$WORDPRESS_URL" in
        https://*) ;;
        *) fail "WORDPRESS_URL must use https" ;;
    esac
    require_positive_integer WORDPRESS_DB_WAIT_RETRIES "$wait_retries"
    require_positive_integer WORDPRESS_DB_WAIT_DELAY "$wait_delay"

    IFS= read -r db_password || fail "missing database password on standard input"
    IFS= read -r admin_password || fail "missing administrator password on standard input"
    IFS= read -r user_password || fail "missing author password on standard input"
    if IFS= read -r _unexpected; then
        fail "unexpected extra bootstrap input"
    fi
    require_password WORDPRESS_DB_PASSWORD "$db_password"
    require_password WORDPRESS_ADMIN_PASSWORD "$admin_password"
    require_password WORDPRESS_USER_PASSWORD "$user_password"

    install -d -m 0755 -o www-data -g www-data /run/php "$wordpress_dir"
    install -d -m 0700 -o www-data -g www-data "$config_dir"
    db_option_file="$(mktemp /run/db-client.XXXXXX)"
    chmod 0600 "$db_option_file"
    printf '[client]\nhost=%s\nuser=%s\npassword="%s"\n' \
        "$WORDPRESS_DB_HOST" "$MYSQL_USER" "$db_password" >"$db_option_file"
    wait_for_database

    install_core_files
    install_content_files
    pause_after core-files
    converge_wordpress_config
    pause_after wordpress-config
    install_wordpress
    pause_after wordpress-core
    ensure_author
    pause_after wordpress-users

    wp option update home "$WORDPRESS_URL" --allow-root --path="$wordpress_dir" >/dev/null
    wp option update siteurl "$WORDPRESS_URL" --allow-root --path="$wordpress_dir" >/dev/null
    wp user get "$WORDPRESS_ADMIN_USER" --allow-root --path="$wordpress_dir" >/dev/null
    wp user get "$WORDPRESS_USER" --allow-root --path="$wordpress_dir" >/dev/null
    verify_user_password "$WORDPRESS_ADMIN_USER" "$admin_password"
    verify_user_password "$WORDPRESS_USER" "$user_password"

    marker_tmp="${wordpress_dir}/.container-stack-initialized.tmp.$$"
    (umask 077; : >"$marker_tmp")
    chown www-data:www-data "$marker_tmp"
    sync -f "$marker_tmp"
    mv -f -- "$marker_tmp" "$marker"
    sync -f "$wordpress_dir"
    pause_after wordpress-marker
    chown -R www-data:www-data "$wordpress_dir" "$config_dir" /run/php
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
