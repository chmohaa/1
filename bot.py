import asyncio
import logging
import os
import sqlite3
from html import escape
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_ALLOWED_USERS = "7328877863,8024893515,6484875134"
ALLOWED_PRIVATE_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", DEFAULT_ALLOWED_USERS).split(",")
    if x.strip()
}
ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", str(min(ALLOWED_PRIVATE_USER_IDS))))


@dataclass
class Config:
    bot_token: str
    db_path: str = "bot.db"
    panel_url: str = "https://panel.example.com"
    panel_login: str = "admin@example.com"
    panel_password: str = "example123"


class AddProviderStates(StatesGroup):
    name = State()
    site_url = State()
    acc_user = State()
    acc_password = State()


class AddServerStates(StatesGroup):
    name = State()
    ip = State()
    ssh_user = State()
    ssh_password = State()
    provider_name = State()
    price = State()
    due_date = State()


class PaidDateState(StatesGroup):
    waiting_date = State()


class BalanceAdjustState(StatesGroup):
    waiting_amount = State()


def get_config() -> Config:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Set BOT_TOKEN environment variable")
    return Config(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "bot.db"),
        panel_url=os.getenv("PANEL_URL", "https://panel.example.com"),
        panel_login=os.getenv("PANEL_LOGIN", "admin@example.com"),
        panel_password=os.getenv("PANEL_PASSWORD", "example123"),
    )


def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                site_url TEXT NOT NULL,
                acc_user TEXT NOT NULL,
                acc_password TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                ip TEXT NOT NULL,
                ssh_user TEXT NOT NULL,
                ssh_password TEXT NOT NULL,
                provider_id INTEGER NOT NULL,
                price REAL NOT NULL,
                due_date TEXT NOT NULL,
                FOREIGN KEY(provider_id) REFERENCES providers(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                paid_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT,
                FOREIGN KEY(server_id) REFERENCES servers(id)
            );

            CREATE TABLE IF NOT EXISTS balance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('balance', '0')"
        )


def parse_date(value: str) -> date:
    parts = value.strip().split()
    if len(parts) != 3:
        raise ValueError("Неверный формат")
    d, m, y = map(int, parts)
    return date(y, m, d)


def format_money(amount: float) -> str:
    return f"{amount:,.2f} ₽".replace(",", " ")


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Пополнить/списать баланс", callback_data="open_balance")],
            [InlineKeyboardButton(text="🗂 Лист всех серверов", callback_data="show_all_servers")],
            [InlineKeyboardButton(text="📅 Предстоящие оплаты", callback_data="show_upcoming")],
            [InlineKeyboardButton(text="❓ Помощь", callback_data="show_help")],
        ]
    )


def reminder_kb(server_id: int, provider_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{server_id}")],
            [InlineKeyboardButton(text="🌐 Оплатить у провайдера", url=provider_url)],
        ]
    )


def server_credentials_keyboard(server: sqlite3.Row, provider: sqlite3.Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏓 Пинг", callback_data=f"ping:{server['id']}")],
            [InlineKeyboardButton(text="📋 Copy user", copy_text=CopyTextButton(text=server["ssh_user"]))],
            [InlineKeyboardButton(text="📋 Copy passwd", copy_text=CopyTextButton(text=server["ssh_password"]))],
            [InlineKeyboardButton(text="📋 Copy acc user", copy_text=CopyTextButton(text=provider["acc_user"]))],
            [InlineKeyboardButton(text="📋 Copy acc passwd", copy_text=CopyTextButton(text=provider["acc_password"]))],
            [InlineKeyboardButton(text="🌐 Сайт провайдера", url=provider["site_url"])],
        ]
    )


def db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_balance(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key='balance'").fetchone()
    return float(row["value"]) if row else 0.0


def set_balance(conn: sqlite3.Connection, value: float) -> None:
    conn.execute("UPDATE settings SET value=? WHERE key='balance'", (str(value),))


def add_transaction(conn: sqlite3.Connection, amount: float, description: str) -> None:
    conn.execute(
        "INSERT INTO balance_transactions(amount, description, created_at) VALUES(?,?,?)",
        (amount, description, datetime.now(MOSCOW_TZ).isoformat()),
    )


def is_private_chat(message: Message) -> bool:
    return message.chat.type == "private"


def is_allowed_private_user(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ALLOWED_PRIVATE_USER_IDS)


async def notify_unauthorized_attempt(bot: Bot, user_id: int | None, username: str | None, full_name: str | None, chat_id: int, chat_type: str, text: str) -> None:
    safe_text = escape((text or "").strip())
    safe_text = safe_text[:800] if safe_text else "(empty)"
    safe_username = escape(username or "-")
    safe_name = escape(full_name or "-")
    msg = (
        "🚨 Неавторизованная попытка команды\n"
        f"user_id: <code>{user_id}</code>\n"
        f"username: @{safe_username}\n"
        f"name: {safe_name}\n"
        f"chat_id: <code>{chat_id}</code>\n"
        f"chat_type: <code>{chat_type}</code>\n"
        f"text: <code>{safe_text}</code>"
    )
    try:
        await bot.send_message(ALERT_CHAT_ID, msg)
    except Exception:
        logging.exception("Failed to send unauthorized alert")


async def ensure_private_access(message: Message) -> bool:
    if not is_private_chat(message):
        await message.answer("В чате доступна только команда /ping и /help.")
        if message.from_user and message.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
            await notify_unauthorized_attempt(
                message.bot,
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
                message.chat.id,
                message.chat.type,
                message.text or "",
            )
        return False
    if not is_allowed_private_user(message):
        await message.answer("⛔ У вас нет доступа к этому боту.")
        await notify_unauthorized_attempt(
            message.bot,
            message.from_user.id if message.from_user else None,
            message.from_user.username if message.from_user else None,
            message.from_user.full_name if message.from_user else None,
            message.chat.id,
            message.chat.type,
            message.text or "",
        )
        return False
    return True


async def ping_ip(ip: str) -> tuple[bool, str]:
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
        return False, "timeout"

    output = stdout.decode("utf-8", errors="ignore").strip()
    err = stderr.decode("utf-8", errors="ignore").strip()
    if proc.returncode == 0:
        summary = output.splitlines()[-2:] if output else ["ok"]
        return True, " | ".join(summary)
    return False, (err or output or "unreachable")


async def cmd_start(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return

    with closing(db_connect(config.db_path)) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('chat_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(message.chat.id),),
        )
        conn.commit()

    text = (
        "Привет! Я помогу контролировать оплаты серверов.\n\n"
        f"Панель управления: {config.panel_url}\n"
        f"Логин: <code>{config.panel_login}</code>\n"
        f"Пароль: <code>{config.panel_password}</code>\n\n"
        "Быстрые команды: /all, /upcoming, /balance, /add_provider, /add_new_server, /help"
    )
    await message.answer(text, reply_markup=main_menu_kb())


async def cmd_help(message: Message) -> None:
    if not is_private_chat(message):
        await message.answer("Этот бот в чате умеет только команду /ping (проверка серверов).")
        return
    if not is_allowed_private_user(message):
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return

    await message.answer(
        """
Доступные команды:
/start — стартовое меню.
/help — справка.
/add_provider — добавить провайдера.
/add_new_server — добавить сервер.
/all — список всех серверов.
/upcoming — оплаты на этой неделе.
/total — сумма расходов по серверам и общая.
/balance — текущий баланс и 3 последних расхода.
/server-info-&lt;название&gt; — показать полный доступ к серверу.
        """.strip()
    )


async def cmd_add_provider(message: Message, state: FSMContext) -> None:
    if not await ensure_private_access(message):
        return
    await state.set_state(AddProviderStates.name)
    await message.answer("Введите название провайдера:")


async def provider_flow(message: Message, state: FSMContext, config: Config) -> None:
    if not await ensure_private_access(message):
        await state.clear()
        return
    current = await state.get_state()
    if current == AddProviderStates.name.state:
        await state.update_data(name=message.text.strip())
        await state.set_state(AddProviderStates.site_url)
        await message.answer("Введите ссылку на сайт провайдера:")
    elif current == AddProviderStates.site_url.state:
        await state.update_data(site_url=message.text.strip())
        await state.set_state(AddProviderStates.acc_user)
        await message.answer("Введите логин аккаунта провайдера:")
    elif current == AddProviderStates.acc_user.state:
        await state.update_data(acc_user=message.text.strip())
        await state.set_state(AddProviderStates.acc_password)
        await message.answer("Введите пароль аккаунта провайдера:")
    elif current == AddProviderStates.acc_password.state:
        data = await state.get_data()
        with closing(db_connect(config.db_path)) as conn:
            try:
                conn.execute(
                    "INSERT INTO providers(name, site_url, acc_user, acc_password) VALUES(?,?,?,?)",
                    (
                        data["name"],
                        data["site_url"],
                        data["acc_user"],
                        message.text.strip(),
                    ),
                )
                conn.commit()
                await message.answer("Провайдер добавлен ✅")
            except sqlite3.IntegrityError:
                await message.answer("Провайдер с таким названием уже существует")
        await state.clear()


async def cmd_add_server(message: Message, state: FSMContext) -> None:
    if not await ensure_private_access(message):
        return
    await state.set_state(AddServerStates.name)
    await message.answer("Название сервера:")


async def server_flow(message: Message, state: FSMContext, config: Config) -> None:
    if not await ensure_private_access(message):
        await state.clear()
        return
    current = await state.get_state()
    if current == AddServerStates.name.state:
        await state.update_data(name=message.text.strip())
        await state.set_state(AddServerStates.ip)
        await message.answer("IP сервера:")
    elif current == AddServerStates.ip.state:
        await state.update_data(ip=message.text.strip())
        await state.set_state(AddServerStates.ssh_user)
        await message.answer("Логин для входа на сервер:")
    elif current == AddServerStates.ssh_user.state:
        await state.update_data(ssh_user=message.text.strip())
        await state.set_state(AddServerStates.ssh_password)
        await message.answer("Пароль для входа на сервер:")
    elif current == AddServerStates.ssh_password.state:
        await state.update_data(ssh_password=message.text.strip())
        await state.set_state(AddServerStates.provider_name)
        await message.answer("Название провайдера (должен быть добавлен через /add_provider):")
    elif current == AddServerStates.provider_name.state:
        await state.update_data(provider_name=message.text.strip())
        await state.set_state(AddServerStates.price)
        await message.answer("Сколько нужно оплачивать (в рублях, например 1200):")
    elif current == AddServerStates.price.state:
        try:
            Decimal(message.text.strip())
        except InvalidOperation:
            await message.answer("Введите число, например 1200")
            return
        await state.update_data(price=float(message.text.strip()))
        await state.set_state(AddServerStates.due_date)
        await message.answer("Дата окончания аренды в формате xx xx xxxx (например 15 04 2026):")
    elif current == AddServerStates.due_date.state:
        try:
            due = parse_date(message.text)
        except ValueError:
            await message.answer("Неверный формат. Пример: 15 04 2026")
            return
        data = await state.get_data()
        with closing(db_connect(config.db_path)) as conn:
            provider = conn.execute(
                "SELECT id FROM providers WHERE name=?", (data["provider_name"],)
            ).fetchone()
            if not provider:
                await message.answer("Провайдер не найден. Сначала добавьте через /add_provider")
                await state.clear()
                return
            try:
                conn.execute(
                    """
                    INSERT INTO servers(name, ip, ssh_user, ssh_password, provider_id, price, due_date)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        data["name"],
                        data["ip"],
                        data["ssh_user"],
                        data["ssh_password"],
                        provider["id"],
                        data["price"],
                        due.isoformat(),
                    ),
                )
                conn.commit()
                await message.answer("Сервер добавлен ✅")
            except sqlite3.IntegrityError:
                await message.answer("Сервер с таким названием уже существует")
        await state.clear()


async def show_all_servers(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return
    with closing(db_connect(config.db_path)) as conn:
        rows = conn.execute("SELECT id, name, due_date FROM servers ORDER BY due_date").fetchall()
    if not rows:
        await message.answer("Серверов пока нет. Используй /add_new_server")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{r['name']} — до {r['due_date']}", callback_data=f"server:{r['id']}"
                )
            ]
            for r in rows
        ]
    )
    await message.answer("Список серверов. Для добавления: /add_new_server", reply_markup=kb)


async def callback_show_server(callback: CallbackQuery, config: Config) -> None:
    if callback.message.chat.type != "private" or not callback.from_user or callback.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
        await callback.answer("Недоступно в этом чате", show_alert=True)
        await notify_unauthorized_attempt(
            callback.bot,
            callback.from_user.id if callback.from_user else None,
            callback.from_user.username if callback.from_user else None,
            callback.from_user.full_name if callback.from_user else None,
            callback.message.chat.id,
            callback.message.chat.type,
            callback.data or "",
        )
        return

    server_id = int(callback.data.split(":", 1)[1])
    with closing(db_connect(config.db_path)) as conn:
        server = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if not server:
            await callback.message.answer("Сервер не найден")
            await callback.answer()
            return
        provider = conn.execute("SELECT * FROM providers WHERE id=?", (server["provider_id"],)).fetchone()

    text = (
        f"Сервер: <b>{server['name']}</b>\n"
        f"Провайдер: {provider['name']}\n"
        f"IP: <code>{server['ip']}</code>\n"
        f"user: <code>{server['ssh_user']}</code>\n"
        f"passwd: <code>{server['ssh_password']}</code>\n"
        f"acc user: <code>{provider['acc_user']}</code>\n"
        f"acc passwd: <code>{provider['acc_password']}</code>\n"
        f"Оплата: {format_money(server['price'])}\n"
        f"Дата окончания: {server['due_date']}"
    )
    await callback.message.answer(text, reply_markup=server_credentials_keyboard(server, provider))
    await callback.answer()


async def server_info_alias(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return
    if not message.text.startswith("/server-info-"):
        return
    name = message.text.replace("/server-info-", "", 1).strip()
    with closing(db_connect(config.db_path)) as conn:
        server = conn.execute("SELECT * FROM servers WHERE name=?", (name,)).fetchone()
        if not server:
            await message.answer("Сервер не найден")
            return
        provider = conn.execute("SELECT * FROM providers WHERE id=?", (server["provider_id"],)).fetchone()
    await message.answer(
        (
            f"Сервер: <b>{server['name']}</b>\n"
            f"Провайдер: {provider['name']}\nIP: <code>{server['ip']}</code>\n"
            f"user: <code>{server['ssh_user']}</code>\npasswd: <code>{server['ssh_password']}</code>\n"
            f"acc user: <code>{provider['acc_user']}</code>\nacc passwd: <code>{provider['acc_password']}</code>"
        ),
        reply_markup=server_credentials_keyboard(server, provider),
    )


async def show_upcoming(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return
    now = datetime.now(MOSCOW_TZ).date()
    week_end = now + timedelta(days=7)
    with closing(db_connect(config.db_path)) as conn:
        rows = conn.execute(
            """
            SELECT s.name, s.ip, s.price, s.due_date, p.site_url
            FROM servers s
            JOIN providers p ON p.id=s.provider_id
            WHERE DATE(s.due_date) BETWEEN DATE(?) AND DATE(?)
            ORDER BY s.due_date
            """,
            (now.isoformat(), week_end.isoformat()),
        ).fetchall()
    if not rows:
        await message.answer("На этой неделе оплат нет 🎉")
        return
    lines = ["Предстоящие оплаты на этой неделе:"]
    for r in rows:
        lines.append(
            f"• {r['name']} ({r['ip']}) — {format_money(r['price'])}, до {r['due_date']}"
        )
    await message.answer("\n".join(lines))


async def cmd_total(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return
    with closing(db_connect(config.db_path)) as conn:
        per_server = conn.execute(
            """
            SELECT s.name, COALESCE(SUM(p.amount), 0) AS total
            FROM servers s
            LEFT JOIN payments p ON p.server_id=s.id
            GROUP BY s.id
            ORDER BY s.name
            """
        ).fetchall()
        grand = conn.execute("SELECT COALESCE(SUM(amount), 0) AS g FROM payments").fetchone()["g"]
    lines = ["Расходы по серверам:"]
    for row in per_server:
        lines.append(f"• {row['name']}: {format_money(row['total'])}")
    lines.append(f"\nИтого по всем серверам: {format_money(grand)}")
    await message.answer("\n".join(lines))


async def cmd_balance(message: Message, config: Config) -> None:
    if not await ensure_private_access(message):
        return
    with closing(db_connect(config.db_path)) as conn:
        bal = get_balance(conn)
        rows = conn.execute(
            "SELECT amount, description, created_at FROM balance_transactions WHERE amount < 0 ORDER BY id DESC LIMIT 3"
        ).fetchall()
    text = [f"Баланс: <b>{format_money(bal)}</b>", "", "Последние 3 расхода:"]
    if not rows:
        text.append("— пока нет")
    for r in rows:
        text.append(
            f"• {format_money(abs(r['amount']))} — {r['description']} ({r['created_at'][:10]})"
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💰 Пополнить/потратить баланс", callback_data="adjust_balance")]]
    )
    await message.answer("\n".join(text), reply_markup=kb)


async def callback_paid(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message.chat.type != "private" or not callback.from_user or callback.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
        await callback.answer("Недоступно", show_alert=True)
        await notify_unauthorized_attempt(
            callback.bot,
            callback.from_user.id if callback.from_user else None,
            callback.from_user.username if callback.from_user else None,
            callback.from_user.full_name if callback.from_user else None,
            callback.message.chat.id,
            callback.message.chat.type,
            callback.data or "",
        )
        return
    server_id = int(callback.data.split(":", 1)[1])
    await state.set_state(PaidDateState.waiting_date)
    await state.update_data(paid_server_id=server_id)
    await callback.message.answer("Введите дату оплаты/новой аренды в формате xx xx xxxx")
    await callback.answer()


async def save_paid_date(message: Message, state: FSMContext, config: Config) -> None:
    if not await ensure_private_access(message):
        await state.clear()
        return
    data = await state.get_data()
    server_id = data.get("paid_server_id")
    if not server_id:
        await message.answer("Не удалось определить сервер")
        await state.clear()
        return
    try:
        due = parse_date(message.text)
    except ValueError:
        await message.answer("Неверный формат даты. Пример: 15 04 2026")
        return

    with closing(db_connect(config.db_path)) as conn:
        server = conn.execute("SELECT name, price FROM servers WHERE id=?", (server_id,)).fetchone()
        if not server:
            await message.answer("Сервер не найден")
            await state.clear()
            return
        conn.execute("UPDATE servers SET due_date=? WHERE id=?", (due.isoformat(), server_id))
        conn.execute(
            "INSERT INTO payments(server_id, amount, paid_date, created_at, note) VALUES(?,?,?,?,?)",
            (
                server_id,
                server["price"],
                due.isoformat(),
                datetime.now(MOSCOW_TZ).isoformat(),
                "Оплачено через кнопку",
            ),
        )
        current_balance = get_balance(conn)
        new_balance = current_balance - float(server["price"])
        set_balance(conn, new_balance)
        add_transaction(conn, -float(server["price"]), f"Оплата {server['name']}")
        conn.commit()
    await message.answer(
        f"Отлично, платёж для {server['name']} учтен. Новая дата окончания: {due.isoformat()}"
    )
    await state.clear()


async def callback_adjust_balance(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message.chat.type != "private" or not callback.from_user or callback.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
        await callback.answer("Недоступно", show_alert=True)
        await notify_unauthorized_attempt(
            callback.bot,
            callback.from_user.id if callback.from_user else None,
            callback.from_user.username if callback.from_user else None,
            callback.from_user.full_name if callback.from_user else None,
            callback.message.chat.id,
            callback.message.chat.type,
            callback.data or "",
        )
        return
    await state.set_state(BalanceAdjustState.waiting_amount)
    await callback.message.answer("Введите сумму в формате +100 или -100")
    await callback.answer()


async def apply_balance_adjust(message: Message, state: FSMContext, config: Config) -> None:
    if not await ensure_private_access(message):
        await state.clear()
        return
    raw = message.text.strip().replace(" ", "")
    if not (raw.startswith("+") or raw.startswith("-")):
        await message.answer("Формат должен быть +100 или -100")
        return
    try:
        delta = float(raw)
    except ValueError:
        await message.answer("Не удалось распознать сумму")
        return
    with closing(db_connect(config.db_path)) as conn:
        bal = get_balance(conn)
        new_balance = bal + delta
        set_balance(conn, new_balance)
        add_transaction(conn, delta, "Ручная корректировка баланса")
        conn.commit()
    await message.answer(f"Готово. Новый баланс: {format_money(new_balance)}")
    await state.clear()


async def callbacks_router(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if callback.message.chat.type != "private" or not callback.from_user or callback.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
        await callback.answer("Недоступно", show_alert=True)
        await notify_unauthorized_attempt(
            callback.bot,
            callback.from_user.id if callback.from_user else None,
            callback.from_user.username if callback.from_user else None,
            callback.from_user.full_name if callback.from_user else None,
            callback.message.chat.id,
            callback.message.chat.type,
            callback.data or "",
        )
        return

    if callback.data == "show_help":
        await cmd_help(callback.message)
        await callback.answer()
    elif callback.data == "show_all_servers":
        await show_all_servers(callback.message, config)
        await callback.answer()
    elif callback.data == "show_upcoming":
        await show_upcoming(callback.message, config)
        await callback.answer()
    elif callback.data == "open_balance":
        await cmd_balance(callback.message, config)
        await callback.answer()


async def send_reminders(bot: Bot, config: Config) -> None:
    now = datetime.now(MOSCOW_TZ).date()
    target = now + timedelta(days=2)
    with closing(db_connect(config.db_path)) as conn:
        users = []
        # Последний пользователь, который написал /start
        row = conn.execute("SELECT value FROM settings WHERE key='chat_id'").fetchone()
        if row:
            chat_id = int(row["value"])
            if chat_id in ALLOWED_PRIVATE_USER_IDS:
                users = [chat_id]
        servers = conn.execute(
            """
            SELECT s.id, s.name, s.ip, s.price, s.due_date, p.site_url
            FROM servers s
            JOIN providers p ON p.id=s.provider_id
            WHERE DATE(s.due_date) BETWEEN DATE(?) AND DATE(?)
            """,
            (now.isoformat(), target.isoformat()),
        ).fetchall()

    if not users:
        return

    for chat_id in users:
        for s in servers:
            cmd = f"/server-info-{s['name']}"
            text = (
                "⏰ Напоминание об оплате сервера\n"
                f"Название: {s['name']}\n"
                f"IP: <code>{s['ip']}</code>\n"
                f"К оплате: {format_money(s['price'])}\n"
                f"Дата окончания аренды: {s['due_date']}\n"
                f"Детали сервера: <code>{cmd}</code>"
            )
            await bot.send_message(chat_id, text, reply_markup=reminder_kb(s["id"], s["site_url"]))


async def cmd_ping(message: Message, config: Config) -> None:
    if is_private_chat(message) and not is_allowed_private_user(message):
        await message.answer("⛔ У вас нет доступа к этому боту.")
        await notify_unauthorized_attempt(
            message.bot,
            message.from_user.id if message.from_user else None,
            message.from_user.username if message.from_user else None,
            message.from_user.full_name if message.from_user else None,
            message.chat.id,
            message.chat.type,
            message.text or "",
        )
        return

    with closing(db_connect(config.db_path)) as conn:
        servers = conn.execute("SELECT id, name, ip FROM servers ORDER BY name").fetchall()

    if not servers:
        await message.answer("Серверов пока нет.")
        return

    await message.answer("Проверяю пинг серверов (ping -c 4)...")
    lines = ["Результат /ping:"]
    for srv in servers:
        ok, details = await ping_ip(srv["ip"])
        status = "✅" if ok else "❌"
        lines.append(f"{status} {srv['name']} ({srv['ip']}) — <code>{details}</code>")
    await message.answer("\n".join(lines))


async def callback_ping_server(callback: CallbackQuery, config: Config) -> None:
    if callback.message.chat.type != "private" or not callback.from_user or callback.from_user.id not in ALLOWED_PRIVATE_USER_IDS:
        await callback.answer("Недоступно", show_alert=True)
        await notify_unauthorized_attempt(
            callback.bot,
            callback.from_user.id if callback.from_user else None,
            callback.from_user.username if callback.from_user else None,
            callback.from_user.full_name if callback.from_user else None,
            callback.message.chat.id,
            callback.message.chat.type,
            callback.data or "",
        )
        return

    server_id = int(callback.data.split(":", 1)[1])
    with closing(db_connect(config.db_path)) as conn:
        server = conn.execute("SELECT name, ip FROM servers WHERE id=?", (server_id,)).fetchone()

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    await callback.answer("Пингую...")
    ok, details = await ping_ip(server["ip"])
    status = "✅" if ok else "❌"
    await callback.message.answer(
        f"{status} Пинг {server['name']} ({server['ip']}):\n<code>{details}</code>"
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = get_config()
    init_db(config.db_path)

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart(), flags={"config": config})
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_ping, Command("ping"), flags={"config": config})
    dp.message.register(cmd_add_provider, Command("add_provider"))
    dp.message.register(cmd_add_server, Command("add_new_server"))
    dp.message.register(show_all_servers, Command("all"), flags={"config": config})
    dp.message.register(show_upcoming, Command("upcoming"), flags={"config": config})
    dp.message.register(cmd_total, Command("total"), flags={"config": config})
    dp.message.register(cmd_balance, Command("balance"), flags={"config": config})
    dp.message.register(server_info_alias, F.text.startswith("/server-info-"), flags={"config": config})

    dp.callback_query.register(callback_show_server, F.data.startswith("server:"), flags={"config": config})
    dp.callback_query.register(callback_ping_server, F.data.startswith("ping:"), flags={"config": config})
    dp.callback_query.register(callback_paid, F.data.startswith("paid:"))
    dp.callback_query.register(callback_adjust_balance, F.data == "adjust_balance")
    dp.callback_query.register(callbacks_router, flags={"config": config})

    dp.message.register(provider_flow, AddProviderStates.name, flags={"config": config})
    dp.message.register(provider_flow, AddProviderStates.site_url, flags={"config": config})
    dp.message.register(provider_flow, AddProviderStates.acc_user, flags={"config": config})
    dp.message.register(provider_flow, AddProviderStates.acc_password, flags={"config": config})

    dp.message.register(server_flow, AddServerStates.name, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.ip, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.ssh_user, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.ssh_password, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.provider_name, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.price, flags={"config": config})
    dp.message.register(server_flow, AddServerStates.due_date, flags={"config": config})

    dp.message.register(save_paid_date, PaidDateState.waiting_date, flags={"config": config})
    dp.message.register(apply_balance_adjust, BalanceAdjustState.waiting_amount, flags={"config": config})

    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(send_reminders, CronTrigger(hour=8, minute=0), kwargs={"bot": bot, "config": config})
    scheduler.add_job(send_reminders, CronTrigger(hour=17, minute=0), kwargs={"bot": bot, "config": config})
    scheduler.start()

    await dp.start_polling(bot, config=config)


if __name__ == "__main__":
    asyncio.run(main())
