#!/bin/sh
# [INTV:ARCH] 이 스크립트도 mariadb entrypoint와 같은 이중 역할 구조다 — "bootstrap" 인자로 한 번
# 호출되어 워드프레스 파일 설치·DB 계정 생성·wp-config 작성까지 마치고, 평소 기동 시에는 상태만 점검한
# 뒤 실제 php-fpm으로 넘어간다(맨 아래 분기 참고). 공용 유틸리티(fail/require_*/pause_after/exec 패턴)는
# mariadb entrypoint와 동일하므로 그쪽 주석을 참고하고, 여기서는 워드프레스 쪽 고유 로직만 짚는다.
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

# [INTV:ARCH] 워드프레스 "코어" 파일(wp-content 제외 전부)을 이미지 안에 미리 담아둔 원본
# (/usr/src/wordpress)에서 실제 웹 루트로 설치/검증하는 함수. wp-content(테마/플러그인/업로드)는
# 사용자가 바꾸는 영역이라 별도 함수(install_content_files)에서 정반대 전략으로 다룬다 — 코어는
# "체크섬으로 무결성을 강제하는 불변 파일", wp-content는 "있으면 건드리지 않는 사용자 데이터".
install_core_files() {
    # [INTV:EDGE] find의 -prune으로 wp-content와 wp-config 심볼릭 링크 경로는 검사에서 제외하고, 그
    # 나머지 범위에서 심볼릭 링크가 하나라도 있으면 실패시킨다 — 코어 파일 자리에 심볼릭 링크를 심어
    # 엉뚱한 곳에 쓰게 만드는 공격(symlink attack)을 막기 위한 방어 코드.
    if find "$wordpress_dir" -path "${wordpress_dir}/wp-content" -prune \
        -o -path "$config_link" -prune \
        -o -type l -print | grep -q .; then
        fail "WordPress core path contains a symbolic link"
    fi
    while IFS= read -r manifest_line; do
        # 체크섬 목록의 각 줄은 "다이제스트  ./경로" 형식(sha256sum 출력 포맷) — 앞부분/뒷부분을
        # "${var%% 패턴}"(뒤에서부터 최長 매칭 제거) / "${var#패턴}"(앞에서부터 매칭 제거)로 분리.
        digest="${manifest_line%% *}"
        relative="${manifest_line#*  ./}"
        # [INTV:EDGE] 목록에 담긴 경로가 절대경로거나 ".."로 상위 디렉터리를 벗어나려 하면 거부 —
        # 경로 조작(path traversal) 방어.
        case "$relative" in
            ""|/*|../*|*/../*|*/..) fail "invalid WordPress core manifest path" ;;
        esac
        source="/usr/src/wordpress/${relative}"
        target="${wordpress_dir}/${relative}"
        parent="${target%/*}"
        install -d -m 0755 -o www-data -g www-data "$parent"
        if [ -L "$target" ]; then
            fail "WordPress core target is a symbolic link: $relative"
        fi
        # [INTV:PERF] 이미 같은 체크섬의 파일이 그 자리에 있다면 다시 복사하지 않고 건너뜀 —
        # 재부트스트랩(컨테이너 재기동)을 반복해도 매번 전체를 다시 쓰지 않는 멱등성(idempotency) 확보.
        if [ -f "$target" ] \
            && printf '%s  %s\n' "$digest" "$target" | sha256sum -c - >/dev/null 2>&1; then
            continue
        fi
        if [ -e "$target" ] && [ ! -f "$target" ]; then
            fail "WordPress core target is not a regular file: $relative"
        fi
        base="${target##*/}"
        # [INTV:EDGE] 같은 디렉터리 안에 임시 이름으로 먼저 쓰고 마지막에 mv로 원자적 치환 — 파일이
        # 절반만 쓰인 상태로 다른 프로세스(php-fpm)에 보이는 순간이 생기지 않게 하는 원자적 발행
        # 패턴(mariadb entrypoint의 스테이징-후-mv와 동일한 원칙).
        temporary="${parent}/.${base}.bootstrap.$$"
        rm -f -- "$temporary"
        cp -p -- "$source" "$temporary"
        chown www-data:www-data "$temporary"
        sync -f "$temporary"
        mv -f -- "$temporary" "$target"
        sync -f "$parent"
    done < /usr/src/wordpress-core.sha256
    (
        cd "$wordpress_dir"
        sha256sum -c /usr/src/wordpress-core.sha256 >/dev/null
    ) || fail "WordPress core checksum verification failed"
    [ -f "${wordpress_dir}/wp-includes/version.php" ] \
        || fail "WordPress core files are incomplete"
}

# [INTV:TRADE_OFF] wp-content는 코어와 달리 체크섬 검증을 하지 않는다 — 사용자가 테마/플러그인/업로드를
# 자유롭게 바꾸는 영역이라, "이미 있으면 절대 덮어쓰지 않고, 없는 것만 채워 넣는다"는 정반대 전략을 쓴다.
install_content_files() {
    source_root=/usr/src/wordpress/wp-content
    target_root="${wordpress_dir}/wp-content"
    if [ -L "$target_root" ]; then
        fail "WordPress content directory must not be a symbolic link"
    fi
    install -d -m 0755 -o www-data -g www-data "$target_root"
    (
        cd "$source_root"
        find . -mindepth 1 -type d -print | sort
    ) | while IFS= read -r relative; do
        target="${target_root}/${relative#./}"
        if [ -L "$target" ] || { [ -e "$target" ] && [ ! -d "$target" ]; }; then
            fail "WordPress content path is not a regular directory: $relative"
        fi
        install -d -m 0755 -o www-data -g www-data "$target"
    done
    (
        cd "$source_root"
        find . -type f -print | sort
    ) | while IFS= read -r relative; do
        source="${source_root}/${relative#./}"
        target="${target_root}/${relative#./}"
        if [ -e "$target" ] || [ -L "$target" ]; then
            continue
        fi
        parent="${target%/*}"
        base="${target##*/}"
        temporary="${parent}/.${base}.bootstrap.$$"
        cp -p -- "$source" "$temporary"
        chown www-data:www-data "$temporary"
        sync -f "$temporary"
        mv -- "$temporary" "$target"
        sync -f "$parent"
    done
}

publish_config_link() {
    # [INTV:EDGE] 심볼릭 링크 자체도 임시 이름으로 만든 뒤 mv로 원자적으로 교체 — wp-config.php 경로가
    # "링크가 없는 상태"에서 "잘못된 대상을 가리키는 상태"로 잠깐이라도 노출되는 걸 피한다.
    temporary="${wordpress_dir}/.wp-config-link.$$"
    rm -f -- "$temporary"
    ln -s "$config_path" "$temporary"
    mv -f -- "$temporary" "$config_link"
    sync -f "$wordpress_dir"
}

# [INTV:ARCH] 아키텍처 핵심: wp-config.php의 실제 내용은 웹 루트(wordpress_dir)가 아니라 별도 볼륨
# (config_dir)에 저장하고, 웹 루트에는 그곳을 가리키는 심볼릭 링크만 둔다. wordpress_dir는 nginx에도
# 읽기 전용으로 마운트되는 공유 볼륨이라, DB 비밀번호와 인증 salt가 담긴 실제 설정 파일을 그 안에 직접
# 두면 유출 표면이 넓어지기 때문이다.
# - [TRAP] 이 함수는 과거 버전(링크 없이 파일을 직접 두던 방식)과의 마이그레이션 호환까지 함께
#   처리한다 — 재구현 시 "이미 심볼릭 링크인 경우"만 다루고 "예전 레이아웃의 일반 파일이 남아있는
#   경우"를 놓치면, 기존 배포를 업그레이드할 때 설정이 유실되거나 두 곳의 내용이 갈라질 수 있다.
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
        # 링크가 아니라 실제 파일이 웹 루트에 남아있는 경우 — 예전 레이아웃에서 마이그레이션하는 경로.
        # 새 위치(config_path)로 옮겨두고 원래 자리는 링크로 대체한다.
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
    # [INTV:PERF] /dev/urandom에서 128바이트를 한 번 읽어 16진수 문자열로 바꾼 뒤, 접미사(01~08)만
    # 바꿔가며 8개의 서로 다른 인증 salt 값으로 잘라 쓴다 — 난수 소스를 여덟 번 따로 읽는 대신 한 번의
    # 읽기로 충분히 긴 값을 얻어 값마다 구분자를 붙이는 방식.
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
                "define('WP_AUTO_UPDATE_CORE', false);" \
                "define('AUTOMATIC_UPDATER_DISABLED', true);" \
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

# [INTV:ARCH] 반환값에 세 가지 뜻을 담는 작은 상태 코드 — 아래 converge_wordpress_config()의 case
# 문에서 그대로 분기 근거로 쓰인다: 0 = 설정이 존재하고 지금 넘어온 값들과 일치, 1 = 설정 자체가 없음
# (처음 부트스트랩), 2 = 설정은 있지만 값이 다름(충돌).
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

# [INTV:ARCH] WORDPRESS_URL(도메인)이 바뀌면 이미 부트스트랩이 끝난 사이트라도 매번 WP_HOME/
# WP_SITEURL을 최신값으로 맞춰야 하므로, 이 함수는 converge_wordpress_config()에서 분기와 무관하게
# 항상 호출된다. wp-cli 대신 직접 만든 작은 PHP 스크립트로 처리하는 이유는 파일을 원자적으로
# 교체(rename)하는 절차까지 이 스크립트 하나에서 함께 제어하기 위함이다.
update_config_urls() {
    updater=/run/container-stack-update-config.php
    # [INTV:TRAP] <<'PHP' — 구분자를 따옴표로 감싼 heredoc은 안의 $변수를 셸이 치환하지 않고 그대로
    # 파일에 쓴다(앞서 mariadb entrypoint의 따옴표 없는 <<SQL과 반대). PHP 자체 변수($path 등)를 셸이
    # 건드리면 안 되므로 필수 — 따옴표를 빼먹으면 PHP 소스 안의 $path, $url 등이 셸에 의해 먼저
    # (대개 빈 문자열로) 치환되어버려 완전히 다른 스크립트가 파일에 쓰인다.
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
            # [INTV:EDGE] 마커가 이미 있다는 건 "예전에 부트스트랩이 끝났다"는 뜻인데 설정이 없다면
            # 정상 상태가 아니므로, 새로 만들지 않고 실패시켜 조용히 새 설정으로 갈아치우는 사고를 막는다.
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
    # [INTV:EDGE] --prompt=admin_password로 값을 표준입력에서 받는다 — mariadb entrypoint와 같은
    # 이유로, 비밀번호를 커맨드라인 인자로 남기지 않기 위함.
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

# [INTV:EDGE] 계정을 "만들었다"는 것과 "지정한 비밀번호로 실제 로그인 가능하다"는 것은 다른 문제라,
# wp-cli의 내부 검증 함수인 wp_check_password()를 워드프레스 실행 컨텍스트 안에서 직접 돌려 확인한다.
# 이 역시 후보 비밀번호를 커맨드라인이 아니라 stdin으로 넘긴다.
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
    # [INTV:EDGE] 이 스택은 TLS만 서빙하므로(nginx.conf에 80 포트가 없음), 사이트 URL도 https로
    # 강제해서 워드프레스가 내부적으로 http:// 링크를 생성하는 불일치를 원천 차단한다.
    case "$WORDPRESS_URL" in
        https://*) ;;
        *) fail "WORDPRESS_URL must use https" ;;
    esac
    require_positive_integer WORDPRESS_DB_WAIT_RETRIES "$wait_retries"
    require_positive_integer WORDPRESS_DB_WAIT_DELAY "$wait_delay"

    # mariadb entrypoint와 같은 이유로 세 비밀번호(DB/관리자/작성자)를 환경변수가 아니라 표준입력
    # 3줄로 받는다.
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
