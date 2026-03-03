#!/bin/bash
# rollback_fix_report.sh — Откат фикса /отчет (восстановление из бэкапа)

VPS="5.42.98.2"
SSH_KEY="$HOME/.ssh/cursor_arkentiy_vps"
PROJECT="/opt/ebidoebi"

echo "⚠️  Откат фикса /отчет..."
echo ""

ssh -i "$SSH_KEY" root@"$VPS" << 'EOF'
  if [ -f /opt/ebidoebi/app/.backup/arkentiy.py.bak.* ]; then
    LATEST_BACKUP=$(ls -t /opt/ebidoebi/app/.backup/arkentiy.py.bak.* | head -1)
    echo "📋 Восстанавливаем из: $LATEST_BACKUP"
    cp "$LATEST_BACKUP" /opt/ebidoebi/app/jobs/arkentiy.py
    cp "${LATEST_BACKUP%arkentiy*}database_pg.py.bak.${LATEST_BACKUP#*bak.}" /opt/ebidoebi/app/database_pg.py 2>/dev/null || echo "Предыдущая версия database_pg.py не найдена"
    
    cd /opt/ebidoebi
    docker compose build --no-cache
    docker compose up -d
    
    echo "✓ Откат завершён"
    sleep 5
    docker compose logs app --tail=10
  else
    echo "❌ Бэкапы не найдены"
  fi
EOF
