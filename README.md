# Telegram Ping Bot

## Chat behavior
- `/ping` in group chat opens a message with two buttons:
  - "Пинг всех серверов"
  - "Пинг отдельного сервера"
- Choosing "Пинг отдельного сервера" replaces the same message with buttons of all servers.
- Group output format:
  - ✅/❌ `name (ip)`
  - `пакетов отправлено/получено sent/received`
  - `пинг: avg мс` (from `avg`)

## Private behavior
- `/ping` in private chat prints a more detailed output.

## Env
- `BOT_TOKEN`
- `DB_PATH` (optional, default `bot.db`)

## Run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```
