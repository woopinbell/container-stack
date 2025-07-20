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

file_env WORDPRESS_DB_PASSWORD
file_env WORDPRESS_ADMIN_PASSWORD
file_env WORDPRESS_USER_PASSWORD

: "${WORDPRESS_DB_HOST:=mariadb}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
: "${MYSQL_USER:?MYSQL_USER is required}"
: "${WORDPRESS_DB_PASSWORD:?WORDPRESS_DB_PASSWORD is required}"
: "${DOMAIN_NAME:?DOMAIN_NAME is required}"
: "${WORDPRESS_TITLE:?WORDPRESS_TITLE is required}"
: "${WORDPRESS_ADMIN_USER:?WORDPRESS_ADMIN_USER is required}"
: "${WORDPRESS_ADMIN_PASSWORD:?WORDPRESS_ADMIN_PASSWORD is required}"
: "${WORDPRESS_ADMIN_EMAIL:?WORDPRESS_ADMIN_EMAIL is required}"
: "${WORDPRESS_USER:?WORDPRESS_USER is required}"
: "${WORDPRESS_USER_PASSWORD:?WORDPRESS_USER_PASSWORD is required}"
: "${WORDPRESS_USER_EMAIL:?WORDPRESS_USER_EMAIL is required}"

install -d -m 0755 -o www-data -g www-data /run/php /var/www/html

for _ in $(seq 1 60); do
    if mysqladmin ping -h"$WORDPRESS_DB_HOST" -u"$MYSQL_USER" -p"$WORDPRESS_DB_PASSWORD" --silent; then
        break
    fi
    sleep 2
done

if [ ! -f /var/www/html/wp-includes/version.php ]; then
    wp core download --allow-root --path=/var/www/html
fi

if [ ! -f /var/www/html/wp-config.php ]; then
    wp config create --allow-root \
        --path=/var/www/html \
        --dbname="$MYSQL_DATABASE" \
        --dbuser="$MYSQL_USER" \
        --dbpass="$WORDPRESS_DB_PASSWORD" \
        --dbhost="$WORDPRESS_DB_HOST" \
        --skip-check

    wp config set --allow-root --path=/var/www/html FS_METHOD direct --type=constant
    wp config set --allow-root --path=/var/www/html WP_HOME "https://${DOMAIN_NAME}" --type=constant
    wp config set --allow-root --path=/var/www/html WP_SITEURL "https://${DOMAIN_NAME}" --type=constant
fi

if ! wp core is-installed --allow-root --path=/var/www/html >/dev/null 2>&1; then
    wp core install --allow-root \
        --path=/var/www/html \
        --url="https://${DOMAIN_NAME}" \
        --title="$WORDPRESS_TITLE" \
        --admin_user="$WORDPRESS_ADMIN_USER" \
        --admin_password="$WORDPRESS_ADMIN_PASSWORD" \
        --admin_email="$WORDPRESS_ADMIN_EMAIL" \
        --skip-email
fi

if ! wp user get "$WORDPRESS_USER" --allow-root --path=/var/www/html >/dev/null 2>&1; then
    wp user create "$WORDPRESS_USER" "$WORDPRESS_USER_EMAIL" \
        --allow-root \
        --path=/var/www/html \
        --role=author \
        --user_pass="$WORDPRESS_USER_PASSWORD"
fi

chown -R www-data:www-data /var/www/html /run/php

exec "$@"
