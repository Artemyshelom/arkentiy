#!/bin/bash
# ============================================================
# deploy_shaburov.sh — Деплой онбординга Шабурова на VPS
# Запускать: bash deploy_shaburov.sh
# ============================================================

set -e

VPS="root@5.42.98.2"
SSH_KEY="$HOME/.ssh/cursor_arkentiy_vps"
PROJECT="/opt/ebidoebi"
LOCAL="$HOME/Desktop/CURSOR/Бизнес/02_Проекты/Аркентий"
DATE=$(date +%Y%m%d_%H%M%S)

echo "=== Деплой Шабурова | $DATE ==="

# ШАГ 1: Разведка
echo ""
echo "[1/6] Разведка VPS..."
ssh -i "$SSH_KEY" "$VPS" "
  echo 'Миграции:' && ls $PROJECT/app/migrations/
  echo 'Ветки iiko_credentials:' && docker compose -f $PROJECT/docker-compose.yml exec -T db psql -U postgres -d ebidoebi -c 'SELECT COUNT(*) FROM iiko_credentials;' 2>/dev/null || echo 'БД пока не доступна напрямую'
"

# ШАГ 2: Бэкапы
echo ""
echo "[2/6] Бэкапы изменяемых файлов..."
ssh -i "$SSH_KEY" "$VPS" "
  cp $PROJECT/app/database_pg.py $PROJECT/app/database_pg.py.bak.$DATE
  cp $PROJECT/app/clients/iiko_bo_events.py $PROJECT/app/clients/iiko_bo_events.py.bak.$DATE
  cp $PROJECT/app/jobs/late_alerts.py $PROJECT/app/jobs/late_alerts.py.bak.$DATE
  cp $PROJECT/app/jobs/daily_report.py $PROJECT/app/jobs/daily_report.py.bak.$DATE
  echo 'Бэкапы созданы'
"

# ШАГ 3: Загрузка файлов (только дельта)
echo ""
echo "[3/6] SCP изменённых файлов..."
scp -i "$SSH_KEY" \
  "$LOCAL/app/database_pg.py" \
  "$VPS:$PROJECT/app/database_pg.py"

scp -i "$SSH_KEY" \
  "$LOCAL/app/clients/iiko_bo_events.py" \
  "$VPS:$PROJECT/app/clients/iiko_bo_events.py"

scp -i "$SSH_KEY" \
  "$LOCAL/app/jobs/late_alerts.py" \
  "$VPS:$PROJECT/app/jobs/late_alerts.py"

scp -i "$SSH_KEY" \
  "$LOCAL/app/jobs/daily_report.py" \
  "$VPS:$PROJECT/app/jobs/daily_report.py"

scp -i "$SSH_KEY" \
  "$LOCAL/app/migrations/004_shaburov_onboarding.sql" \
  "$VPS:$PROJECT/app/migrations/004_shaburov_onboarding.sql"

echo "Файлы загружены"

# ШАГ 4: Сборка
echo ""
echo "[4/6] Docker build --no-cache..."
ssh -i "$SSH_KEY" "$VPS" "
  cd $PROJECT && docker compose build --no-cache
"

# ШАГ 5: Запуск + ожидание
echo ""
echo "[5/6] Docker up -d + ожидание 15 сек..."
ssh -i "$SSH_KEY" "$VPS" "
  cd $PROJECT && docker compose up -d
"
sleep 15

# ШАГ 6: Проверка
echo ""
echo "[6/6] Проверка статуса..."
ssh -i "$SSH_KEY" "$VPS" "
  cd $PROJECT
  echo '--- Containers ---'
  docker compose ps
  echo ''
  echo '--- Logs (last 30) ---'
  docker compose logs app --tail=30
"

echo ""
echo "=== Деплой завершён ==="
echo "Проверь:"
echo "  - нет ERROR в логах"
echo "  - контейнер healthy"
echo "  - миграция 004 применена (ищи 'Онбординг завершён: tenant_id=...' в логах)"
