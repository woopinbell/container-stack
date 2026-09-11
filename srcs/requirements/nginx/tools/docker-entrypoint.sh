#!/bin/sh
set -eu

# [INTV:TRAP] ":" 는 아무 일도 하지 않는 셸 내장 명령(no-op) — 여기서는 실행 자체가 아니라 뒤에 붙은
# "${VAR:=기본값}" 확장의 부작용을 쓰려는 것이다. ":-"와 달리 ":="는 VAR가 비어있을 때 그 값을 실제로
# DOMAIN_NAME 변수에 대입까지 해준다(이후 코드에서 계속 그 기본값을 참조할 수 있게).
# - [TRAP] ":-"로 잘못 쓰면 이 줄에서는 기본값이 출력/평가만 되고 변수에는 반영되지 않아, 아래
#   openssl 호출에서 DOMAIN_NAME이 여전히 빈 채로 쓰인다.
: "${DOMAIN_NAME:=localhost}"

cert_dir=/etc/nginx/ssl
cert_file="${cert_dir}/container-stack.crt"
key_file="${cert_dir}/container-stack.key"

mkdir -p "$cert_dir" /run/nginx

# [INTV:ARCH] [INTV:EDGE] 실제 도메인/CA 없이 자체 서명(self-signed) 인증서로 TLS를 강제 — 인증서가
# 없을 때만 새로 생성해서, 컨테이너를 재시작해도(이미지를 새로 빌드하지 않는 한) 같은 인증서를 계속 쓴다.
# - [TRAP] 이 존재 확인 없이 매 기동마다 새로 생성하면, 재시작할 때마다 인증서 지문이 바뀌어 클라이언트의
#   인증서 캐시/신뢰 예외가 매번 깨진다.
if [ ! -s "$cert_file" ] || [ ! -s "$key_file" ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -subj "/CN=${DOMAIN_NAME}" \
        -keyout "$key_file" \
        -out "$cert_file" >/dev/null 2>&1
fi

# [INTV:EDGE] exec "$@" — 이 셸을 CMD로 받은 명령(nginx)으로 교체한다(자식을 새로 fork하지 않음).
# - [TRAP] exec 없이 그냥 "$@"만 실행하면 이 셸 스크립트가 계속 PID 1로 남아, docker stop이 보내는
#   SIGQUIT/SIGTERM을 nginx가 직접 받지 못하고 그레이스풀 셧다운이 깨진다.
exec "$@"
