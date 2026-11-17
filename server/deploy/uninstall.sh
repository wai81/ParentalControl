#!/usr/bin/env bash
# Removes the parental control server.  sudo ./server/deploy/uninstall.sh [--purge]
# Without --purge the data (database, backups) and /etc/pc-server.env are kept,
# so a later install.sh continues where it stopped.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Запустите через sudo: sudo $0 [--purge]" >&2; exit 1; }
PURGE=0
[[ ${1:-} == --purge ]] && PURGE=1

for unit in pc-server.service pc-server-backup.timer pc-server-backup.service; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
rm -rf /opt/pc-server /var/cache/pc-server-uv
echo "Сервер удалён: службы и /opt/pc-server."

if [[ $PURGE == 1 ]]; then
  rm -rf /var/lib/pc-server /etc/pc-server.env
  id pcserver >/dev/null 2>&1 && userdel pcserver
  echo "Удалены и данные: /var/lib/pc-server, /etc/pc-server.env, пользователь pcserver."
else
  echo "Данные сохранены в /var/lib/pc-server (база и резервные копии), настройки в /etc/pc-server.env."
  echo "Удалить и их: sudo $0 --purge"
fi
