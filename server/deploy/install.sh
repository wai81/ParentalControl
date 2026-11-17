#!/usr/bin/env bash
# Installs or updates the parental control server on a Raspberry Pi (or any Debian/Ubuntu
# with systemd). Run from the unpacked package:  sudo ./server/deploy/install.sh
#
#   /opt/pc-server/app     program files (replaced on every update)
#   /opt/pc-server/venv    Python environment
#   /opt/pc-server/python  Python itself (downloaded by uv)
#   /var/lib/pc-server     data: database, session key, backups (kept on updates)
#   /etc/pc-server.env     settings (kept on updates)
set -euo pipefail

SERVICE_USER=pcserver
PREFIX=/opt/pc-server
DATA=/var/lib/pc-server
ENV_FILE=/etc/pc-server.env

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите через sudo: sudo $0"
command -v systemctl >/dev/null || die "нужен systemd"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)
[[ -f "$SRC/uv.lock" && -d "$SRC/server/src/pc_server" ]] || die "запускайте из распакованного архива pc-server"

case "$(uname -m)" in
  aarch64|x86_64) ;;
  armv7l|armv6l)
    echo "ВНИМАНИЕ: 32-битная система ($(uname -m)). Рекомендуется 64-битная Raspberry Pi OS:"
    echo "часть библиотек для 32 бит придётся собирать, это долго и может не получиться."
    read -r -p "Продолжить? [y/N] " answer
    [[ $answer == [yYдД]* ]] || exit 1 ;;
  *) echo "ВНИМАНИЕ: непроверенная архитектура $(uname -m)" ;;
esac

say "uv (менеджер Python)"
command -v curl >/dev/null || { apt-get update && apt-get install -y curl ca-certificates; }
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

say "Пользователь и папки"
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 755 "$PREFIX"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA"
if [[ ! -f $ENV_FILE ]]; then
  install -o root -g "$SERVICE_USER" -m 640 "$HERE/pc-server.env" "$ENV_FILE"
  echo "Создан $ENV_FILE"
fi
FIRST_INSTALL=0
[[ -f $DATA/server.db ]] || FIRST_INSTALL=1

say "Программа"
rm -rf "$PREFIX/app.new"
mkdir "$PREFIX/app.new"
cp -a "$SRC/." "$PREFIX/app.new/"
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_PYTHON_PREFERENCE=only-managed \
       UV_PROJECT_ENVIRONMENT="$PREFIX/venv" UV_CACHE_DIR=/var/cache/pc-server-uv
# Python 3.13: the version the server is tested with.
(cd "$PREFIX/app.new" && uv sync --frozen --no-dev --no-editable --package pc-server --python 3.13)
chmod -R a+rX "$PREFIX"
rm -rf "$PREFIX/app.old"
[[ -d $PREFIX/app ]] && mv "$PREFIX/app" "$PREFIX/app.old"
mv "$PREFIX/app.new" "$PREFIX/app"
rm -rf "$PREFIX/app.old"

say "Службы systemd"
install -m 644 "$HERE/pc-server.service" "$HERE/pc-server-backup.service" \
  "$HERE/pc-server-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pc-server.service pc-server-backup.timer >/dev/null
systemctl restart pc-server.service
systemctl start pc-server-backup.timer

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
PORT=${PC_SERVER_PORT:-8000}
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null || {
  journalctl -u pc-server -n 30 --no-pager
  die "сервер не отвечает, журнал выше"
}
echo "Сервер работает."

SET_PASSWORD="runuser -u $SERVICE_USER -- env PC_SERVER_DATA_DIR=$DATA $PREFIX/venv/bin/pc_server set-password"
if [[ $FIRST_INSTALL == 1 ]]; then
  say "Пароль родителя для веб-панели (не короче 8 символов)"
  if [[ -t 0 ]]; then
    $SET_PASSWORD
  else
    echo "Не терминал: пароль не задан. Задайте его командой:  sudo $SET_PASSWORD"
  fi
fi

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Готово"
echo "Веб-панель в домашней сети:  http://${LAN_IP:-адрес-pi}:$PORT"
echo "Этот адрес указывайте в 'pcagent connect --server' на компьютерах детей."
if command -v tailscale >/dev/null; then
  echo "Доступ с телефона вне дома (https):  sudo tailscale serve --bg $PORT"
else
  echo "Для доступа с телефона вне дома установите Tailscale (см. server/README.md)."
fi
echo "Сменить пароль:  sudo $SET_PASSWORD"
echo "Журнал сервера:  journalctl -u pc-server -f"
