FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        mariadb-client \
        mariadb-server \
    && rm -rf /var/lib/apt/lists/*

RUN rm -rf /var/lib/mysql/* \
    && mkdir -p /run/mysqld /var/lib/mysql \
    && chown -R mysql:mysql /run/mysqld /var/lib/mysql

EXPOSE 3306

CMD ["mariadbd", "--user=mysql", "--console"]
