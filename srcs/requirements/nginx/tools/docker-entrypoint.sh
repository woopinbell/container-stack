#!/bin/sh
set -eu

: "${DOMAIN_NAME:=localhost}"

cert_dir=/etc/nginx/ssl
cert_file="${cert_dir}/container-stack.crt"
key_file="${cert_dir}/container-stack.key"

mkdir -p "$cert_dir" /run/nginx

if [ ! -s "$cert_file" ] || [ ! -s "$key_file" ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -subj "/CN=${DOMAIN_NAME}" \
        -keyout "$key_file" \
        -out "$cert_file" >/dev/null 2>&1
fi

exec "$@"
