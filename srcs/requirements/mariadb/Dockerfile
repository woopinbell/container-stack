# [INTV:ARCH] [INTV:TRADE_OFF] 태그가 아니라 @sha256 다이제스트로 베이스 이미지를 고정 — 같은 태그라도
# 나중에 다른 이미지로 바뀔 수 있어, 다이제스트로 못박아야 "언제 빌드해도 동일한 베이스"가 보장된다.
# 대신 베이스 이미지의 보안 패치가 자동으로 들어오지 않으므로, 다이제스트 갱신을 별도 프로세스로 관리해야 한다.
FROM debian:bookworm-20241202-slim@sha256:1537a6a1cbc4b4fd401da800ee9480207e7dc1f23560c21259f681db56768f63

# [INTV:ARCH] 기본 sources.list 대신 snapshot.debian.org의 특정 시각 저장소로 교체해 패키지 버전까지 고정
# (재현 가능한 빌드). rm으로 지우는 debian.sources는 최신 Debian 이미지의 기본 저장소 목록(deb822 형식)이라
# 두 목록이 공존하면 충돌한다.
# - [TRAP] Check-Valid-Until=false 없이 스냅샷 저장소를 쓰면, 스냅샷 시점이 오래될수록 Release 파일이
#   "만료됨"으로 판정되어 apt update 자체가 실패한다.
RUN printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20241214T000000Z bookworm main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20241214T000000Z bookworm-updates main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20241214T000000Z bookworm-security main' \
        > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources

# [INTV:PERF] --no-install-recommends로 필수 의존성만 설치해 이미지 용량을 줄이고, apt 캐시 삭제(rm -rf)를
# 반드시 같은 RUN 안에서 실행한다.
# - [TRAP] 캐시 삭제를 별도 RUN으로 분리하면 Docker 레이어 특성상 이전 레이어에 캐시가 이미 구워져 최종
#   이미지 크기에서 전혀 줄어들지 않는다. "설치 + 캐시 삭제"는 반드시 한 RUN(한 레이어)에 묶을 것.
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        mariadb-client \
        mariadb-server \
    && rm -rf /var/lib/apt/lists/*

# [INTV:ARCH] 패키지가 만들어둔 기본 데이터 디렉터리는 지운다 — 실제 데이터는 Compose가 마운트하는 볼륨
# (mysql-volume)에 저장되므로 이미지 안에 남겨둘 필요가 없다. mysql 사용자(서버가 저권한으로 전환하는 계정)
# 에게 디렉터리 소유권을 미리 부여해 entrypoint에서 권한 문제가 나지 않게 한다.
RUN rm -rf /var/lib/mysql \
    && mkdir -p /run/mysqld /var/lib/mysql-volume \
    && chown -R mysql:mysql /run/mysqld /var/lib/mysql-volume

COPY conf/50-server.cnf /etc/mysql/mariadb.conf.d/50-server.cnf
COPY tools/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# [INTV:TRAP] EXPOSE는 실제로 포트를 열어주지 않는다 — "이 컨테이너는 이 포트를 쓴다"는 문서화 + 같은
# 네트워크의 다른 서비스가 서비스명으로 참조할 수 있게 하는 선언일 뿐이다. 실제 호스트 노출은
# docker-compose.yml의 ports 설정이 결정한다.
EXPOSE 3306

# [INTV:ARCH] ENTRYPOINT는 컨테이너 시작 시 항상 실행되는 진입점, CMD는 그 진입점에 전달되는 기본 인자.
# - [TRAP] docker run으로 CMD 부분만 덮어써도 entrypoint 스크립트(부트스트랩/검증 로직)는 항상 거치게
#   설계해야 한다. ENTRYPOINT 자체를 덮어쓰는 식으로 재구현하면 초기화/검증 로직을 우회할 길이 생긴다.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["mariadbd", "--user=mysql", "--console"]
