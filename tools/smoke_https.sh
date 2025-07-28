#!/bin/sh
set -eu

url="${SMOKE_URL:-https://localhost/healthz}"

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for smoke checks" >&2
    exit 1
fi

for _ in $(seq 1 "${SMOKE_RETRIES:-30}"); do
    if curl -kfsS "$url" >/dev/null; then
        echo "https smoke passed: $url"
        exit 0
    fi
    sleep "${SMOKE_DELAY:-2}"
done

echo "https smoke failed: $url" >&2
exit 1
