# [INTV:ARCH] 베이스 이미지 다이제스트 고정 + 저장소 스냅샷 고정 + apt 캐시 정리 — mariadb Dockerfile과
# 동일한 재현 가능 빌드 패턴을 그대로 반복 (근거는 그쪽 주석 참고).
FROM debian:bookworm-20241202-slim@sha256:1537a6a1cbc4b4fd401da800ee9480207e7dc1f23560c21259f681db56768f63

RUN printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20241214T000000Z bookworm main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20241214T000000Z bookworm-updates main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20241214T000000Z bookworm-security main' \
        > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources

RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        nginx \
        openssl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/nginx/ssl /var/www/html /run/nginx

# [INTV:TRADE_OFF] nginx는 원래 sites-available/sites-enabled로 여러 가상 호스트를 관리하는 게 관례지만,
# 이 스택은 사이트가 하나뿐이라 sites-available/default를 곧바로 서빙 설정으로 써서 심볼릭 링크 단계를 생략했다.
# 가상 호스트가 늘어나면 이 단순화가 발목을 잡으므로, 그 시점엔 표준 구조로 되돌려야 한다.
COPY conf/nginx.conf /etc/nginx/sites-available/default
COPY tools/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# [INTV:ARCH] [INTV:EDGE] 443만 노출 — 이 스택은 평문 HTTP(80)를 아예 열지 않고 TLS만 서빙한다.
# HTTP->HTTPS 리다이렉트조차 두지 않는 강한 선택이라, 80으로 들어온 요청은 애초에 받을 방법이 없다.
EXPOSE 443

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
