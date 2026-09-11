# [INTV:ARCH] 베이스 이미지 다이제스트 고정 + 저장소 스냅샷 고정 — mariadb Dockerfile과 동일한 재현
# 가능 빌드 패턴 (근거는 그쪽 주석 참고).
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
        default-mysql-client \
        libfcgi-bin \
        php8.2-cli \
        php8.2-curl \
        php8.2-fpm \
        php8.2-gd \
        php8.2-intl \
        php8.2-mbstring \
        php8.2-mysql \
        php8.2-xml \
        php8.2-zip \
    && rm -rf /var/lib/apt/lists/*

# [INTV:ARCH] apt 패키지가 아니라 버전을 직접 못박아 내려받는 두 구성요소(wp-cli, 워드프레스 코어) —
# 한 곳(ENV)에서만 버전을 올리면 되게끔 분리해 유지보수 지점을 하나로 모았다.
ENV WP_CLI_VERSION=2.11.0 \
    WORDPRESS_VERSION=6.7.1

# [INTV:EDGE] curl로 받은 파일을 sha256sum -c로 검증하는 공급망(supply-chain) 무결성 검사 — 다운로드가
# 중간에 깨졌거나 저장소가 변조된 파일을 내려줬을 때 빌드를 그 자리에서 실패시킨다.
# - [TRAP] 이 검증을 생략하고 curl 결과를 그대로 신뢰하면, 이미지 빌드 시점의 네트워크 경로(미러/CDN)가
#   손상됐을 때 변조된 바이너리가 조용히 이미지에 들어가는 공급망 공격 표면이 생긴다.
RUN curl -fsSL \
        "https://github.com/wp-cli/wp-cli/releases/download/v${WP_CLI_VERSION}/wp-cli-${WP_CLI_VERSION}.phar" \
        -o /usr/local/bin/wp \
    && echo 'a39021ac809530ea607580dbf93afbc46ba02f86b6cffd03de4b126ca53079f6  /usr/local/bin/wp' \
        | sha256sum -c - \
    && chmod 0755 /usr/local/bin/wp \
    && curl -fsSL "https://wordpress.org/wordpress-${WORDPRESS_VERSION}.tar.gz" \
        -o /tmp/wordpress.tar.gz \
    && echo '33529cd638c845007e8e0d26c91d60c9c16b822c849c8deead03d0c851a26deb  /tmp/wordpress.tar.gz' \
        | sha256sum -c - \
    && mkdir -p /usr/src/wordpress /run/php /var/www/html /var/www/config \
    && tar -xzf /tmp/wordpress.tar.gz --strip-components=1 -C /usr/src/wordpress \
    && rm /tmp/wordpress.tar.gz \
    && cd /usr/src/wordpress \
    # [INTV:ARCH] wp-content(테마/플러그인/업로드처럼 사용자가 바꾸는 영역)를 제외한 "코어 파일"만 모아
    # 체크섬 목록을 빌드 시점에 만들어 둔다 — 이 목록은 entrypoint 스크립트가 컨테이너 기동 시 코어 파일이
    # 볼륨 위에서 손상/변조되지 않았는지 검증·복구하는 데 그대로 쓰인다 (wordpress entrypoint 참고).
    && find . -type f ! -path './wp-content/*' -print0 \
        | sort -z \
        | xargs -0 sha256sum > /usr/src/wordpress-core.sha256 \
    && chown -R www-data:www-data \
        /usr/src/wordpress /run/php /var/www/html /var/www/config

WORKDIR /var/www/html

COPY conf/www.conf /etc/php/8.2/fpm/pool.d/www.conf
COPY tools/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# [INTV:ARCH] php-fpm은 HTTP가 아니라 FastCGI 프로토콜로 통신 — 이 포트는 브라우저가 아니라 nginx가
# fastcgi_pass로 연결하는 내부 전용 용도다.
EXPOSE 9000

ENTRYPOINT ["docker-entrypoint.sh"]
# [INTV:EDGE] -F: php-fpm을 포그라운드로 실행. 컨테이너의 메인 프로세스(PID 1)가 데몬화되어 백그라운드로
# 숨어버리면 안 되기 때문 (PID 1 문제 — 컨테이너 런타임의 정지 신호는 PID 1에게 전달된다).
CMD ["php-fpm8.2", "-F"]
