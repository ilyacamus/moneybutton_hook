# Личный Telegram-бот на вашем Mac

Бот отвечает только вам. Остальные пользователи получают отказ, а их сообщения не сохраняются и не анализируются. Анализы без ограничений, оплаты в боте нет. Расход — запросы к Claude API: в среднем несколько центов за анализ, видно в консоли Anthropic.

## 1. Что понадобится

1. **Токен бота.** Напишите [@BotFather](https://t.me/BotFather) команду `/newbot`, задайте имя и ник бота. Токен вида `1234567890:AA…` придёт в ответ. Токен — секрет: не пересылайте его в чаты.
2. **Ваш Telegram id.** Напишите любое сообщение [@userinfobot](https://t.me/userinfobot), он пришлёт число `Id`.
3. **Ключ Claude API.** [console.anthropic.com](https://console.anthropic.com) → API keys → Create key. Пополните баланс; для старта хватит $10–20.
4. **Mac** с Python 3.13 и ffmpeg. Как установить — в разделе «Установка» [LOCAL_MODE.md](LOCAL_MODE.md).

## 2. Установка

```bash
cd moneybutton_hook                       # клон репозитория
git pull
.venv/bin/pip install -e ".[bot,asr]"     # бот и распознавание речи в видео

# база знаний: ваш канал, крипто-ниша, обученные модели (приватный репозиторий)
git clone https://github.com/ilyacamus/moneybutton_data ../moneybutton_data
mkdir -p data && cp ../moneybutton_data/db/hookbot.db data/hookbot.db
```

В `.env` (создаётся из `.env.example`):

```
TELEGRAM_BOT_TOKEN=<токен от BotFather>
ADMIN_IDS=<ваш id>
BOT_PRIVATE=1
OWNER_NICHE=Крипта и трейдинг
ANTHROPIC_API_KEY=<ключ>
HOOKBOT_DB=data/hookbot.db
```

## 3. Проверка и запуск

```bash
set -a; . ./.env; set +a
.venv/bin/python -m telegram_bot --check   # токен, ключ, доступ, ниша и её модель — всё ✅
.venv/bin/python -m telegram_bot           # запуск; остановка — Ctrl+C
```

Откройте бота в Telegram и нажмите **Start**. В приветствии будут ниша и модель ниши. Если с другого аккаунта бот отвечает «закрыт», личный режим работает.

## 4. Автозапуск

Бот будет работать, пока Mac включён, и сам перезапустится после сбоя.

```bash
sed "s#REPO#$PWD#" scripts/macos/com.moneybutton.bot.plist > ~/Library/LaunchAgents/com.moneybutton.bot.plist
launchctl load ~/Library/LaunchAgents/com.moneybutton.bot.plist
tail -f data/logs/bot.log
```

Остановить: `launchctl unload ~/Library/LaunchAgents/com.moneybutton.bot.plist`.

Чтобы бот ещё и сам учился на авторах ниши, добавьте в `.env` `SCOUT_ENABLED=1` и `IG_SOURCE=web` ([LOCAL_MODE.md](LOCAL_MODE.md)). Тогда отдельный `scout run` не запускайте: обоим процессам нужна одна база.

## 5. Как пользоваться

| Что прислать | Что получите |
|---|---|
| Фразу из первых 3 секунд (можно добавить строки «Текст на экране:», «Визуал:») | Оценку по 9 критериям, прогноз удержания, **потенциал залёта в крипто-нише**, 3 улучшенные версии |
| Сценарий (кнопка «✨ Хуки по сценарию» или `/generate …`) | 8 хуков на разных приёмах |
| Видео | Разбор кадров 0–3 с, звука и склеек |
| `/result 17 skip 38% просмотры 12к` после публикации | Бот запоминает реальный результат и подстраивает оценки под вашу аудиторию |
| `/science`, `/stats`, `/playbook` | Что работает в нише, чему бот научился, правила для вашего канала |
| `/niche @аккаунт` | Ниша другого канала (когда подключён Instagram) |

## 6. Как открыть бот для других

Добавьте их id в `ADMIN_IDS` через запятую. Бот сам присылает id пользователя в ответ «закрыт». Чтобы продавать анализы за Telegram Stars, поставьте `BOT_PRIVATE=0`: новые пользователи получат бесплатные анализы и пакеты из `HOOKBOT_PACKS` ([BUSINESS.md](BUSINESS.md)).

Пока бот работает на Mac, он недоступен, когда Mac выключен или спит. Для круглосуточной работы тот же код запускается на сервере (`docker compose up -d`, см. README).
