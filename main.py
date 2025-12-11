import asyncio
import os
import sqlite3
from contextlib import closing
from urllib.parse import urlparse
import json

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv


# ---------- Настройки и запуск ----------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")

DB_PATH = "alerts.db"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


# ---------- Работа с БД (SQLite) ----------

def init_db():
    """Создаём таблицу alerts, если её ещё нет."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                market_url TEXT NOT NULL,
                outcome TEXT NOT NULL,
                target_price REAL NOT NULL,
                direction TEXT NOT NULL DEFAULT '>=',
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()


def add_alert(user_id: int, market_url: str, outcome: str, target_price: float):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO alerts (user_id, market_url, outcome, target_price, direction, active)
            VALUES (?, ?, ?, ?, '>=', 1)
            """,
            (user_id, market_url, outcome, target_price),
        )
        conn.commit()


def get_user_alerts(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            SELECT id, market_url, outcome, target_price, direction
            FROM alerts
            WHERE user_id = ? AND active = 1
            ORDER BY id
            """,
            (user_id,),
        )
        return cur.fetchall()


def deactivate_alert(user_id: int, alert_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            UPDATE alerts
            SET active = 0
            WHERE id = ? AND user_id = ? AND active = 1
            """,
            (alert_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_all_active_alerts():
    """Все активные алерты для фонового воркера."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            SELECT id, user_id, market_url, outcome, target_price, direction
            FROM alerts
            WHERE active = 1
            """
        )
        return cur.fetchall()


# ---------- Polymarket API helpers ----------

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def extract_event_slug(market_url: str) -> str | None:
    """
    Извлекает slug события из ссылки Polymarket.
    Примеры:
    - https://polymarket.com/event/bitcoin-above-on-december-12?tid=... -> bitcoin-above-on-december-12
    - https://polymarket.com/event/bitcoin-above-on-december-12/bitcoin-above-78k-on-december-12 -> bitcoin-above-on-december-12
    """
    try:
        parsed = urlparse(market_url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "event":
            return parts[1]
    except Exception:
        return None
    return None


async def fetch_event_by_slug(slug: str) -> dict | None:
    """
    Тянем описание события по slug из Gamma API.
    """
    url = f"{GAMMA_BASE}/events?slug={slug}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    # Варианты формата ответа:
    # 1) {"events": [ {...}, ... ]}
    # 2) [ {...}, ... ]
    if isinstance(data, dict):
        events = data.get("events") or []
    elif isinstance(data, list):
        events = data
    else:
        events = []

    if not events:
        return None

    return events[0]  # первый event


async def strike_exists_in_event(market_url: str, strike: int) -> bool:
    """
    Проверяет, есть ли в событии маркет с таким страйком (groupItemTitle).
    """
    slug = extract_event_slug(market_url)
    if not slug:
        return False

    event = await fetch_event_by_slug(slug)
    if not event:
        return False

    markets = event.get("markets") or []
    if not isinstance(markets, list):
        return False

    for m in markets:
        title = str(m.get("groupItemTitle") or "")
        normalized = title.replace(",", "").strip()
        try:
            v = int(normalized)
        except ValueError:
            continue
        if v == strike:
            return True

    return False


def pick_token_id_for_outcome(event: dict, outcome_str: str) -> str | None:
    """
    Выбираем ERC-1155 token_id из поля clobTokenIds:
    - ищем нужный market по страйку из outcome_str (например, '82000 YES');
    - берём clobTokenIds[0] для YES, clobTokenIds[1] для NO.
    """
    markets = event.get("markets") or []
    if not isinstance(markets, list) or not markets:
        return None

    # Разбираем outcome: ожидаем что-то вроде "82000 yes"
    parts = outcome_str.strip().split()
    strike = None
    side_yes = True  # по умолчанию YES
    if parts:
        try:
            strike = int(parts[0])
        except ValueError:
            strike = None
    if len(parts) >= 2:
        side_str = parts[1].lower()
        if side_str in ("no", "нет", "нету"):
            side_yes = False

    chosen_market = None

    # Если есть страйк — пытаемся найти по groupItemTitle ("78,000", "86,000", ...)
    if strike is not None:
        for m in markets:
            title = str(m.get("groupItemTitle") or "")
            normalized = title.replace(",", "").strip()
            try:
                v = int(normalized)
            except ValueError:
                continue
            if v == strike:
                chosen_market = m
                break

    # Если по страйку не нашли — лучше не падать в первый маркет, а вернуть None
    if chosen_market is None:
        return None

    clob_token_ids_raw = chosen_market.get("clobTokenIds")
    if not clob_token_ids_raw:
        return None

    try:
        clob_ids = json.loads(clob_token_ids_raw)
    except Exception:
        return None

    if not isinstance(clob_ids, list) or len(clob_ids) < 2:
        return None

    # 0 — YES, 1 — NO
    token_id = clob_ids[0] if side_yes else clob_ids[1]
    return str(token_id)


async def fetch_token_price(token_id: str, side: str = "BUY") -> float | None:
    """
    Получаем текущую цену токена через CLOB /price.
    Возвращаем цену в долларах (0.0–1.0).
    """
    params = {"token_id": token_id, "side": side.upper()}
    url = f"{CLOB_BASE}/price"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    price_str = data.get("price")
    if price_str is None:
        return None
    try:
        return float(price_str)
    except ValueError:
        return None


# ---------- FSM-память для /add ----------

user_add_state = {}  # {user_id: {"step": int, "market_url": str, "outcome": str}}


# ---------- Команды бота ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это бот для алертов по Polymarket.\n"
        "Команды:\n"
        "/add — добавить алерт\n"
        "/list — показать твои алерты\n"
        "/delete <id> — удалить алерт"
    )


@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    user_id = message.from_user.id
    user_add_state[user_id] = {"step": 1}
    await message.answer(
        "Ок, создаём новый алерт.\n"
        "1/3: пришли ссылку на рынок Polymarket."
    )


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    user_id = message.from_user.id
    alerts = get_user_alerts(user_id)

    if not alerts:
        await message.answer("У тебя пока нет активных алертов.")
        return

    lines = []
    for alert_id, market_url, outcome, target_price, direction in alerts:
        lines.append(
            f"ID: {alert_id}\n"
            f"Рынок: {market_url}\n"
            f"Исход: {outcome}\n"
            f"Условие: цена {direction} {target_price}\n"
            f"/delete {alert_id}\n"
            f"---"
        )

    await message.answer("\n".join(lines))


@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    user_id = message.from_user.id
    args = message.text.strip().split(maxsplit=1)

    if len(args) < 2:
        await message.answer("Нужно указать ID алерта: `/delete 1`", parse_mode="Markdown")
        return

    try:
        alert_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом, пример: `/delete 1`", parse_mode="Markdown")
        return

    ok = deactivate_alert(user_id=user_id, alert_id=alert_id)
    if ok:
        await message.answer(f"Алерт с ID {alert_id} деактивирован.")
    else:
        await message.answer("Не нашёл активный алерт с таким ID (возможны: уже удалён или чужой).")


@dp.message()
async def handle_add_flow_or_default(message: types.Message):
    """Обрабатываем шаги диалога /add, если пользователь в состоянии добавления."""
    user_id = message.from_user.id

    if user_id not in user_add_state:
        return

    state = user_add_state[user_id]
    step = state.get("step", 0)

    # Шаг 1: ждём ссылку
    if step == 1:
        market_url = message.text.strip()
        state["market_url"] = market_url
        state["step"] = 2
        await message.answer(
            "2/3: укажи исход, например: `82000 YES`.",
            parse_mode="Markdown"
        )
        return

    # Шаг 2: ждём исход
    if step == 2:
        outcome = message.text.strip()
        state["outcome"] = outcome
        state["step"] = 3
        await message.answer(
            "3/3: укажи целевую цену в центах (например, 48 означает 0.48)."
        )
        return

    # Шаг 3: ждём целевую цену + проверяем страйк
    if step == 3:
        try:
            target_price = float(message.text.replace(",", ".").strip())
        except ValueError:
            await message.answer("Не понял число. Введи цену в формате типа 48 или 12.5.")
            return

        market_url = state["market_url"]
        outcome = state["outcome"]

        # Пытаемся вытащить страйк из outcome (первое число)
        parts = outcome.strip().split()
        strike = None
        if parts:
            try:
                strike = int(parts[0])
            except ValueError:
                strike = None

        if strike is None:
            await message.answer(
                "Не смог распознать страйк в исходе. "
                "Например, напиши так: `82000 YES`.",
                parse_mode="Markdown",
            )
            return

        # Проверяем, что такой страйк реально есть в событии Polymarket
        if not await strike_exists_in_event(market_url, strike):
            await message.answer(
                "Похоже, в этом событии нет маркета с таким страйком.\n"
                "Проверь число в исходе (например, 78000, 82000, 86000) "
                "и попробуй ещё раз.",
            )
            return

        # Сохраняем в БД
        add_alert(user_id=user_id, market_url=market_url, outcome=outcome, target_price=target_price)

        # Чистим состояние
        user_add_state.pop(user_id, None)

        await message.answer(
            f"Алерт добавлен!\n\n"
            f"Рынок: {market_url}\n"
            f"Исход: {outcome}\n"
            f"Целевая цена: {target_price}"
        )
        return


# ---------- Фоновый воркер ----------

async def alerts_worker():
    """
    Цикл:
    - Берём все активные алерты.
    - Для каждого:
      - парсим slug события;
      - тянем событие из Gamma API и выбираем token_id;
      - по token_id берём текущую цену из CLOB API;
      - сравниваем с target_price (в центах);
      - при выполнении условия шлём уведомление и деактивируем алерт.
    """
    await asyncio.sleep(5)

    while True:
        alerts = get_all_active_alerts()
        print(f"[alerts_worker] Активных алертов: {len(alerts)}")

        for alert_id, user_id, market_url, outcome, target_price_cents, direction in alerts:
            try:
                slug = extract_event_slug(market_url)
                if not slug:
                    print(f"[alerts_worker] Не смог вытащить slug из URL {market_url}")
                    continue

                event = await fetch_event_by_slug(slug)
                if not event:
                    print(f"[alerts_worker] Не нашёл событие по slug {slug}")
                    continue

                token_id = pick_token_id_for_outcome(event, outcome)
                if not token_id:
                    print(f"[alerts_worker] Не смог выбрать token_id для outcome '{outcome}'")
                    continue

                price_usd = await fetch_token_price(token_id, side="BUY")
                if price_usd is None:
                    print(f"[alerts_worker] Не удалось получить цену для token_id {token_id}")
                    continue

                current_cents = price_usd * 100.0
                print(
                    f"[alerts_worker] alert {alert_id}: price={current_cents:.2f}c, "
                    f"target={target_price_cents}c, dir={direction}"
                )

                should_trigger = False
                if direction == ">=" and current_cents >= target_price_cents:
                    should_trigger = True
                elif direction == "<=" and current_cents <= target_price_cents:
                    should_trigger = True

                if should_trigger:
                    text = (
                        f"Сработал алерт #{alert_id}!\n\n"
                        f"Рынок: {market_url}\n"
                        f"Исход: {outcome}\n"
                        f"Целевая цена: {target_price_cents}c\n"
                        f"Текущая цена: {current_cents:.2f}c\n"
                    )
                    try:
                        await bot.send_message(user_id, text)
                    except Exception as e:
                        print(f"[alerts_worker] Ошибка отправки сообщения пользователю {user_id}: {e}")

                    deactivate_alert(user_id=user_id, alert_id=alert_id)

            except Exception as e:
                print(f"[alerts_worker] Ошибка при обработке алерта {alert_id}: {e}")

        await asyncio.sleep(60)


# ---------- Точка входа ----------

async def main():
    init_db()
    asyncio.create_task(alerts_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
