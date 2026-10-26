import asyncio
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

OWNER_ID = int(os.getenv("OWNER_ID", "8799426613"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
EVENT_SECRET = os.getenv("EVENT_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DB_DIR = Path(os.getenv("DB_DIR", "db"))
DB_PATH = DB_DIR / "bot.sqlite3"
MOSCOW = ZoneInfo("Europe/Moscow")
telegram_app: Application | None = None
monitoring = True


def connect_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global monitoring
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS allowed_users (user_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS subscribers (user_id INTEGER PRIMARY KEY)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('monitoring', 'true')")
        row = conn.execute("SELECT value FROM settings WHERE key='monitoring'").fetchone()
        monitoring = row is not None and row["value"] == "true"


def remember_user(update: Update) -> None:
    user = update.effective_user
    if user is None:
        return
    now = now_moscow()
    name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO users(user_id, username, name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name,
                last_seen=excluded.last_seen
        """, (user.id, user.username or "", name, now, now))


def set_monitoring(enabled: bool) -> None:
    global monitoring
    monitoring = enabled
    with connect_db() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES ('monitoring', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("true" if enabled else "false",))


def get_ids(table: str) -> set[int]:
    if table not in {"allowed_users", "subscribers"}:
        raise ValueError("Invalid table")
    with connect_db() as conn:
        return {int(row[0]) for row in conn.execute(f"SELECT user_id FROM {table}")}


def add_id(table: str, user_id: int) -> None:
    if table not in {"allowed_users", "subscribers"}:
        raise ValueError("Invalid table")
    with connect_db() as conn:
        conn.execute(f"INSERT OR IGNORE INTO {table}(user_id) VALUES (?)", (user_id,))


def remove_id(table: str, user_id: int) -> None:
    if table not in {"allowed_users", "subscribers"}:
        raise ValueError("Invalid table")
    with connect_db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))


def is_owner(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and (user.id == OWNER_ID or user.id in get_ids("allowed_users"))


def now_moscow() -> str:
    return datetime.now(MOSCOW).strftime("%d.%m.%Y %H:%M:%S MSK")


async def send_recipients(message: str) -> None:
    if telegram_app is None:
        return
    recipients = {OWNER_ID} | get_ids("subscribers")
    for chat_id in recipients:
        try:
            await telegram_app.bot.send_message(chat_id=chat_id, text=message[:4000])
        except Exception as exc:
            print(f"Telegram delivery failed for user {chat_id}: {type(exc).__name__}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if not is_authorized(update):
        await update.effective_message.reply_text("Доступ не разрешён. Обратись к владельцу бота.")
        return
    if is_owner(update):
        set_monitoring(True)
        await update.effective_message.reply_text("Общий мониторинг включён.")
    else:
        add_id("subscribers", update.effective_user.id)
        await update.effective_message.reply_text("Подписка на события включена.")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if not is_authorized(update):
        return
    if is_owner(update):
        set_monitoring(False)
        await update.effective_message.reply_text("Общий мониторинг остановлен. События никому не отправляются.")
    else:
        remove_id("subscribers", update.effective_user.id)
        await update.effective_message.reply_text("Ты отписан от событий. Общий мониторинг продолжает работать.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if not is_authorized(update):
        return
    state = "включён" if monitoring else "остановлен"
    if is_owner(update):
        await update.effective_message.reply_text(
            f"Общий мониторинг {state}.\nРазрешённых пользователей: {len(get_ids('allowed_users'))}.\n"
            f"Подписаны на события: {len(get_ids('subscribers'))}."
        )
    else:
        own_state = "включена" if update.effective_user.id in get_ids("subscribers") else "выключена"
        await update.effective_message.reply_text(f"Общий мониторинг {state}. Твоя подписка {own_state}.")


async def rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if not is_owner(update):
        return
    if len(context.args) != 2 or context.args[0].lower() not in {"true", "false"}:
        await update.effective_message.reply_text(
            "Использование:\n/rule true TELEGRAM_ID — разрешить доступ и подписать\n"
            "/rule false TELEGRAM_ID — запретить доступ"
        )
        return
    mode = context.args[0].lower()
    try:
        target_id = int(context.args[1])
        if target_id <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Telegram ID должен быть положительным числом.")
        return
    if target_id == OWNER_ID:
        await update.effective_message.reply_text("Нельзя изменять доступ владельца.")
        return

    if mode == "true":
        add_id("allowed_users", target_id)
        add_id("subscribers", target_id)
        await update.effective_message.reply_text(f"Доступ выдан пользователю {target_id}; он добавлен в рассылку.")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Тебе разрешён доступ к мониторингу сервера. Отправь /start для подписки или /stop для отписки."
            )
        except Exception:
            await update.effective_message.reply_text(
                "Доступ сохранён, но личное уведомление не доставлено. Пользователь должен сначала открыть бота и нажать Start."
            )
    else:
        existed = target_id in get_ids("allowed_users")
        remove_id("allowed_users", target_id)
        remove_id("subscribers", target_id)
        if existed:
            await update.effective_message.reply_text(f"Доступ пользователя {target_id} отозван.")
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="Твой доступ к мониторингу сервера отозван. События больше не будут приходить."
                )
            except Exception:
                await update.effective_message.reply_text(
                    "Доступ отозван, но уведомление не доставлено: пользователь ещё не запускал бота или заблокировал его."
                )
        else:
            await update.effective_message.reply_text(f"У пользователя {target_id} доступа и так не было.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if not is_owner(update):
        return
    with connect_db() as conn:
        rows = conn.execute("SELECT user_id, username, name FROM users ORDER BY user_id").fetchall()
    if not rows:
        await update.effective_message.reply_text("Пока нет пользователей в базе. Они появятся после первого сообщения боту.")
        return
    entries = []
    for row in rows:
        label = f"@{row['username']}" if row["username"] else (row["name"] or "без username")
        entries.append(f"{label} (ID: {row['user_id']})")
    await update.effective_message.reply_text(f"Всего пользователей, писавших боту: {len(entries)}")
    chunk = ""
    for entry in entries:
        addition = entry if not chunk else ", " + entry
        if len(chunk) + len(addition) > 3500:
            await update.effective_message.reply_text(chunk)
            chunk = entry
        else:
            chunk += addition
    if chunk:
        await update.effective_message.reply_text(chunk)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)


async def event_handler(request: web.Request) -> web.Response:
    if not EVENT_SECRET:
        return web.json_response({"error": "EVENT_SECRET is not configured"}, status=503)
    if request.headers.get("X-Event-Secret", "") != EVENT_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not monitoring:
        return web.json_response({"ok": True, "ignored": "monitoring_stopped"})
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "expected JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "JSON object required"}, status=400)
    safe = {}
    blocked = ("token", "password", "passwd", "authorization", "cookie", "secret")
    for key, value in payload.items():
        if any(word in str(key).lower() for word in blocked):
            safe[str(key)] = "[скрыто]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
        else:
            safe[str(key)] = json.dumps(value, ensure_ascii=False)[:700]
    event_type = str(safe.pop("type", "event"))[:100]
    action = str(safe.pop("action", "не указано"))[:500]
    details = json.dumps(safe, ensure_ascii=False)[:1800] if safe else "без дополнительных данных"
    await send_recipients(f"[{now_moscow()}]\nТип: {event_type}\nДействие: {action}\nДанные: {details}")
    return web.json_response({"ok": True})


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "monitoring": monitoring})


async def main() -> None:
    global telegram_app
    init_db()  # Creates ./db/bot.sqlite3 automatically on first launch.
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in hosting environment variables")
    if not EVENT_SECRET:
        raise RuntimeError("Set EVENT_SECRET in hosting environment variables")

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("stop", stop_cmd))
    telegram_app.add_handler(CommandHandler("status", status_cmd))
    telegram_app.add_handler(CommandHandler("rule", rule_cmd))
    telegram_app.add_handler(CommandHandler("users", users_cmd))
    telegram_app.add_handler(MessageHandler(filters.ALL, track_user), group=1)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/event", event_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Telegram logger started; HTTP port={PORT}; database={DB_PATH}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
