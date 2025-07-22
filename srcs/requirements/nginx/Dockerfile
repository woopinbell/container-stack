FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        nginx \
        openssl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/nginx/ssl /var/www/html /run/nginx

COPY tools/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 443

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
