#!/bin/bash
# init-letsencrypt.sh — первичный выпуск SSL через Let's Encrypt
# Запускать ОДИН РАЗ на VPS после настройки DNS.
#
# Использование:
#   chmod +x nginx/init-letsencrypt.sh
#   ./nginx/init-letsencrypt.sh
#
# Предусловие: A-записи уже указывают на этот сервер (важно!).

set -euo pipefail

DOMAINS_ASCII=("arkenty.ru" "www.arkenty.ru")
EMAIL="admin@arkenty.ru"          # ← ваш email для уведомлений Let's Encrypt
STAGING=0                         # 1 = тест без реального сертификата (ставьте 1 при отладке)
DATA_PATH="./certbot"

# IDN домен аркентий.рф — вычисляем punycode для certbot
IDN_UNICODE="аркентий.рф"
IDN_PUNYCODE=$(python3 -c "
parts = '$IDN_UNICODE'.split('.')
encoded = '.'.join(p.encode('idna').decode('ascii') for p in parts)
print(encoded)
" 2>/dev/null)

if [[ -z "$IDN_PUNYCODE" ]]; then
    echo "⚠️  Не удалось вычислить punycode для $IDN_UNICODE — пропускаем этот домен"
    IDN_ENABLED=0
else
    echo "ℹ️  $IDN_UNICODE → $IDN_PUNYCODE"
    IDN_ENABLED=1
    DOMAINS_IDN=("$IDN_UNICODE" "www.аркентий.рф")  # nginx server_name (UTF-8)
    DOMAINS_IDN_PUNYCODE=("$IDN_PUNYCODE" "www.$IDN_PUNYCODE")
fi

mkdir -p "$DATA_PATH/conf" "$DATA_PATH/www"

# 1. Скачиваем recommended nginx SSL параметры от certbot
if [[ ! -e "$DATA_PATH/conf/options-ssl-nginx.conf" ]]; then
    echo "### Скачиваем SSL параметры..."
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
        -o "$DATA_PATH/conf/options-ssl-nginx.conf"
fi

if [[ ! -e "$DATA_PATH/conf/ssl-dhparams.pem" ]]; then
    echo "### Скачиваем DH параметры..."
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem \
        -o "$DATA_PATH/conf/ssl-dhparams.pem"
fi

# 2. Создаём самоподписанные dummy-сертификаты, чтобы nginx стартовал
create_dummy() {
    local domain=$1
    local path="$DATA_PATH/conf/live/$domain"
    if [[ ! -d "$path" ]]; then
        echo "### Создаём dummy-сертификат для $domain..."
        mkdir -p "$path"
        docker compose run --rm --entrypoint openssl certbot \
            req -x509 -nodes -newkey rsa:2048 -days 1 \
            -keyout "$path/privkey.pem" \
            -out   "$path/fullchain.pem" \
            -subj "/CN=localhost" 2>/dev/null
    fi
}

create_dummy "arkenty.ru"
[[ "$IDN_ENABLED" -eq 1 ]] && create_dummy "аркентий.рф"

# 3. Стартуем nginx с dummy-сертификатами
echo "### Запускаем nginx..."
docker compose up -d nginx

# Ждём пока nginx поднимется
echo "### Ожидаем nginx (5 сек)..."
sleep 5

# 4. Удаляем dummy, запрашиваем реальные сертификаты
get_cert() {
    local domains_args=()
    for d in "$@"; do domains_args+=("-d" "$d"); done

    local staging_arg=""
    [[ "$STAGING" -eq 1 ]] && staging_arg="--staging"

    echo "### Запрашиваем сертификат для: $*"
    docker compose run --rm --entrypoint certbot certbot \
        certonly --webroot \
        -w /var/www/certbot \
        "${domains_args[@]}" \
        --email "$EMAIL" \
        --rsa-key-size 4096 \
        --agree-tos \
        --no-eff-email \
        --force-renewal \
        $staging_arg
}

# 4а. Удаляем dummy для arkenty.ru и получаем реальный
echo "### Удаляем dummy-сертификат arkenty.ru..."
rm -rf "$DATA_PATH/conf/live/arkenty.ru"

get_cert "${DOMAINS_ASCII[@]}"

# 4б. Аналогично для IDN домена
if [[ "$IDN_ENABLED" -eq 1 ]]; then
    echo "### Удаляем dummy-сертификат аркентий.рф..."
    rm -rf "$DATA_PATH/conf/live/аркентий.рф"
    # Certbot требует punycode для IDN доменов
    get_cert "${DOMAINS_IDN_PUNYCODE[@]}"
    # Создаём symlink с Unicode-именем, на который ссылается nginx.conf
    ln -sfn "$DATA_PATH/conf/live/$IDN_PUNYCODE" "$DATA_PATH/conf/live/аркентий.рф" 2>/dev/null || true
fi

# 5. Перезагружаем nginx с реальными сертификатами
echo "### Перезагружаем nginx..."
docker compose exec nginx nginx -s reload

echo ""
echo "✅ SSL успешно выпущен!"
echo "   https://arkenty.ru"
[[ "$IDN_ENABLED" -eq 1 ]] && echo "   https://аркентий.рф"
echo ""
echo "Авторенев настроен через службу certbot в docker-compose (timer: 12h)."
