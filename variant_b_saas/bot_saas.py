# -*- coding: utf-8 -*-
"""
ВАРИАНТ Б: КОММЕРЧЕСКИЙ SaaS-БОТ (АНАЛОГ SIZIPH.RU НА ПРОДАЖУ)
Мультипользовательская система с тарифами, дневными квотами, балансом,
реферальной программой (+750 ₽ / подарочные дни как в Сизифе),
СБ-проверкой работодателей и панелью администратора (/admin).

Тарифная сетка (как в Сизифе + расширение на все площадки и Антифрод):
 - 🆓 TRIAL (Пробный): 10 мульти-поисков/сутки, 3 ИИ-адаптации резюме/писем, 3 СБ-проверки работодателей.
 - ⚡ FAST (7 дней — 690 ₽): до 100 поисков и откликов/сутки, безлимит писем, 20 СБ-проверок/сутки.
 - 💪 STRONG (30 дней — 1 490 ₽): полный доступ ко всем площадкам, безлимит адаптаций резюме и СБ-проверок.
 - 👑 MAX (30 дней + VIP — 2 490 ₽): всё из Strong + ИИ-коуч глубокого разбора резюме и автозаполнение анкет.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Подключаем общее ядро
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_engine import (
    DEFAULT_TG_CHANNELS,
    RESUME_FOCUSES,
    Vacancy,
    adapt_resume_for_vacancy,
    build_resume_variant,
    deep_check_employer,
    extract_text_from_pdf_bytes,
    generate_cover_letter,
    pick_best_resume_variant,
    search_all_platforms,
    search_social_networks,
    verify_resume_facts,
)
from shared_engine.miniapp import webapp_url

# ─────────────────────────────────────────────────────────────────────────────
# ТАРИФНЫЕ ПЛАНЫ И КВОТЫ SaaS
# ─────────────────────────────────────────────────────────────────────────────

PLANS = {
    "trial": {
        "name": "🆓 Пробный (Free)",
        "price_rub": 0,
        "days": 365,
        "daily_searches": 10,
        "daily_ai": 3,
        "daily_scam_checks": 3,
    },
    "fast": {
        "name": "⚡ Fast (7 дней)",
        "price_rub": 690,
        "days": 7,
        "daily_searches": 100,
        "daily_ai": 50,
        "daily_scam_checks": 30,
    },
    "strong": {
        "name": "💪 Strong (30 дней)",
        "price_rub": 1490,
        "days": 30,
        "daily_searches": 300,
        "daily_ai": 200,
        "daily_scam_checks": 150,
    },
    "max": {
        "name": "👑 Max VIP (30 дней)",
        "price_rub": 2490,
        "days": 30,
        "daily_searches": 9999,
        "daily_ai": 9999,
        "daily_scam_checks": 9999,
    },
}

REFERRAL_BONUS_RUB = 750  # Как на промо-плитке Сизифа («+750 ₽ за друга»)


# ─────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ SaaS (МУЛЬТИПОЛЬЗОВАТЕЛЬСКАЯ + БИЛЛИНГ + РЕФЕРАЛКА)
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).resolve().parent / "saas_platform.db"


class SaaSDB:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    full_name TEXT DEFAULT '',
                    plan TEXT DEFAULT 'trial',
                    plan_until TEXT DEFAULT '',
                    balance_rub INTEGER DEFAULT 0,
                    gift_days INTEGER DEFAULT 1,
                    referred_by INTEGER DEFAULT 0,
                    referrals_count INTEGER DEFAULT 0,
                    resume_text TEXT DEFAULT '',
                    search_query TEXT DEFAULT 'Менеджер / Специалист',
                    city TEXT DEFAULT 'Москва',
                    min_salary INTEGER DEFAULT 0,
                    usage_date TEXT DEFAULT '',
                    used_searches INTEGER DEFAULT 0,
                    used_ai INTEGER DEFAULT 0,
                    used_scam INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    plan TEXT,
                    amount_rub INTEGER,
                    status TEXT DEFAULT 'paid',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get_or_create_user(
        self,
        user_id: int,
        username: str = "",
        full_name: str = "",
        referrer_id: int = 0,
    ) -> Tuple[Dict[str, Any], bool]:
        """Возвращает (профиль_пользователя, is_new_user)."""
        today = dt.date.today().isoformat()
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            is_new = False
            if not row:
                is_new = True
                valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else 0
                c.execute(
                    """
                    INSERT INTO users (user_id, username, full_name, referred_by, usage_date)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, username or "", full_name or "", valid_ref, today),
                )
                if valid_ref:
                    c.execute(
                        """
                        UPDATE users
                        SET balance_rub = balance_rub + ?,
                            gift_days = gift_days + 1,
                            referrals_count = referrals_count + 1
                        WHERE user_id = ?
                        """,
                        (REFERRAL_BONUS_RUB, valid_ref),
                    )
                row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

            user = dict(row)

            # Сброс суточных лимитов в полночь
            if user.get("usage_date") != today:
                c.execute(
                    """
                    UPDATE users
                    SET usage_date = ?, used_searches = 0, used_ai = 0, used_scam = 0
                    WHERE user_id = ?
                    """,
                    (today, user_id),
                )
                user["usage_date"] = today
                user["used_searches"] = 0
                user["used_ai"] = 0
                user["used_scam"] = 0

            # Проверка истечения платного тарифа
            if user["plan"] != "trial" and user.get("plan_until"):
                try:
                    until_dt = dt.date.fromisoformat(user["plan_until"])
                    if until_dt < dt.date.today():
                        c.execute("UPDATE users SET plan = 'trial', plan_until = '' WHERE user_id = ?", (user_id,))
                        user["plan"] = "trial"
                        user["plan_until"] = ""
                except Exception:
                    pass

            return user, is_new

    def check_and_consume_quota(self, user_id: int, kind: str) -> Tuple[bool, str]:
        """
        kind: 'search' | 'ai' | 'scam'
        Возвращает (разрешено_ли, сообщение_если_лимит).
        """
        user, _ = self.get_or_create_user(user_id)
        plan_key = user.get("plan", "trial")
        plan = PLANS.get(plan_key, PLANS["trial"])

        col_map = {
            "search": ("used_searches", plan["daily_searches"], "мульти-поисков вакансий"),
            "ai": ("used_ai", plan["daily_ai"], "ИИ-генераций резюме и писем"),
            "scam": ("used_scam", plan["daily_scam_checks"], "СБ-проверок работодателей"),
        }
        col, limit_val, label = col_map[kind]
        used = int(user.get(col) or 0)
        if used >= limit_val:
            return (
                False,
                f"🔒 <b>Дневной лимит {label} исчерпан ({used}/{limit_val}) на тарифе {plan['name']}.</b>\n\n"
                "Перейдите в раздел «💳 Тарифы и Бонусы», чтобы активировать подарочный день или подключить тариф Fast / Strong / Max!",
            )
        with self._conn() as c:
            c.execute(f"UPDATE users SET {col} = {col} + 1 WHERE user_id = ?", (user_id,))
        return True, ""

    def activate_plan(self, user_id: int, plan_key: str, amount_rub: int = 0) -> Dict[str, Any]:
        plan = PLANS[plan_key]
        until = (dt.date.today() + dt.timedelta(days=plan["days"])).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE users SET plan = ?, plan_until = ? WHERE user_id = ?",
                (plan_key, until, user_id),
            )
            c.execute(
                "INSERT INTO payments (user_id, plan, amount_rub, status) VALUES (?, ?, ?, 'paid')",
                (user_id, plan_key, amount_rub),
            )
        u, _ = self.get_or_create_user(user_id)
        return u

    def activate_gift_day(self, user_id: int) -> Tuple[bool, str]:
        user, _ = self.get_or_create_user(user_id)
        gifts = int(user.get("gift_days") or 0)
        if gifts <= 0:
            return False, "У вас пока нет подарочных дней. Пригласите друга по реферальной ссылке и получите +1 день и +750 ₽!"
        until = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE users SET gift_days = gift_days - 1, plan = 'strong', plan_until = ? WHERE user_id = ?",
                (until, user_id),
            )
        return True, f"🎁 Подарочный день активирован! Тариф Strong включён до {until}."

    def update_field(self, user_id: int, field: str, val: Any) -> None:
        with self._conn() as c:
            c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (val, user_id))

    def get_admin_stats(self) -> Dict[str, Any]:
        with self._conn() as c:
            total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            paid_users = c.execute("SELECT COUNT(*) FROM users WHERE plan != 'trial'").fetchone()[0]
            revenue = c.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM payments").fetchone()[0]
            searches_today = c.execute("SELECT COALESCE(SUM(used_searches), 0) FROM users").fetchone()[0]
            return {
                "total_users": total_users,
                "paid_users": paid_users,
                "revenue": revenue,
                "searches_today": searches_today,
            }


db = SaaSDB()
USER_DECKS: Dict[int, Dict[str, Any]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# СОСТОЯНИЯ FSM
# ─────────────────────────────────────────────────────────────────────────────

class SaaSForm(StatesGroup):
    waiting_query = State()
    waiting_resume = State()
    waiting_scam_target = State()
    waiting_adapt_vacancy = State()
    waiting_letter_vacancy = State()


# ─────────────────────────────────────────────────────────────────────────────
# КЛАВИАТУРЫ SaaS
# ─────────────────────────────────────────────────────────────────────────────

def saas_main_kb() -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(text="🔎 Найти вакансии (Все площадки)"),
            KeyboardButton(text="🛡 Пробить работодателя (СБ)"),
        ],
        [
            KeyboardButton(text="🌐 Соцсети (Reddit, Discord, HN…)"),
            KeyboardButton(text="📄 ИИ-Резюме и Сопроводительные"),
        ],
        [
            KeyboardButton(text="💳 Тарифы и Бонусы (+750 ₽)"),
            KeyboardButton(text="👤 Личный кабинет и Квоты"),
        ],
    ]
    app_url = webapp_url("saas")
    if app_url:
        rows.append([KeyboardButton(text="🕊 Открыть приложение", web_app=WebAppInfo(url=app_url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def saas_plans_kb(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    gifts = int(user.get("gift_days") or 0)
    rows = [
        [InlineKeyboardButton(text="⚡ Купить Fast (7 дней) — 690 ₽", callback_data="buy:fast")],
        [InlineKeyboardButton(text="💪 Купить Strong (30 дней) — 1 490 ₽ (Хит)", callback_data="buy:strong")],
        [InlineKeyboardButton(text="👑 Купить Max VIP (30 дней) — 2 490 ₽", callback_data="buy:max")],
        [InlineKeyboardButton(text=f"🎁 Запустить подарочный день (доступно: {gifts})", callback_data="gift:activate")],
        [InlineKeyboardButton(text="🤝 Пригласить друга (+750 ₽ и +1 день)", callback_data="ref:show")],
    ]
    plans_url = webapp_url("saas", anchor="plans")
    if plans_url:
        rows.append(
            [InlineKeyboardButton(text="🕊 Открыть тарифы в приложении", web_app=WebAppInfo(url=plans_url))]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def saas_deck_kb(idx: int, total: int, url: str) -> InlineKeyboardMarkup:
    prev_i = max(0, idx - 1)
    next_i = min(total - 1, idx + 1)
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sdeck:nav:{prev_i}"))
    nav.append(InlineKeyboardButton(text=f"{idx + 1} / {total}", callback_data="sdeck:noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sdeck:nav:{next_i}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav,
            [
                InlineKeyboardButton(text="✉️ ИИ-Письмо", callback_data=f"sdeck:letter:{idx}"),
                InlineKeyboardButton(text="🎯 Адаптировать резюме", callback_data=f"sdeck:adapt:{idx}"),
            ],
            [
                InlineKeyboardButton(text="🛡 Пробить в СБ", callback_data=f"sdeck:scam:{idx}"),
                InlineKeyboardButton(text="🔗 Откликнуться ↗", url=url),
            ],
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# РОУТЕР SaaS-БОТА
# ─────────────────────────────────────────────────────────────────────────────

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    ref_id = 0
    if command.args and command.args.startswith("ref_"):
        digits = "".join(c for c in command.args if c.isdigit())
        if digits:
            ref_id = int(digits)

    u, is_new = db.get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
        referrer_id=ref_id,
    )
    plan_info = PLANS.get(u["plan"], PLANS["trial"])
    app_hint = (
        "\n🕊 <b>Mini App:</b> кнопка «Открыть приложение» — тарифы, квоты и СБ-проверки в одном окне.\n"
        if webapp_url("saas")
        else ""
    )

    await message.answer(
        "🚀 <b>Добро пожаловать в Автопилот Поиска Работы и СБ-Проверки (SaaS Edition — Вариант Б)!</b>\n\n"
        "Мы автоматизируем поиск вакансий на <b>HH.ru, в Telegram-каналах, на Авито, ВКонтакте, Одноклассниках, Habr, SuperJob, LinkedIn и Instagram</b>, "
        "переписываем резюме под каждую вакансию и проверяем работодателей на мошенничество.\n\n"
        f"💳 <b>Ваш тариф:</b> {plan_info['name']}\n"
        f"🎁 <b>Подарочных дней на счету:</b> {u['gift_days']} дн.\n"
        f"💰 <b>Бонусный баланс:</b> {u['balance_rub']} ₽\n"
        f"🎯 <b>Текущая профессия:</b> <code>{u['search_query']}</code>\n"
        f"{app_hint}\n"
        "Выберите нужный раздел в меню 👇",
        reply_markup=saas_main_kb(),
    )


@router.message(F.text == "👤 Личный кабинет и Квоты")
async def menu_cabinet(message: Message) -> None:
    u, _ = db.get_or_create_user(message.from_user.id)
    p = PLANS.get(u["plan"], PLANS["trial"])
    until_str = f" (до {u['plan_until']})" if u.get("plan_until") else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить профессию для поиска", callback_data="cab:query")],
            [InlineKeyboardButton(text="📥 Загрузить базовое резюме (PDF / Текст)", callback_data="cab:resume")],
            [InlineKeyboardButton(text="💳 Улучшить тариф / Запустить подарочный день", callback_data="cab:plans")],
        ]
    )
    await message.answer(
        f"👤 <b>Личный кабинет · ID {u['user_id']}</b>\n\n"
        f"• <b>Тариф:</b> {p['name']}{until_str}\n"
        f"• <b>Бонусный счёт:</b> {u['balance_rub']} ₽ | <b>Подарочных дней:</b> {u['gift_days']}\n"
        f"• <b>Приглашено друзей:</b> {u['referrals_count']}\n\n"
        f"📊 <b>Использовано квот сегодня ({u['usage_date']}):</b>\n"
        f"• Мульти-поисков: <b>{u['used_searches']} / {p['daily_searches']}</b>\n"
        f"• ИИ-резюме и писем: <b>{u['used_ai']} / {p['daily_ai']}</b>\n"
        f"• СБ-проверок работодателей: <b>{u['used_scam']} / {p['daily_scam_checks']}</b>\n\n"
        f"🎯 <b>Профессия поиска:</b> <code>{u['search_query']}</code>",
        reply_markup=kb,
    )


@router.message(F.text == "💳 Тарифы и Бонусы (+750 ₽)")
async def menu_plans(message: Message) -> None:
    u, _ = db.get_or_create_user(message.from_user.id)
    await message.answer(
        "💳 <b>Тарифы и Подарочные дни (Монетизация SaaS)</b>\n\n"
        "⚡ <b>Fast (7 дней) — 690 ₽</b>\n"
        "До 100 поисков в сутки, 50 ИИ-писем и адаптаций резюме, 30 СБ-проверок компаний в сутки.\n\n"
        "💪 <b>Strong (30 дней) — 1 490 ₽</b>\n"
        "Оптимальный выбор: месяц активного поиска по всем площадкам Рунета, до 200 ИИ-писем и 150 СБ-проверок в день.\n\n"
        "👑 <b>Max VIP (30 дней) — 2 490 ₽</b>\n"
        "Полный безлимит на все функции, приоритетные ИИ-модели, ИИ-разбор резюме и авто-анкеты.\n\n"
        f"💰 <b>Ваш бонусный баланс:</b> {u['balance_rub']} ₽ (списывается автоматически при покупке!)",
        reply_markup=saas_plans_kb(u),
    )


@router.callback_query(F.data == "cab:plans")
async def cb_open_plans(call: CallbackQuery) -> None:
    u, _ = db.get_or_create_user(call.from_user.id)
    await call.message.answer("💳 <b>Выберите тариф или активируйте бонус:</b>", reply_markup=saas_plans_kb(u))
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_plan(call: CallbackQuery) -> None:
    plan_key = call.data.split(":")[1]
    plan = PLANS.get(plan_key)
    if not plan:
        return
    u, _ = db.get_or_create_user(call.from_user.id)
    discount = min(u["balance_rub"], plan["price_rub"] // 2)  # До 50% оплата бонусами
    final_price = plan["price_rub"] - discount

    # В демо-режиме / MVP сразу активируем тариф (при подключении ЮKassa / Telegram Stars сюда ставится send_invoice)
    if discount > 0:
        db.update_field(call.from_user.id, "balance_rub", u["balance_rub"] - discount)
    updated = db.activate_plan(call.from_user.id, plan_key, amount_rub=final_price)

    await call.message.edit_text(
        f"✅ <b>Тариф «{plan['name']}» успешно подключён!</b>\n\n"
        f"• Списано бонусов: {discount} ₽\n"
        f"• Сумма транзакции: {final_price} ₽\n"
        f"• Действует до: <b>{updated['plan_until']}</b>\n\n"
        "Все расширенные лимиты поиска, ИИ-редактора резюме и СБ-проверки уже активны!"
    )
    await call.answer("Тариф активирован!")


@router.callback_query(F.data == "gift:activate")
async def cb_gift_activate(call: CallbackQuery) -> None:
    ok, msg = db.activate_gift_day(call.from_user.id)
    await call.answer(msg, show_alert=True)
    if ok:
        u, _ = db.get_or_create_user(call.from_user.id)
        await call.message.edit_reply_markup(reply_markup=saas_plans_kb(u))


@router.callback_query(F.data == "ref:show")
async def cb_ref_show(call: CallbackQuery, bot: Bot) -> None:
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    await call.message.answer(
        "🤝 <b>Партнёрская программа (как в Сизифе: +750 ₽ и подарочный день!)</b>\n\n"
        "Отправьте эту ссылку друзьям, которые ищут работу:\n"
        f"<code>{ref_link}</code>\n\n"
        f"За каждого друга вы мгновенно получаете <b>+{REFERRAL_BONUS_RUB} ₽</b> на бонусный счёт и <b>+1 подарочный день</b> тарифа Strong!"
    )
    await call.answer()


# ── ПОИСК ВАКАНСИЙ С УЧЁТОМ КВОТЫ ТАРИФА ─────────────────────────────────────

@router.message(F.text == "🔎 Найти вакансии (Все площадки)")
async def saas_search(message: Message) -> None:
    uid = message.from_user.id
    ok, err_msg = db.check_and_consume_quota(uid, "search")
    if not ok:
        u, _ = db.get_or_create_user(uid)
        await message.answer(err_msg, reply_markup=saas_plans_kb(u))
        return

    u, _ = db.get_or_create_user(uid)
    w = await message.answer(
        f"🔎 <b>Ищем вакансии по запросу «{u['search_query']}» на всех площадках Рунета...</b>\n"
        "Проверяем HH.ru, Работа России (ФНС ИНН), Telegram-каналы, Авито, ВК, ОК, LinkedIn..."
    )
    items = await search_all_platforms(u["search_query"], city=u["city"], min_salary=u["min_salary"])
    if not items:
        await w.edit_text("По вашему запросу ничего не найдено. Смените профессию в Личном кабинете.")
        return

    USER_DECKS[uid] = {"items": items, "idx": 0}
    first = items[0]
    await w.edit_text(
        f"🗂 <b>Вакансия 1 из {len(items)}</b> | <b>{first.platform}</b>\n"
        f"🛡 <b>Антифрод:</b> {first.trust_badge}\n\n"
        f"💼 <b>{first.title}</b>\n"
        f"🏢 {first.company}\n"
        f"💰 <b>{first.salary}</b>\n\n"
        f"{first.description[:450]}",
        reply_markup=saas_deck_kb(0, len(items), first.url),
        disable_web_page_preview=True,
    )


@router.message(F.text == "🌐 Соцсети (Reddit, Discord, HN…)")
async def saas_social_search(message: Message) -> None:
    uid = message.from_user.id
    ok, err_msg = db.check_and_consume_quota(uid, "search")
    if not ok:
        u, _ = db.get_or_create_user(uid)
        await message.answer(err_msg, reply_markup=saas_plans_kb(u))
        return

    u, _ = db.get_or_create_user(uid)
    query = u.get("search_query") or "Специалист"
    w = await message.answer(
        f"🌐 <b>Облетаю соцсети и комьюнити по запросу «{query}»…</b>\n"
        "Telegram, Reddit, Discord, Bluesky, Mastodon, Hacker News, RemoteOK + шлюзы в закрытые сети."
    )
    res = await search_social_networks(query, city=u.get("city") or "", limit_per_network=2)
    lines = [f"🌐 <b>Соцсети и комьюнити по запросу «{query}»:</b>\n"]
    live = res.get("items") or []
    if live:
        lines.append(f"✅ <b>Живые находки ({len(live)}):</b>")
        for v in live[:8]:
            lines.append(f"• <b>{v['platform']}</b> — {str(v['title'])[:70]}\n  {v['url']}")
    hubs = res.get("hubs") or []
    if hubs:
        lines.append("\n🔗 <b>Шлюзы в закрытые сети:</b>")
        for h in hubs[:6]:
            lines.append(f"• <b>{h['platform']}</b> — {h['url']}")
    if not live and not hubs:
        lines.append("😕 В соцсетях пока пусто. Смените профессию в Личном кабинете.")
    lines.append("\n💡 <i>Совет: живые посты уже проверены антифродом, но прямые ссылки всегда открывайте внимательно.</i>")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n… (обрезано)"
    await w.edit_text(text, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("sdeck:"))
async def cb_sdeck(call: CallbackQuery) -> None:
    uid = call.from_user.id
    deck = USER_DECKS.get(uid)
    if not deck or not deck.get("items"):
        await call.answer("Обновите поиск вакансий", show_alert=True)
        return
    parts = call.data.split(":")
    act = parts[1]
    if act == "noop":
        await call.answer()
        return
    items: List[Vacancy] = deck["items"]
    idx = max(0, min(len(items) - 1, int(parts[2])))
    v = items[idx]
    u, _ = db.get_or_create_user(uid)

    if act == "nav":
        await call.message.edit_text(
            f"🗂 <b>Вакансия {idx + 1} из {len(items)}</b> | <b>{v.platform}</b>\n"
            f"🛡 <b>Антифрод:</b> {v.trust_badge}\n\n"
            f"💼 <b>{v.title}</b>\n"
            f"🏢 {v.company}\n"
            f"💰 <b>{v.salary}</b>\n\n"
            f"{v.description[:450]}",
            reply_markup=saas_deck_kb(idx, len(items), v.url),
            disable_web_page_preview=True,
        )
        await call.answer()

    elif act in ("letter", "adapt"):
        ok, err_msg = db.check_and_consume_quota(uid, "ai")
        if not ok:
            await call.message.answer(err_msg, reply_markup=saas_plans_kb(u))
            await call.answer()
            return
        res_text = u.get("resume_text") or f"Профильный кандидат: {u['search_query']}"
        vac_info = f"{v.title} в {v.company}. {v.description}"
        if act == "letter":
            await call.answer("ИИ генерирует сопроводительное письмо...")
            let = await generate_cover_letter(res_text, vac_info, tone="natural", platform=v.platform)
            await call.message.answer(f"✉️ <b>Готовое письмо под «{v.title}»:</b>\n\n<code>{let}</code>")
        else:
            await call.answer("ИИ адаптирует резюме под вакансию...")
            ad = await adapt_resume_for_vacancy(res_text, vac_info, mode="ats")
            fc = verify_resume_facts(res_text, ad, vac_info)
            await call.message.answer(
                f"🎯 <b>Адаптация резюме под «{v.title}»:</b>\n"
                f"🛡 <b>Сверка фактов:</b> {fc['badge']}\n\n{ad}"
            )

    elif act == "scam":
        ok, err_msg = db.check_and_consume_quota(uid, "scam")
        if not ok:
            await call.message.answer(err_msg, reply_markup=saas_plans_kb(u))
            await call.answer()
            return
        await call.answer("Проводим СБ-проверку...")
        rep = await deep_check_employer(f"{v.company} {v.inn} {v.title} {v.description}")
        links_html = "\n".join(f"• <a href='{url}'>{name}</a>" for name, url in rep["links"].items())
        flags_html = "\n".join(f"• {f}" for f in rep["flags"])
        await call.message.answer(
            f"🛡 <b>СБ-досье на компанию «{v.company}»</b>\n"
            f"<b>Вердикт:</b> {rep['verdict']} ({rep['score']}%)\n\n"
            f"{flags_html}\n\n"
            f"<b>🧠 Анализ ИИ:</b>\n{rep['ai_analysis']}\n\n"
            f"<b>🔗 Реестры:</b>\n{links_html}",
            disable_web_page_preview=True,
        )


# ── ОТДЕЛЬНЫЕ РАЗДЕЛЫ МЕНЮ SaaS ──────────────────────────────────────────────

@router.message(F.text == "🛡 Пробить работодателя (СБ)")
async def saas_scam_btn(message: Message, state: FSMContext) -> None:
    await state.set_state(SaaSForm.waiting_scam_target)
    await message.answer(
        "🛡 <b>СБ-проверка работодателя или текста вакансии</b>\n\n"
        "Пришлите ИНН, название юрлица или подозрительный текст вакансии с Авито / ВК / Telegram:"
    )


@router.message(SaaSForm.waiting_scam_target)
async def saas_scam_run(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    ok, err_msg = db.check_and_consume_quota(uid, "scam")
    if not ok:
        u, _ = db.get_or_create_user(uid)
        await message.answer(err_msg, reply_markup=saas_plans_kb(u))
        return
    w = await message.answer("🕵️‍♂️ <b>Проверяем по базам и маркерам мошенничества...</b>")
    rep = await deep_check_employer(message.text or "")
    links_html = "\n".join(f"• <a href='{url}'>{name}</a>" for name, url in rep["links"].items())
    flags_html = "\n".join(f"• {f}" for f in rep["flags"])
    await w.edit_text(
        f"🛡 <b>СБ-Отчёт ({rep['score']}%) — {rep['verdict']}</b>\n\n"
        f"{flags_html}\n\n"
        f"<b>🧠 Экспертиза ИИ:</b>\n{rep['ai_analysis']}\n\n"
        f"<b>🔗 Прямые ссылки на госреестры:</b>\n{links_html}",
        disable_web_page_preview=True,
    )


@router.message(F.text == "📄 ИИ-Резюме и Сопроводительные")
async def saas_resume_menu(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Загрузить моё базовое резюме (PDF / Текст)", callback_data="cab:resume")],
            [InlineKeyboardButton(text="🧬 Создать 5 A/B-копий резюме (без выдумок)", callback_data="saas:ab_gen")],
            [InlineKeyboardButton(text="🎯 Адаптировать резюме под текст вакансии", callback_data="saas:adapt")],
            [InlineKeyboardButton(text="✉️ Сгенерировать сопроводительное письмо", callback_data="saas:letter")],
        ]
    )
    await message.answer("📄 <b>ИИ-инструменты упаковки соискателя (с защитой фактов и A/B-копиями):</b>", reply_markup=kb)


@router.callback_query(F.data == "saas:ab_gen")
async def cb_saas_ab_gen(call: CallbackQuery) -> None:
    uid = call.from_user.id
    u, _ = db.get_or_create_user(uid)
    res_text = (u.get("resume_text") or "").strip()
    if len(res_text) < 30:
        await call.answer("Сначала загрузите базовое резюме!", show_alert=True)
        return
    ok, err_msg = db.check_and_consume_quota(uid, "ai")
    if not ok:
        await call.message.answer(err_msg, reply_markup=saas_plans_kb(u))
        await call.answer()
        return
    await call.answer("Собираю 5 A/B-копий резюме...")
    w = await call.message.answer("🧬 <b>Создаю 5 целевых A/B-копий резюме со сверкой фактов...</b>")
    role = u.get("search_query") or "Специалист"
    lines = [f"🧬 <b>Готовы 5 A/B-копий резюме под роль «{role}»:</b>\n"]
    for fk in RESUME_FOCUSES:
        built = await build_resume_variant(res_text, focus=fk, target_role=role, use_llm=True)
        lines.append(
            f"• <b>{built['label']}</b> — <code>{built['title']}</code>\n"
            f"  🛡 <i>{built['fact_badge']}</i>"
        )
    await w.edit_text("\n".join(lines))


@router.callback_query(F.data == "cab:query")
async def cb_set_q(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SaaSForm.waiting_query)
    await call.message.answer("🎯 Введите желаемую должность / специализацию для поиска:")
    await call.answer()


@router.message(SaaSForm.waiting_query)
async def save_saas_q(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text:
        db.update_field(message.from_user.id, "search_query", message.text.strip())
    await message.answer(f"✅ Специализация сохранена: <b>{message.text}</b>")


@router.callback_query(F.data == "cab:resume")
async def cb_set_res(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SaaSForm.waiting_resume)
    await call.message.answer("📥 Отправьте PDF-файл резюме или вставьте текст резюме сообщением:")
    await call.answer()


@router.message(SaaSForm.waiting_resume, F.document)
async def save_saas_pdf(message: Message, state: FSMContext, bot: Bot) -> None:
    file = await bot.get_file(message.document.file_id)
    buf = await bot.download_file(file.file_path)
    text = extract_text_from_pdf_bytes(buf.read())
    if len(text) < 20:
        await message.answer("⚠️ Не удалось прочитать PDF. Пришлите резюме текстом.")
        return
    db.update_field(message.from_user.id, "resume_text", text)
    await state.clear()
    await message.answer(f"✅ Резюме из PDF сохранено ({len(text)} симв.)!")


@router.message(SaaSForm.waiting_resume, F.text)
async def save_saas_txt(message: Message, state: FSMContext) -> None:
    db.update_field(message.from_user.id, "resume_text", (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Ваше резюме сохранено в профиле!")


@router.callback_query(F.data == "saas:adapt")
async def cb_saas_adapt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SaaSForm.waiting_adapt_vacancy)
    await call.message.answer("🎯 Пришлите описание вакансии, под которую нужно адаптировать ваше резюме:")
    await call.answer()


@router.message(SaaSForm.waiting_adapt_vacancy)
async def run_saas_adapt(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    ok, err_msg = db.check_and_consume_quota(uid, "ai")
    if not ok:
        u, _ = db.get_or_create_user(uid)
        await message.answer(err_msg, reply_markup=saas_plans_kb(u))
        return
    u, _ = db.get_or_create_user(uid)
    w = await message.answer("⏳ <b>ИИ пересобирает акценты вашего резюме...</b>")
    res = await adapt_resume_for_vacancy(u.get("resume_text") or u["search_query"], message.text or "")
    await w.edit_text(res)


@router.callback_query(F.data == "saas:letter")
async def cb_saas_letter(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SaaSForm.waiting_letter_vacancy)
    await call.message.answer("✉️ Пришлите текст вакансии или поста, чтобы сгенерировать сопроводительное письмо:")
    await call.answer()


@router.message(SaaSForm.waiting_letter_vacancy)
async def run_saas_letter(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    ok, err_msg = db.check_and_consume_quota(uid, "ai")
    if not ok:
        u, _ = db.get_or_create_user(uid)
        await message.answer(err_msg, reply_markup=saas_plans_kb(u))
        return
    u, _ = db.get_or_create_user(uid)
    w = await message.answer("✍️ <b>Генерируем сопроводительное письмо...</b>")
    let = await generate_cover_letter(u.get("resume_text") or u["search_query"], message.text or "")
    await w.edit_text(f"✉️ <b>Готовый отклик:</b>\n\n<code>{let}</code>")


# ── АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА СЕРВИСА (/admin) ──────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    st = db.get_admin_stats()
    await message.answer(
        "👑 <b>Админ-панель владельца SaaS-сервиса</b>\n\n"
        f"• Всего пользователей в базе: <b>{st['total_users']}</b>\n"
        f"• Активных платных подписок: <b>{st['paid_users']}</b>\n"
        f"• Выручка (оплаты): <b>{st['revenue']} ₽</b>\n"
        f"• Поисковых запросов сегодня: <b>{st['searches_today']}</b>\n\n"
        "Команды управления:\n"
        "• <code>/grant ID_пользователя max</code> — выдать тариф вручную"
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in PLANS:
        await message.answer("Использование: <code>/grant USER_ID fast|strong|max</code>")
        return
    target_id = int(parts[0])
    plan_key = parts[1]
    db.activate_plan(target_id, plan_key, amount_rub=0)
    await message.answer(f"✅ Пользователю <code>{target_id}</code> выдан тариф <b>{PLANS[plan_key]['name']}</b>.")


async def _run_webhook(bot: Bot, dp: Dispatcher, webhook_url: str) -> None:
    """Запуск в webhook-режиме (надёжнее long polling на нестабильной сети).

    Требует публичный HTTPS-адрес (обратный прокси/туннель), который Telegram
    будет вызывать. Настройки берутся из env:
      WEBHOOK_URL    — полный публичный URL (напр. https://домен/webhook)
      WEBHOOK_PATH   — локальный путь-приёмник (дефолт /webhook)
      WEBHOOK_HOST   — адрес прослушивания (дефолт 0.0.0.0)
      WEBHOOK_PORT   — порт (дефолт 8080)
      WEBHOOK_SECRET — необязательный секрет X-Telegram-Bot-Api-Secret-Token
    """
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_PORT", "8080"))
    path = os.environ.get("WEBHOOK_PATH", "/webhook")
    secret = os.environ.get("WEBHOOK_SECRET", "").strip() or None

    await bot.set_webhook(webhook_url, secret_token=secret, drop_pending_updates=True)
    logging.info(
        "Webhook установлен: %s (secret: %s)",
        webhook_url, "задан" if secret else "не задан",
    )

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.info("Webhook-сервер слушает http://%s:%s%s", host, port, path)
    print("💼 Вариант Б (Commercial SaaS Job Hunter Bot) запущен в webhook-режиме!")

    try:
        # Держим процесс живым: aiohttp-сервер крутится в текущем event loop.
        await asyncio.Event().wait()
    finally:
        await bot.delete_webhook(drop_pending_updates=True)
        await runner.cleanup()
        logging.info("Webhook удалён, сервер остановлен.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    token = os.environ.get("SAAS_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token:
        print(
            "\n[!] ОШИБКА: Не задан токен Telegram-бота для SaaS-версии!\n"
            "Задайте переменную окружения SAAS_BOT_TOKEN (или BOT_TOKEN) от @BotFather:\n"
            "PowerShell:  $env:SAAS_BOT_TOKEN='ваш_токен'; python bot_saas.py\n"
        )
        return

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        await _run_webhook(bot, dp, webhook_url)
    else:
        print("💼 Вариант Б (Commercial SaaS Job Hunter Bot) запущен и готов к работе!")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
