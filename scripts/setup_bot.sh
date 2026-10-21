#!/bin/bash
# One command to set up the private Telegram bot on macOS or Linux (a VPS):
# Python environment, the private data, settings (asked once), a check, and autostart.
#   ./scripts/setup_bot.sh
# Safe to run again: an existing database and settings are kept.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
DATA_REPO="${DATA_REPO:-https://github.com/ilyacamus/moneybutton_data}"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- 1. Python ----------------------------------------------------------------
say "1/5 Окружение Python"
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Нужен Python 3.11 или новее. macOS: brew install python@3.13 · Ubuntu: sudo apt install python3.12 python3.12-venv"
  exit 1
fi
command -v ffmpeg >/dev/null 2>&1 || echo "⚠️  Нет ffmpeg — разбор видео работать не будет (macOS: brew install ffmpeg · Ubuntu: sudo apt install ffmpeg)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[bot]"
ENV_TOOL=(.venv/bin/python scripts/env_value.py .env)

# --- 2. Data ------------------------------------------------------------------
say "2/5 База знаний"
mkdir -p data
if [ -f data/hookbot.db ]; then
  echo "База уже есть (data/hookbot.db) — оставляю как есть: в ней история бота."
else
  if [ -d ../moneybutton_data/.git ]; then
    git -C ../moneybutton_data pull -q || echo "⚠️  Не удалось обновить ../moneybutton_data — беру то, что есть."
  else
    git clone -q "$DATA_REPO" ../moneybutton_data || echo "⚠️  Не удалось скачать приватные данные: войдите в GitHub (gh auth login) и запустите скрипт снова."
  fi
  if [ -f ../moneybutton_data/db/hookbot.db ]; then
    cp ../moneybutton_data/db/hookbot.db data/hookbot.db
    echo "База знаний скопирована: ваш канал, крипто-ниша, обученные модели."
  else
    echo "⚠️  Бот запустится без вашего канала: встроенная модель крипто-ниши всё равно работает."
  fi
fi

# --- 3. Settings --------------------------------------------------------------
say "3/5 Настройки (.env)"
[ -f .env ] || cp .env.example .env
ask() {  # ask KEY "question" [secret]
  if [ -n "$("${ENV_TOOL[@]}" "$1")" ]; then echo "$1 уже задан."; return; fi
  local value=""
  while [ -z "$value" ]; do
    if [ "${3:-}" = secret ]; then read -r -s -p "$2: " value; echo; else read -r -p "$2: " value; fi
  done
  "${ENV_TOOL[@]}" "$1" "$value"
}
default() {  # default KEY VALUE — only when not set yet
  [ -n "$("${ENV_TOOL[@]}" "$1")" ] || "${ENV_TOOL[@]}" "$1" "$2"
}
ask TELEGRAM_BOT_TOKEN "Токен бота от @BotFather (ввод скрыт)" secret
ask ADMIN_IDS "Ваш Telegram id (пришлёт @userinfobot)"
ask ANTHROPIC_API_KEY "Ключ Claude API, console.anthropic.com → API keys (ввод скрыт)" secret
default BOT_PRIVATE 1
default OWNER_NICHE "Крипта и трейдинг"
default OWNER_WORKSPACE kanyuka
default HOOKBOT_DB data/hookbot.db
chmod 600 .env

# --- 4. Check -----------------------------------------------------------------
say "4/5 Проверка"
set -a; . ./.env; set +a
if ! .venv/bin/python -m telegram_bot --check; then
  echo "Исправьте пункты с ❌ в файле .env и запустите скрипт снова."
  exit 1
fi

# --- 5. Autostart -------------------------------------------------------------
say "5/5 Автозапуск"
chmod +x scripts/run_bot.sh
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.moneybutton.bot.plist"
    mkdir -p "$(dirname "$PLIST")"
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    sed "s#REPO#$REPO#" scripts/macos/com.moneybutton.bot.plist > "$PLIST"
    launchctl load "$PLIST"
    STOP="launchctl unload $PLIST"
    ;;
  Linux)
    UNIT="$HOME/.config/systemd/user/moneybutton-bot.service"
    mkdir -p "$(dirname "$UNIT")"
    sed "s#REPO#$REPO#" scripts/linux/moneybutton-bot.service > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now moneybutton-bot.service
    loginctl enable-linger "$USER" 2>/dev/null || echo "⚠️  Чтобы бот работал после выхода из сервера: sudo loginctl enable-linger $USER"
    STOP="systemctl --user stop moneybutton-bot"
    ;;
  *)
    echo "Автозапуск для $(uname -s) не настроен — запускайте ./scripts/run_bot.sh"
    STOP="Ctrl+C"
    ;;
esac

say "Готово. Бот работает сам."
echo "Откройте бота в Telegram и нажмите Start."
echo "Журнал:      tail -f $REPO/data/logs/bot.log"
echo "Остановить:  $STOP"
echo "Резервные копии базы — каждый день в data/backups/ (последние 7)."
