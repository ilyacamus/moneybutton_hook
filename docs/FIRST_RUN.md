# Первый реальный запуск: канал → целевая аудитория → обучение

Сценарий для сессии Claude Code (или для человека в терминале), когда подключён Instagram через Apify или официальный API ([INSTAGRAM_SETUP.md](INSTAGRAM_SETUP.md)).

## 0. Предусловия

- Секрет `APIFY_TOKEN` (или `IG_ACCESS_TOKEN`); разрешены домены `api.apify.com` (или `graph.facebook.com`), `*.cdninstagram.com`, `*.fbcdn.net`.
- Ник Instagram владельца: `export IG_USERNAME=<ник без @>`.
- `pip install -e ".[dev]"`, `ffmpeg` в системе.
- База — `data/hookbot.db` (в `.gitignore`). Репозиторий публичный: базу, видео и таблицы со статистикой **не коммитить**.

## 1. Подключение

```bash
hookbot ig-check                          # все шаги без ❌
pytest -m live -k "apify or instagram" -s # живые ответы: профиль, Reels с просмотрами
```

## 2. Аудит канала

```bash
hookbot profile --niche "<ниша из описания профиля>"
hookbot audit --limit 60 [--csv my_reels.csv]   # таблица добавляет skip rate и сохранения
hookbot audience
```

Без `ANTHROPIC_API_KEY` разбор хуков делает сама сессия Claude Code через инструменты плагина (`plugins/hook-master/scripts/hook_memory_mcp.py`, `HOOKBOT_DB=data/hookbot.db`, `HOOKBOT_WORKSPACE=cli`):

1. `instagram_check` → `audit_collect` (`limit`, `csv_path`);
2. `next_reels` (`scope: own`) — кадры 0–3 с и факты о звуке и склейках;
3. **оценивает отдельный агент**, которому передаются только кадры, подпись и факты, — без просмотров и лайков (слепой разбор); результат — `save_reel_analysis`;
4. повторять 2–3, пока очередь не пуста; затем `audit_report`.

## 3. Целевая аудитория и её контент

```bash
hookbot target --budget 40
hookbot gap
```

Без ключа — инструменты плагина: `audience_signals` → профиль аудитории (`save_audience_profile`) → кандидаты: веб-поиск + `search_accounts` → `verify_and_watch` → `collect_audience` → `next_reels` (`scope: audience`) с тем же слепым разбором → `gap_report`.

## 4. Наука и обучение

```bash
hookbot science   # что подтверждается на корпусе и в литературе
hookbot learn     # плейбук по результатам канала
hookbot validate  # можно ли доверять прогнозу
```

Дальше разведчик (`hookbot scout run` или бот с `SCOUT_ENABLED=1`) сам добирает новые ролики канала и авторов аудитории и переобучается. Для круглосуточной работы нужен `ANTHROPIC_API_KEY` и постоянный сервер: облачная сессия временная.

## 5. Отчёт владельцу

Что проверено вживую, сколько роликов разобрано, что связано с просмотрами, вовлечённостью и (если есть таблица) удержанием 3 с, профиль аудитории с уверенностью, найденные авторы и что у них залетает, рекомендации с оценкой доказательности, стоимость запусков Apify. Цифры — только из отчётов, без домыслов.
