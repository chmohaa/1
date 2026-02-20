import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


@dataclass
class Config:
    bot_token: str
    db_path: str = "bot.db"


def get_config() -> Config:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Set BOT_TOKEN environment variable")
    return Config(bot_token=token, db_path=os.getenv("DB_PATH", "bot.db"))


def db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                ip TEXT NOT NULL
            )
            """
        )


def ping_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏓 Пинг всех серверов", callback_data="chat_ping_all")],
            [InlineKeyboardButton(text="🎯 Пинг отдельного сервера", callback_data="chat_ping_choose")],
        ]
    )


def parse_ping_output(output: str) -> tuple[int, int, str | None]:
    sent = 4
    received = 0
    avg = None

    m_packets = re.search(r"(\d+) packets transmitted, (\d+) received", output)
    if m_packets:
        sent = int(m_packets.group(1))
        received = int(m_packets.group(2))

    m_avg = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", output)
    if m_avg:
        avg = m_avg.group(1)

    return sent, received, avg


async def ping_ip(ip: str) -> tuple[bool, int, int, str | None, str]:
    proc = await asyncio.create_subprocess_exec(
        "ping",
        "-c",
        "4",
        ip,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        return False, 4, 0, None, "timeout"

    out = stdout.decode("utf-8", errors="ignore")
    err = stderr.decode("utf-8", errors="ignore")
    sent, received, avg = parse_ping_output(out)
    ok = proc.returncode == 0
    raw = (out or err).strip()
    return ok, sent, received, avg, raw


async def cmd_help(message: Message) -> None:
    if message.chat.type in {"group", "supergroup"}:
        await message.answer(
            "Этот бот в чате умеет:\n"
            "/ping — открыть меню пинга (всех серверов или одного сервера)."
        )
        return
    await message.answer("В ЛС доступен /ping для подробной проверки серверов.")


async def cmd_ping(message: Message, config: Config) -> None:
    if message.chat.type in {"group", "supergroup"}:
        await message.answer("Выбери режим проверки:", reply_markup=ping_menu_kb())
        return

    with closing(db_connect(config.db_path)) as conn:
        rows = conn.execute("SELECT id, name, ip FROM servers ORDER BY name").fetchall()
    if not rows:
        await message.answer("Серверов пока нет.")
        return

    await message.answer("Проверяю пинг серверов (подробный режим ЛС)...")
    for row in rows:
        ok, sent, received, avg, raw = await ping_ip(row["ip"])
        status = "✅" if ok else "❌"
        avg_text = f"{avg} ms" if avg else "n/a"
        tail = "\n".join(raw.splitlines()[-3:]) if raw else "no output"
        await message.answer(
            f"{status} {row['name']} ({row['ip']})\n"
            f"packets: {sent}/{received}\n"
            f"avg: {avg_text}\n"
            f"<code>{tail}</code>"
        )


async def callback_chat_ping_all(callback: CallbackQuery, config: Config) -> None:
    if callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("Эта кнопка только для чата", show_alert=True)
        return

    with closing(db_connect(config.db_path)) as conn:
        rows = conn.execute("SELECT id, name, ip FROM servers ORDER BY name").fetchall()

    if not rows:
        await callback.message.answer("Серверов пока нет.")
        await callback.answer()
        return

    await callback.answer("Проверяю...")
    lines = ["Результат пинга:"]
    for row in rows:
        ok, sent, received, avg, _ = await ping_ip(row["ip"])
        status = "✅" if ok else "❌"
        ping_text = f"{avg} мс" if avg else "n/a"
        lines.append(
            f"{status} {row['name']} ({row['ip']})\n"
            f"пакетов отправлено/получено {sent}/{received}\n"
            f"пинг: {ping_text}"
        )
    await callback.message.answer("\n\n".join(lines))


async def callback_chat_ping_choose(callback: CallbackQuery, config: Config) -> None:
    if callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("Эта кнопка только для чата", show_alert=True)
        return

    with closing(db_connect(config.db_path)) as conn:
        rows = conn.execute("SELECT id, name FROM servers ORDER BY name").fetchall()

    if not rows:
        await callback.answer("Серверов нет", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=row["name"], callback_data=f"chat_ping_one:{row['id']}")]
            for row in rows
        ]
    )
    await callback.message.edit_text("Выбери сервер для пинга:", reply_markup=kb)
    await callback.answer()


async def callback_chat_ping_one(callback: CallbackQuery, config: Config) -> None:
    if callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("Эта кнопка только для чата", show_alert=True)
        return

    server_id = int(callback.data.split(":", 1)[1])
    with closing(db_connect(config.db_path)) as conn:
        row = conn.execute("SELECT id, name, ip FROM servers WHERE id=?", (server_id,)).fetchone()

    if not row:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    await callback.answer("Пингую...")
    ok, sent, received, avg, _ = await ping_ip(row["ip"])
    status = "✅" if ok else "❌"
    ping_text = f"{avg} мс" if avg else "n/a"
    await callback.message.answer(
        f"{status} {row['name']} ({row['ip']})\n"
        f"пакетов отправлено/получено {sent}/{received}\n"
        f"пинг: {ping_text}"
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = get_config()
    init_db(config.db_path)

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()

    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_ping, Command("ping"), flags={"config": config})

    dp.callback_query.register(callback_chat_ping_all, F.data == "chat_ping_all", flags={"config": config})
    dp.callback_query.register(callback_chat_ping_choose, F.data == "chat_ping_choose", flags={"config": config})
    dp.callback_query.register(callback_chat_ping_one, F.data.startswith("chat_ping_one:"), flags={"config": config})

    await dp.start_polling(bot, config=config)


if __name__ == "__main__":
    asyncio.run(main())
