# MoneyButton Hook — самообучающийся оценщик хуков

Instagram показывает авторам **skip rate** — долю зрителей, пролиставших Reels в первые 3 секунды. Этот проект помогает выиграть эти 3 секунды и учится на реальных цифрах каждого автора.

Один движок — три интерфейса:

- **Telegram-бот** для продажи (пакеты анализов за Telegram Stars);
- **плагин для Claude** (Claude Code / Cowork) — 8 навыков и MCP-сервер памяти;
- **CLI** для локальной работы и тестов.

## Что умеет

| Функция | Бот | Плагин | CLI |
|---|---|---|---|
| 🎯 Оценка хука по 9 критериям и 18 психологическим триггерам, прогноз удержания 3 с | хук текстом или `/score` | `/hook-master:hook-score` | `hookbot score` |
| 📹 Разбор видео: кадры 0–3 с, когда начинается звук, склейки | пришлите ролик | путь к видео в `hook-score` | `hookbot score --video` |
| ✨ Генерация хуков по сценарию | `/generate` | `/hook-master:hook-generate` | `hookbot generate` |
| ♻️ Переписать старый хук | `/rewrite` | `/hook-master:hook-rewrite` | `hookbot rewrite` |
| 🎬 Что добавить: варианты звука, действия в кадре, текста, монтажа + таймлайн 0–3 с | `/actions` | `/hook-master:hook-actions` | `hookbot actions` |
| 📈 Внести статистику → переобучение | `/result 17 skip 38% просмотры 12к` | `/hook-master:hook-result` | `hookbot result` |
| 🧠 Плейбук: что работает именно у этой аудитории | `/stats`, `/playbook` | `/hook-master:hook-learn` | `hookbot learn` |
| 🔎 Аудит канала: вслепую разобрать свои Reels и найти, что держит вашу аудиторию | `/audit`, `/myreel`, `/audience` | `/hook-master:hook-audit` | `hookbot audit` |
| 👥 Целевая аудитория → авторы для неё → что у них залетает и чем отличаются ваши хуки | `/target`, `/gap` | `/hook-master:hook-target` | `hookbot target`, `hookbot gap` |
| 🛰 Робот-разведчик: круглосуточно собирает Reels, находит залетевшие, сверяет с наукой | `/science`, `/trends`, `/watch` | — | `hookbot scout run`, `hookbot science` |
| ✅ Насколько можно доверять оценщику: ошибка прогноза, AUC, надёжность | `/validate` | инструмент `validation_report` | `hookbot validate` |

## Документация

- [**Психология первых 3 секунд**](docs/PSYCHOLOGY.md) — приёмы, механизмы, таймлайн, звук/действие/текст.
- [**Научный подход**](docs/METHODOLOGY.md) — уровни доказательности, свежие исследования, корпус залетевших, аудит канала, целевая аудитория, проверка точности и ограничения.
- [**Как оценщик учится**](docs/SELF_LEARNING.md) — калибровка рубрики, влияние триггеров, плейбук, робот-разведчик, автономные напоминания.
- [**Подключение Instagram**](docs/INSTAGRAM_SETUP.md) — токен и проверка `hookbot ig-check`; без Facebook — аудит по таблице и видеофайлам.
- [**Как продавать**](docs/BUSINESS.md) — Telegram Stars, себестоимость, распространение плагина.
- База знаний: [`plugins/hook-master/knowledge/`](plugins/hook-master/knowledge/).

## Быстрый старт

### Плагин для Claude

```
/plugin install hook-master --marketplace ilyacamus/moneybutton_hook
```

или `/plugin marketplace add ilyacamus/moneybutton_hook`, затем `/plugin install hook-master@moneybutton`.

Нужен `python3` (3.11+) в `PATH`; для разбора видео — `ffmpeg`. Память и выученные веса хранятся в `${CLAUDE_PLUGIN_DATA}/hooks.db` и переживают обновления плагина.

Для аудита канала и поиска авторов вашей аудитории плагин спросит токен Instagram Graph API (`ig_access_token`; как получить — [INSTAGRAM_SETUP.md](docs/INSTAGRAM_SETUP.md)). Без токена аудит работает по CSV-выгрузке статистики и папке с роликами.

### Telegram-бот

```bash
cp .env.example .env   # TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, ADMIN_IDS
docker compose up -d --build
```

Для аудита канала и разведчика добавьте в `.env` `IG_ACCESS_TOKEN` (бизнес- или авторский аккаунт владельца бота, [как получить](docs/INSTAGRAM_SETUP.md)) и `SCOUT_ENABLED=1`, затем проверьте подключение: `hookbot ig-check`.

Без Docker:

```bash
pip install -e ".[bot]"
set -a; . ./.env; set +a
python -m telegram_bot
```

### CLI

```bash
pip install -e .
export ANTHROPIC_API_KEY=...
hookbot profile --niche "финансы для фрилансеров"
hookbot score "Хотите больше клиентов?" --visual "говорящая голова"
hookbot score "Я слил 47 000 на таргет" --video reel.mp4
hookbot generate script.txt
hookbot result 1 --skip 38 --views 12000
hookbot stats

# аудит канала и целевая аудитория (нужен IG_ACCESS_TOKEN или CSV)
hookbot ig-check
hookbot audit --csv insights.csv --videos ./reels
hookbot target --budget 40
hookbot gap
```

## Устройство

```
hookbot/core/        stdlib-ядро: база знаний (data/*.json, 65 источников), память SQLite,
                     самообучение, разбор видео через ffmpeg, статистика, корпус
                     залетевших, Instagram Graph API, аудит канала, целевая аудитория
hookbot/scout.py     робот-разведчик: сбор → слепой разбор → выводы → переобучение
hookbot/engine.py    функции оценщика на Claude API (structured outputs,
                     кэш системного промпта, server-side fallbacks)
hookbot/render.py    отчёты для Telegram (HTML) и терминала
telegram_bot/        aiogram 3: сценарии, оплата Stars, напоминания о статистике
plugins/hook-master/ плагин: навыки, MCP-сервер памяти, копия ядра (hookcore)
```

Модель по умолчанию — `claude-opus-5-5` с `effort=medium`; меняется переменными `HOOKBOT_MODEL` и `HOOKBOT_EFFORT`. Запросы идут с серверными fallback-моделями на случай отказа классификатора (`HOOKBOT_FALLBACKS=off`, чтобы выключить).

## Разработка

```bash
pip install -e ".[dev]"
pytest
python -m hookbot.plugin_sync          # после правок в hookbot/core
python -m hookbot.plugin_sync --check  # CI: плагин не отстал от ядра
claude plugin validate ./plugins/hook-master
```

Источник истины для базы знаний — `hookbot/core/data/`. Плагин содержит её копию и Markdown-версию, которые генерирует `plugin_sync`; руками их не правят.
