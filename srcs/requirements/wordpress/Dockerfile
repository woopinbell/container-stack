FROM debian:bookworm-slim

RUN apt-get update \
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
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
        -o /usr/local/bin/wp \
    && chmod +x /usr/local/bin/wp \
    && mkdir -p /run/php /var/www/html \
    && chown -R www-data:www-data /run/php /var/www/html

WORKDIR /var/www/html

EXPOSE 9000

CMD ["php-fpm8.2", "-F"]
