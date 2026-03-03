#!/bin/bash
# deploy_fix_report.sh — Деплой фикса /отчет для Шабурова (tenant_id=3)
# Использование: bash deploy_fix_report.sh

set -e

VPS="5.42.98.2"
SSH_KEY="$HOME/.ssh/cursor_arkentiy_vps"
PROJECT="/opt/ebidoebi"
LOCAL_APP="app/jobs/arkentiy.py app/database_pg.py"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "=== Деплой фикса /отчет | $TIMESTAMP ==="
echo ""

# ШАГ 1: Разведка
echo "📋 Разведка..."
ssh -i "$SSH_KEY" root@"$VPS" "ls -lah $PROJECT/app/jobs/arkentiy.py $PROJECT/app/database_pg.py && echo '✓ Файлы найдены на VPS'"
echo ""

# ШАГ 2: Бэкап
echo "💾 Создаём бэкапы на VPS..."
ssh -i "$SSH_KEY" root@"$VPS" << 'EOF'
  mkdir -p /opt/ebidoebi/app/.backup
  cp /opt/ebidoebi/app/jobs/arkentiy.py /opt/ebidoebi/app/.backup/arkentiy.py.bak.$TIMESTAMP
  cp /opt/ebidoebi/app/database_pg.py /opt/ebidoebi/app/.backup/database_pg.py.bak.$TIMESTAMP
  echo "✓ Бэкапы созданы"
EOF
echo ""

# ШАГ 3: SCP — только изменённые файлы
echo "📤 Копируем файлы на VPS..."
scp -i "$SSH_KEY" app/jobs/arkentiy.py root@"$VPS":"$PROJECT/app/jobs/arkentiy.py"
scp -i "$SSH_KEY" app/database_pg.py root@"$VPS":"$PROJECT/app/database_pg.py"
echo "✓ Файлы скопированы"
echo ""

# ШАГ 4: Build + up
echo "🔨 Пересобираем контейнер..."
ssh -i "$SSH_KEY" root@"$VPS" << 'EOF'
  cd /opt/ebidoebi
  docker compose build --no-cache
  docker compose up -d
  echo "✓ Контейнер пересобран"
EOF
echo ""

# ШАГ 5: Проверка здоровья
echo "⏳ Ждём 10 сек для инициализации..."
sleep 10
echo ""

echo "🔍 Проверяем состояние контейнеров..."
ssh -i "$SSH_KEY" root@"$VPS" "docker compose ps"
echo ""

# ШАГ 6: Логи
echo "📋 Последние логи (tail -20)..."
ssh -i "$SSH_KEY" root@"$VPS" "docker compose logs app --tail=20" | tail -30
echo ""

echo "✅ Деплой завершён!"
echo ""
echo "Следующие шаги:"
echo "1. Проверь в Telegram: /отчет (в чате Шабурова)"
echo "2. Если есть ошибки в логах выше — откат: bash rollback_fix_report.sh"
