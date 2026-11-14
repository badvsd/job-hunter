# -*- coding: utf-8 -*-
"""
ВАРИАНТ А: ЛИЧНЫЙ ИИ-БОТ ДЛЯ ПОИСКА РАБОТЫ И ПРОВЕРКИ РАБОТОДАТЕЛЕЙ (Personal Edition).
Создан специально «для себя» — полный безлимит, без пейволлов и подписок.

Функции:
1. 🔎 Мульти-поиск по всему Рунету (HH.ru, Работа России с ИНН, Telegram-каналы в реальном времени,
   Авито Работа, ВКонтакте, Одноклассники, Habr Career, SuperJob, LinkedIn, Instagram).
2. 🃏 Колода карточек (в стиле Сизифа): листание вакансий, мгновенный антифрод-скоринг каждой вакансии,
   1-клик генерация сопроводительного письма/питча и 1-клик адаптация резюме под открытую вакансию.
3. 📄 Моё резюме + ИИ-Редактор: загрузка PDF или текста, переупаковка под вакансию (ATS / Цифры / Аудит).
4. ✉️ Генератор сопроводительных писем в 3 тонах (Деловой / Живой как в Сизифе / Короткий питч для TG, ВК и Авито).
5. 🛡 Антифрод и СБ-проверка работодателя: проверка ИНН (контрольные разряды ФНС), проверка верификации на HH,
   детектор 15+ мошеннических схем (дроппинг, залоги, платное обучение, обратный выкуп), ссылки в ЕГРЮЛ/Суды/ФССП.
6. 🤖 Фоновый Автопилот-мониторинг: бот сам присылает новые безопасные вакансии по вашему профилю.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
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
    GEO_CONFIG,
    GEO_ORDER,
    RESUME_FOCUSES,
    Vacancy,
    adapt_resume_for_vacancy,
    build_resume_variant,
    deep_check_employer,
    extract_text_from_pdf_bytes,
    generate_cover_letter,
    geo_label,
    pick_best_resume_variant,
    search_all_platforms,
    search_hh,
    search_social_networks,
    search_telegram_channels,
    search_trudvsem,
    verify_resume_facts,
)
from shared_engine.miniapp import webapp_url
from shared_engine.paths import data_path

# ─────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ ЛИЧНОГО БОТА (SQLite)
# Хранится в папке данных (DATA_DIR), чтобы переживала перезапуски на хостинге.
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = data_path("personal_hunter.db")


class PersonalDB:
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
                CREATE TABLE IF NOT EXISTS profile (
                    user_id INTEGER PRIMARY KEY,
                    resume_text TEXT DEFAULT '',
                    search_query TEXT DEFAULT 'Python разработчик',
                    city TEXT DEFAULT 'Москва',
                    min_salary INTEGER DEFAULT 0,
                    tone TEXT DEFAULT 'natural',
                    tg_channels TEXT DEFAULT '',
                    autopilot INTEGER DEFAULT 0,
                    geo TEXT DEFAULT 'ru'
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_vacancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    vac_id TEXT,
                    platform TEXT,
                    title TEXT,
                    company TEXT,
                    salary TEXT,
                    url TEXT,
                    trust_badge TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Мягкая миграция: профиль из Telegram Mini App
            cols = {row["name"] for row in c.execute("PRAGMA table_info(profile)").fetchall()}
            for column, ddl in (
                ("tg_username", "TEXT DEFAULT ''"),
                ("tg_name", "TEXT DEFAULT ''"),
                ("geo", "TEXT DEFAULT 'ru'"),
            ):
                if column not in cols:
                    c.execute(f"ALTER TABLE profile ADD COLUMN {column} {ddl}")

            # До 5 A/B-копий резюме под разные направления и акценты (цифры, стек, команда, продукт, ATS)
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS resume_variants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot INTEGER NOT NULL,
                    focus TEXT DEFAULT 'ats',
                    title TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    fact_ok INTEGER DEFAULT 1,
                    fact_badge TEXT DEFAULT '',
                    used_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, slot)
                )
                """
            )

    def get_profile(self, user_id: int) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                default_ch = ",".join(DEFAULT_TG_CHANNELS)
                c.execute(
                    "INSERT INTO profile (user_id, tg_channels) VALUES (?, ?)",
                    (user_id, default_ch),
                )
                row = c.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row)

    def update_field(self, user_id: int, field: str, value: Any) -> None:
        self.get_profile(user_id)
        with self._conn() as c:
            c.execute(f"UPDATE profile SET {field} = ? WHERE user_id = ?", (value, user_id))

    def save_vacancy(self, user_id: int, v: Vacancy) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO saved_vacancies (user_id, vac_id, platform, title, company, salary, url, trust_badge)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, v.id, v.platform, v.title, v.company, v.salary, v.url, v.trust_badge),
            )

    def get_saved(self, user_id: int, limit: int = 15) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM saved_vacancies WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_resume_variants(self, user_id: int) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM resume_variants WHERE user_id = ? ORDER BY slot ASC LIMIT 5",
                (user_id,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["fact_ok"] = bool(d.get("fact_ok", 1))
                out.append(d)
            return out

    def upsert_resume_variant(
        self,
        user_id: int,
        slot: int,
        focus: str,
        title: str,
        content: str,
        fact_ok: bool = True,
        fact_badge: str = "",
    ) -> Dict[str, Any]:
        slot = max(1, min(5, int(slot or 1)))
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO resume_variants (user_id, slot, focus, title, content, fact_ok, fact_badge, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, slot) DO UPDATE SET
                    focus = excluded.focus,
                    title = excluded.title,
                    content = excluded.content,
                    fact_ok = excluded.fact_ok,
                    fact_badge = excluded.fact_badge,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, slot, focus, title, content, 1 if fact_ok else 0, fact_badge),
            )
            row = c.execute(
                "SELECT * FROM resume_variants WHERE user_id = ? AND slot = ?",
                (user_id, slot),
            ).fetchone()
            d = dict(row)
            d["fact_ok"] = bool(d.get("fact_ok", 1))
            return d

    def delete_resume_variant(self, user_id: int, slot: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM resume_variants WHERE user_id = ? AND slot = ?",
                (user_id, int(slot)),
            )
            return cur.rowcount > 0

    def bump_variant_used(self, user_id: int, slot: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE resume_variants SET used_count = used_count + 1 WHERE user_id = ? AND slot = ?",
                (user_id, int(slot)),
            )


db = PersonalDB()

# Оперативный кэш найденных колод вакансий: user_id -> {"items": List[Vacancy], "idx": int}
USER_DECKS: Dict[int, Dict[str, Any]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# СОСТОЯНИЯ FSM
# ─────────────────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    waiting_search_query = State()
    waiting_resume = State()
    waiting_target_vacancy_for_resume = State()
    waiting_target_vacancy_for_letter = State()
    waiting_scam_check = State()
    waiting_city = State()
    waiting_min_salary = State()
    waiting_tg_channels = State()
    waiting_social_query = State()


# ─────────────────────────────────────────────────────────────────────────────
# КЛАВИАТУРЫ
# ─────────────────────────────────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(text="🔎 Искать везде (Мульти-поиск)"),
            KeyboardButton(text="🛡 Проверить работодателя (Антифрод)"),
        ],
        [
            KeyboardButton(text="📄 Моё резюме и ИИ-адаптация"),
            KeyboardButton(text="✉️ Написать сопроводительное"),
        ],
        [
            KeyboardButton(text="⭐ Избранные вакансии"),
            KeyboardButton(text="⚙️ Настройки поиска и Автопилот"),
        ],
        [
            KeyboardButton(text="🌐 Поиск в соцсетях и комьюнити"),
        ],
    ]
    app_url = webapp_url("personal")
    if app_url:
        rows.append([KeyboardButton(text="🕊 Открыть приложение", web_app=WebAppInfo(url=app_url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def search_sources_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Искать сразу по ВСЕМ площадкам Рунета", callback_data="src:all")],
            [
                InlineKeyboardButton(text="🅰️ Только HH.ru", callback_data="src:hh"),
                InlineKeyboardButton(text="✈️ Только Telegram-каналы", callback_data="src:tg"),
            ],
            [
                InlineKeyboardButton(text="🏛 Работа России (с ИНН)", callback_data="src:tv"),
                InlineKeyboardButton(text="🌐 Авито / ВК / ОК / LinkedIn / Insta", callback_data="src:hubs"),
            ],
            [InlineKeyboardButton(text="✏️ Изменить профессию / запрос", callback_data="set:query")],
        ]
    )


def deck_card_kb(idx: int, total: int, url: str) -> InlineKeyboardMarkup:
    prev_idx = max(0, idx - 1)
    next_idx = min(total - 1, idx + 1)
    nav_row = []
    if idx > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"deck:nav:{prev_idx}"))
    nav_row.append(InlineKeyboardButton(text=f"{idx + 1} из {total}", callback_data="deck:noop"))
    if idx < total - 1:
        nav_row.append(InlineKeyboardButton(text="Дальше ➡️", callback_data=f"deck:nav:{next_idx}"))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav_row,
            [
                InlineKeyboardButton(text="✉️ Письмо под вакансию", callback_data=f"deck:letter:{idx}"),
                InlineKeyboardButton(text="🎯 Адаптировать резюме", callback_data=f"deck:adapt:{idx}"),
            ],
            [
                InlineKeyboardButton(text="🛡 Пробить компанию (СБ)", callback_data=f"deck:scam:{idx}"),
                InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"deck:save:{idx}"),
            ],
            [InlineKeyboardButton(text="🔗 Открыть вакансию / Написать HR ↗", url=url)],
        ]
    )


def geo_picker_kb(current: str = "ru") -> InlineKeyboardMarkup:
    rows = []
    for key in GEO_ORDER:
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{GEO_CONFIG[key]['label']}", callback_data=f"geo:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(prof: Dict[str, Any]) -> InlineKeyboardMarkup:
    tone_map = {"natural": "Живой (Сизиф)", "pro": "Строго деловой", "short_tg": "Короткий питч (TG/ВК/Авито)"}
    ap_status = "🟢 ВКЛ" if prof.get("autopilot") else "⚪ ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🎯 Запрос: {prof['search_query']}", callback_data="set:query")],
            [
                InlineKeyboardButton(text=f"📍 Город: {prof['city']}", callback_data="set:city"),
                InlineKeyboardButton(text=f"💰 Мин. ЗП: {prof['min_salary']} ₽", callback_data="set:salary"),
            ],
            [InlineKeyboardButton(text=f"🌍 Гео-регион: {geo_label(prof.get('geo') or 'ru')}", callback_data="set:geo")],
            [InlineKeyboardButton(text=f"🗣 Тон писем: {tone_map.get(prof['tone'], 'Живой')}", callback_data="set:tone")],
            [InlineKeyboardButton(text="📡 Список Telegram-каналов для парсинга", callback_data="set:channels")],
            [InlineKeyboardButton(text=f"🤖 Фоновый Автопилот: {ap_status}", callback_data="set:autopilot")],
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ БОТА
# ─────────────────────────────────────────────────────────────────────────────

router = Router()


def format_vacancy_card(v: Vacancy, idx: int, total: int) -> str:
    flags_block = ""
    if v.trust_flags:
        flags_block = "\n\n<b>⚠️ Флаги безопасности:</b>\n" + "\n".join(f"• {f}" for f in v.trust_flags)

    inn_line = f"\n<b>ИНН юрлица:</b> {v.inn}" if v.inn else ""
    contact_line = f"\n<b>Контакт:</b> {v.contact}" if v.contact else ""
    city_line = f" ({v.city})" if v.city else ""

    return (
        f"🗂 <b>Карточка {idx + 1} из {total}</b>  |  <b>{v.platform}</b>\n"
        f"🛡 <b>Доверие:</b> {v.trust_badge}\n\n"
        f"💼 <b>{v.title}</b>\n"
        f"🏢 <b>Компания:</b> {v.company}{city_line}{inn_line}{contact_line}\n"
        f"💰 <b>Зарплата:</b> {v.salary}\n\n"
        f"📝 <b>Описание:</b>\n{v.description[:500]}"
        f"{flags_block}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    prof = db.get_profile(message.from_user.id)
    has_resume = "✅ Загружено" if len(prof.get("resume_text") or "") > 30 else "⚠️ Ещё не загружено"
    app_hint = (
        "\n🕊 <b>Mini App:</b> нажмите «Открыть приложение» на клавиатуре — тот же движок в уютном мобильном интерфейсе.\n"
        if webapp_url("personal")
        else ""
    )
    await message.answer(
        "👋 <b>Добро пожаловать в ваш личный ИИ-комбайн поиска работы (Вариант А — Personal Edition)!</b>\n\n"
        "Это ваш персональный аналог <b>Сизифа (siziph.ru)</b> с расширением на все площадки Рунета и встроенной Службой Безопасности:\n\n"
        "• 🔎 <b>Мульти-поиск:</b> HH.ru, Работа России (с ИНН), Telegram-каналы, Авито, ВК, Одноклассники, Habr, SuperJob, LinkedIn, Instagram.\n"
        "• 📄 <b>ИИ-редактор резюме:</b> точечная адаптация вашего резюме под конкретную вакансию без выдумывания фактов.\n"
        "• ✉️ <b>Умные сопроводительные:</b> живые отклики и короткие питчи для мессенджеров.\n"
        "• 🛡 <b>Антифрод-проверка работодателя:</b> проверка ИНН, судов, отзывов и детектор скам-схем (дроппинг, залоги, фейки).\n\n"
        f"📌 <b>Текущий поисковый запрос:</b> {prof['search_query']} ({prof['city']})\n"
        f"📄 <b>Базовое резюме:</b> {has_resume}\n"
        f"{app_hint}\n"
        "Выберите действие на клавиатуре ниже 👇",
        reply_markup=main_menu_kb(),
    )


# ── 1. МУЛЬТИ-ПОИСК ВАКАНСИЙ ─────────────────────────────────────────────────

@router.message(F.text == "🔎 Искать везде (Мульти-поиск)")
@router.message(Command("search"))
async def menu_search(message: Message) -> None:
    prof = db.get_profile(message.from_user.id)
    await message.answer(
        f"🔎 <b>Мультиплатформенный поиск вакансий</b>\n\n"
        f"Текущий запрос: <b>{prof['search_query']}</b>\n"
        f"Город: <b>{prof['city']}</b> | Мин. ЗП: <b>{prof['min_salary']} ₽</b>\n\n"
        "Где запускаем поиск?",
        reply_markup=search_sources_kb(),
    )


@router.callback_query(F.data.startswith("src:"))
async def cb_run_search(call: CallbackQuery) -> None:
    src = call.data.split(":")[1]
    uid = call.from_user.id
    prof = db.get_profile(uid)
    q = prof["search_query"]
    city = prof["city"]
    sal = int(prof.get("min_salary") or 0)
    channels = [c.strip().lstrip("@") for c in (prof.get("tg_channels") or "").split(",") if c.strip()]
    geo = (prof.get("geo") or "ru").strip().lower() or "ru"
    gcfg = GEO_CONFIG.get(geo, GEO_CONFIG["ru"])
    global_mode = bool(gcfg.get("world") or gcfg.get("ngo"))

    await call.message.edit_text(
        f"⏳ <b>Запускаю парсеры и антифрод-фильтр по запросу «{q}»...</b>\n"
        f"🌍 Гео: {geo_label(geo)}. Опрашиваем площадки и проверяем работодателей..."
    )

    if src == "hh":
        hh_city = "удалённо" if global_mode else city
        items = await search_hh(q, city=hh_city, min_salary=sal, limit=10)
    elif src == "tg":
        items = await search_telegram_channels(q, channels=channels, limit=10)
    elif src == "tv":
        items = await search_trudvsem(f"{q} {city}".strip(), limit=10)
    elif src == "hubs":
        from shared_engine import generate_crossplatform_search_hubs, generate_global_hubs
        if global_mode:
            items = generate_global_hubs(q, geo=geo)
        else:
            items = generate_crossplatform_search_hubs(q, city=city)
    else:
        items = await search_all_platforms(q, city=city, min_salary=sal, tg_channels=channels, geo=geo)

    if not items:
        await call.message.edit_text(
            "😕 По этому запросу свежих вакансий не нашлось. Попробуйте упростить запрос в настройках.",
            reply_markup=search_sources_kb(),
        )
        return

    USER_DECKS[uid] = {"items": items, "idx": 0}
    first = items[0]
    await call.message.edit_text(
        format_vacancy_card(first, 0, len(items)),
        reply_markup=deck_card_kb(0, len(items), first.url),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("deck:"))
async def cb_deck_actions(call: CallbackQuery) -> None:
    uid = call.from_user.id
    deck = USER_DECKS.get(uid)
    if not deck or not deck.get("items"):
        await call.answer("Колода устарела, запустите поиск заново", show_alert=True)
        return

    parts = call.data.split(":")
    action = parts[1]
    items: List[Vacancy] = deck["items"]

    if action == "noop":
        await call.answer()
        return

    idx = int(parts[2])
    idx = max(0, min(len(items) - 1, idx))
    deck["idx"] = idx
    v = items[idx]
    prof = db.get_profile(uid)

    if action == "nav":
        await call.message.edit_text(
            format_vacancy_card(v, idx, len(items)),
            reply_markup=deck_card_kb(idx, len(items), v.url),
            disable_web_page_preview=True,
        )
        await call.answer()

    elif action == "save":
        db.save_vacancy(uid, v)
        await call.answer("⭐ Вакансия сохранена в Избранное!", show_alert=False)

    elif action == "letter":
        await call.answer("✍️ ИИ пишет сопроводительное письмо...", show_alert=False)
        resume_text = prof.get("resume_text") or f"Специалист по направлению {prof['search_query']}"
        vac_info = f"Должность: {v.title}\nКомпания: {v.company}\nОписание: {v.description}"
        variants = db.get_resume_variants(call.from_user.id)
        match = pick_best_resume_variant(variants, vacancy_title=v.title, vacancy_text=vac_info, base_resume=resume_text)
        best_var = match["best"]
        if variants and best_var.get("slot"):
            db.bump_variant_used(call.from_user.id, int(best_var["slot"]))
        eff_resume = f"{resume_text}\n\nАкцент резюме ({best_var['label']}):\n{best_var['content']}"
        letter = await generate_cover_letter(
            base_resume=eff_resume,
            vacancy_info=vac_info,
            tone=prof.get("tone", "natural"),
            platform=v.platform,
        )
        await call.message.answer(
            f"✉️ <b>Готовое сопроводительное письмо под «{v.title}» ({v.company}):</b>\n"
            f"🎯 <i>Автовыбор резюме: {best_var['label']} ({match['match_score']}% совпадение)</i>\n\n"
            f"{html.escape(letter)}\n\n"
            "💡 <i>Нажмите на текст письма, чтобы скопировать его в буфер обмена в 1 клик.</i>"
        )

    elif action == "adapt":
        await call.answer("🎯 ИИ подбирает лучшую A/B-копию и адаптирует резюме...", show_alert=False)
        resume_text = prof.get("resume_text")
        if not resume_text or len(resume_text) < 30:
            await call.message.answer(
                "⚠️ <b>Сначала добавьте ваше базовое резюме!</b>\n"
                "Нажмите в меню «📄 Моё резюме и ИИ-адаптация» и отправьте текст или PDF-файл резюме."
            )
            return
        vac_info = f"Должность: {v.title}\nКомпания: {v.company}\nТребования: {v.description}"
        variants = db.get_resume_variants(call.from_user.id)
        match = pick_best_resume_variant(variants, vacancy_title=v.title, vacancy_text=vac_info, base_resume=resume_text)
        best_var = match["best"]
        adapted = await adapt_resume_for_vacancy(resume_text, vac_info, mode="ats")
        fact_check = verify_resume_facts(resume_text, adapted, vac_info)
        await call.message.answer(
            f"🎯 <b>Адаптация вашего резюме под вакансию «{v.title}» ({v.company}):</b>\n"
            f"🏆 <b>Лучшая A/B-копия:</b> {best_var['label']} — {best_var['title']} ({match['match_score']}%)\n"
            f"🛡 <b>Сверка фактов:</b> {fact_check['badge']}\n\n{adapted}"
        )

    elif action == "scam":
        await call.answer("🛡 Запускаю СБ-проверку компании...", show_alert=False)
        check_target = f"{v.company} {v.inn} {v.title} {v.description}".strip()
        report = await deep_check_employer(check_target)
        links_html = "\n".join(f"• <a href='{u}'>{name}</a>" for name, u in report["links"].items())
        flags_html = "\n".join(f"• {fl}" for fl in report["flags"])
        await call.message.answer(
            f"🛡 <b>СБ-Отчёт по работодателю: {v.company}</b>\n\n"
            f"<b>Вердикт:</b> {report['verdict']}\n"
            f"<b>Рейтинг доверия (Trust Score):</b> <b>{report['score']}%</b>\n\n"
            f"<b>Факторы проверки:</b>\n{flags_html}\n\n"
            f"<b>🧠 Экспертиза ИИ-аудитора:</b>\n{report['ai_analysis']}\n\n"
            f"<b>🔗 Прямая проверка по госреестрам и отзывам:</b>\n{links_html}",
            disable_web_page_preview=True,
        )


# ── 🌐 СОЦСЕТИ И КОМЬЮНИТИ (Telegram, Reddit, Discord, HN, LinkedIn, Инста…) ──

@router.message(F.text == "🌐 Поиск в соцсетях и комьюнити")
async def menu_social_search(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_social_query)
    await message.answer(
        "🌐 <b>Поиск в соцсетях и комьюнити</b>\n\n"
        "Соберу свежие посты о найме: Telegram, Reddit, Discord, Bluesky, Mastodon, "
        "Hacker News, RemoteOK + умные шлюзы в закрытые сети (LinkedIn, Facebook, "
        "Instagram, X, ВКонтакте, ОК, Behance, Dribbble).\n\n"
        "✏️ <b>Введите запрос</b> — например: «Python разработчик» или «дизайнер удалённо»:"
    )


@router.message(Form.waiting_social_query)
async def process_social_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    query = (message.text or "").strip()
    if len(query) < 3:
        await message.answer("Слишком короткий запрос. Например: «Python разработчик».")
        return
    prof = db.get_profile(message.from_user.id)
    city = prof.get("city") or ""
    w = await message.answer(
        f"🌐 <b>Облетаю соцсети по запросу «{query}»…</b>\n"
        "Telegram, Reddit, Discord, Bluesky, Mastodon, Hacker News, RemoteOK + шлюзы в закрытые сети."
    )
    res = await search_social_networks(query, city=city, limit_per_network=2)
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
    geo = (prof.get("geo") or "ru").strip().lower()
    if geo in ("eu", "world", "ngo"):
        from shared_engine import generate_global_hubs
        ghubs = generate_global_hubs(query, geo=geo)
        if ghubs:
            lines.append("\n🌍 <b>Международные и НКО-площадки:</b>")
            for h in ghubs[:8]:
                lines.append(f"• <b>{h.platform}</b> — {h.url}")
    if not live and not hubs:
        lines.append("😕 Свежих вакансий в соцсетях пока не нашлось. Попробуйте другой запрос.")
    lines.append("\n💡 <i>Живые посты уже пропущены через антифрод-фильтр. Шлюзы открывайте с осторожностью.</i>")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n… (обрезано)"
    await w.edit_text(text, disable_web_page_preview=True)


# ── 2. АНТИФРОД И ПРОВЕРКА РАБОТОДАТЕЛЯ НА ОБМАН ─────────────────────────────

@router.message(F.text == "🛡 Проверить работодателя (Антифрод)")
@router.message(Command("check"))
async def menu_scam_check(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_scam_check)
    await message.answer(
        "🛡 <b>Служба Безопасности и Антифрод-проверка работодателя</b>\n\n"
        "Отправьте мне <b>любые данные</b> для пробива:\n"
        "• <b>ИНН</b> (10 или 12 цифр) или <b>название компании</b> (например: ООО Ромашка или 7736207543)\n"
        "• <b>Текст подозрительной вакансии</b> с Авито / ВК / Telegram\n"
        "• <b>Переписку с рекрутером</b> (перешлите сообщение сюда)\n\n"
        "Я проверю контрольную сумму ИНН ФНС, карточку на HH.ru, прогоню через детектор мошеннических схем (дроппинг, залоги, обратный выкуп, серые схемы) и подготовлю прямые ссылки на ЕГРЮЛ, Арбитражные суды и ФССП."
    )


@router.message(Form.waiting_scam_check)
async def process_scam_check(message: Message, state: FSMContext) -> None:
    await state.clear()
    wait_msg = await message.answer("🕵️‍♂️ <b>Провожу проверку по базам и антифрод-правилам...</b>")
    report = await deep_check_employer(message.text or "")
    links_html = "\n".join(f"• <a href='{u}'>{name}</a>" for name, u in report["links"].items())
    flags_html = "\n".join(f"• {fl}" for fl in report["flags"])
    await wait_msg.edit_text(
        f"🛡 <b>Результаты СБ-проверки работодателя / вакансии</b>\n\n"
        f"<b>Вердикт:</b> {report['verdict']}\n"
        f"<b>Рейтинг доверия (Trust Score):</b> <b>{report['score']}%</b>\n\n"
        f"<b>Маркеры и реестры:</b>\n{flags_html}\n\n"
        f"<b>🧠 Заключение ИИ-юриста и вопросы на собеседование:</b>\n{report['ai_analysis']}\n\n"
        f"<b>🔗 Досье в госреестрах и отзывы сотрудников:</b>\n{links_html}",
        disable_web_page_preview=True,
    )


# ── 3. МОЁ РЕЗЮМЕ И ИИ-АДАПТАЦИЯ ПОД ВАКАНСИЮ ────────────────────────────────

@router.message(F.text == "📄 Моё резюме и ИИ-адаптация")
@router.message(Command("resume"))
async def menu_resume(message: Message) -> None:
    prof = db.get_profile(message.from_user.id)
    res_text = prof.get("resume_text") or ""
    variants = db.get_resume_variants(message.from_user.id)
    preview = (res_text[:350] + "...") if len(res_text) > 350 else (res_text or "Резюме пока не загружено.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Загрузить / Обновить резюме (Текст или PDF)", callback_data="res:upload")],
            [InlineKeyboardButton(text="🧬 Создать 5 A/B-копий (ATS / Цифры / Стек / Команда / Продукт)", callback_data="res:ab_gen")],
            [InlineKeyboardButton(text=f"📊 Мои A/B-копии резюме ({len(variants)}/5) и сверка фактов", callback_data="res:ab_list")],
            [InlineKeyboardButton(text="🎯 Адаптировать резюме под конкретную вакансию", callback_data="res:adapt")],
            [InlineKeyboardButton(text="🔬 ИИ-разбор слабых мест резюме (как в Сизифе)", callback_data="res:audit")],
        ]
    )
    await message.answer(
        f"📄 <b>Управление резюме и A/B-оптимизация (ИИ-коуч)</b>\n\n"
        f"<b>Текущее базовое резюме ({len(res_text)} симв.):</b>\n"
        f"<i>{preview}</i>\n\n"
        f"🧬 <b>Активных A/B-копий:</b> <b>{len(variants)} из 5</b> (при отклике бот автоматически выбирает лучшую копию под вакансию).\n\n"
        "Выберите действие:",
        reply_markup=kb,
    )


@router.callback_query(F.data == "res:ab_gen")
async def cb_res_ab_gen(call: CallbackQuery) -> None:
    prof = db.get_profile(call.from_user.id)
    res_text = (prof.get("resume_text") or "").strip()
    if len(res_text) < 30:
        await call.answer("Сначала загрузите базовое резюме!", show_alert=True)
        return
    await call.answer("Создаю 5 A/B-копий со сверкой фактов...")
    w = await call.message.answer("🧬 <b>Собираю 5 целевых A/B-копий резюме и проверяю факты на отсутствие выдумок...</b>")
    role = prof.get("search_query") or "Специалист"
    lines = [f"🧬 <b>Готовы 5 A/B-копий резюме под направление «{role}»:</b>\n"]
    for fk in RESUME_FOCUSES:
        built = await build_resume_variant(res_text, focus=fk, target_role=role, use_llm=True)
        db.upsert_resume_variant(
            user_id=call.from_user.id,
            slot=built["slot"],
            focus=built["focus"],
            title=built["title"],
            content=built["content"],
            fact_ok=built["fact_ok"],
            fact_badge=built["fact_badge"],
        )
        lines.append(
            f"• <b>{built['label']}</b> — {built['title']}\n"
            f"  🛡 <i>{built['fact_badge']}</i>"
        )
    lines.append("\n💡 <i>Теперь при каждом отклике или генерации письма автоматически выбирается лучшая копия под конкретную вакансию.</i>")
    await w.edit_text("\n".join(lines))


@router.callback_query(F.data == "res:ab_list")
async def cb_res_ab_list(call: CallbackQuery) -> None:
    variants = db.get_resume_variants(call.from_user.id)
    if not variants:
        await call.answer("У вас пока нет A/B-копий — нажмите «Создать 5 A/B-копий»!", show_alert=True)
        return
    await call.answer()
    lines = ["📊 <b>Ваши A/B-копии резюме (до 5 акцентов):</b>\n"]
    for v in variants:
        meta = RESUME_FOCUSES.get(str(v.get("focus") or "ats"), RESUME_FOCUSES["ats"])
        lines.append(
            f"<b>{meta['label']}</b>\n"
            f"📌 Заголовок: {v['title']}\n"
            f"🛡 Сверка фактов: <i>{v['fact_badge']}</i> | Откликов: <b>{v.get('used_count', 0)}</b>\n"
        )
    await call.message.answer("\n".join(lines))


@router.callback_query(F.data == "res:upload")
async def cb_res_upload(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.waiting_resume)
    await call.message.answer(
        "📥 <b>Отправьте ваше резюме:</b>\n"
        "• Можете прислать <b>PDF-файл</b> (например, выгрузку с HH.ru)\n"
        "• Или просто вставить <b>текст резюме</b> ответным сообщением."
    )
    await call.answer()


@router.message(Form.waiting_resume, F.document)
async def handle_resume_pdf(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await message.answer("⚠️ Пожалуйста, отправьте файл в формате PDF или пришлите резюме обычным текстом.")
        return
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    pdf_bytes = buf.read()
    extracted = extract_text_from_pdf_bytes(pdf_bytes)
    if not extracted or len(extracted) < 30:
        await message.answer("⚠️ Не удалось извлечь текст из PDF. Пришлите резюме текстом.")
        return
    db.update_field(message.from_user.id, "resume_text", extracted)
    await state.clear()
    await message.answer(
        f"✅ <b>PDF-резюме успешно распознано и сохранено!</b> ({len(extracted)} символов).\n"
        "Теперь вы можете в 1 клик адаптировать его под любую вакансию и генерировать точные отклики.",
        reply_markup=main_menu_kb(),
    )


@router.message(Form.waiting_resume, F.text)
async def handle_resume_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 30:
        await message.answer("⚠️ Текст слишком короткий. Опишите ваш опыт, стек и навыки хотя бы в нескольких строках.")
        return
    db.update_field(message.from_user.id, "resume_text", text)
    await state.clear()
    await message.answer(
        f"✅ <b>Базовое резюме сохранено!</b> ({len(text)} символов).",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "res:adapt")
async def cb_res_adapt(call: CallbackQuery, state: FSMContext) -> None:
    prof = db.get_profile(call.from_user.id)
    if len(prof.get("resume_text") or "") < 30:
        await call.answer("Сначала загрузите базовое резюме!", show_alert=True)
        return
    await state.set_state(Form.waiting_target_vacancy_for_resume)
    await call.message.answer(
        "🎯 <b>Пришлите текст целевой вакансии</b> (с любой площадки — HH, Авито, ВК, LinkedIn, TG), "
        "и я пересоберу ваше резюме под её ключевые слова и требования ATS без выдумывания несуществующих фактов:"
    )
    await call.answer()


@router.message(Form.waiting_target_vacancy_for_resume)
async def process_res_adapt(message: Message, state: FSMContext) -> None:
    await state.clear()
    prof = db.get_profile(message.from_user.id)
    w = await message.answer("⏳ <b>ИИ сверяет ваше резюме с вакансией и перестраивает акценты...</b>")
    res = await adapt_resume_for_vacancy(prof["resume_text"], message.text or "", mode="ats")
    await w.edit_text(res)


@router.callback_query(F.data == "res:audit")
async def cb_res_audit(call: CallbackQuery) -> None:
    prof = db.get_profile(call.from_user.id)
    if len(prof.get("resume_text") or "") < 30:
        await call.answer("Сначала загрузите базовое резюме!", show_alert=True)
        return
    await call.answer("Запускаю ИИ-разбор резюме...")
    w = await call.message.answer("🔬 <b>ИИ-коуч разбирает ваше резюме на слабые места...</b>")
    res = await adapt_resume_for_vacancy(
        prof["resume_text"],
        f"Целевая должность: {prof['search_query']}. Найди слабые формулировки без цифр и предложи усиление.",
        mode="audit",
    )
    await w.edit_text(res)


# ── 4. НАПИСАНИЕ СОПРОВОДИТЕЛЬНЫХ ПИСЕМ ──────────────────────────────────────

@router.message(F.text == "✉️ Написать сопроводительное")
@router.message(Command("letter"))
async def menu_letter(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_target_vacancy_for_letter)
    await message.answer(
        "✉️ <b>Генератор конверсионных сопроводительных писем и питчей</b>\n\n"
        "Вставьте сюда <b>текст вакансии или пост из соцсети/мессенджера</b>, на который хотите откликнуться.\n"
        "Я свяжу задачи работодателя с вашим резюме и напишу готовое письмо:"
    )


@router.message(Form.waiting_target_vacancy_for_letter)
async def process_custom_letter(message: Message, state: FSMContext) -> None:
    await state.clear()
    prof = db.get_profile(message.from_user.id)
    resume_text = prof.get("resume_text") or f"Профильный специалист: {prof['search_query']}"
    w = await message.answer("✍️ <b>Пишу персонализированный отклик...</b>")
    letter = await generate_cover_letter(
        base_resume=resume_text,
        vacancy_info=message.text or "",
        tone=prof.get("tone", "natural"),
    )
    await w.edit_text(
        f"✉️ <b>Готовое сопроводительное письмо:</b>\n\n{html.escape(letter)}\n\n"
        "💡 <i>Нажмите на блок с текстом, чтобы скопировать его.</i>"
    )


# ── 5. ИЗБРАННЫЕ ВАКАНСИИ ────────────────────────────────────────────────────

@router.message(F.text == "⭐ Избранные вакансии")
async def menu_saved(message: Message) -> None:
    saved = db.get_saved(message.from_user.id)
    if not saved:
        await message.answer("⭐ У вас пока нет сохранённых вакансий. Нажимайте «⭐ Сохранить» под карточками в поиске!")
        return
    lines = ["⭐ <b>Ваши сохранённые вакансии:</b>\n"]
    for i, s in enumerate(saved, 1):
        lines.append(
            f"{i}. <b><a href='{s['url']}'>{s['title']}</a></b> — {s['company']} "
            f"({s['salary']}) [{s['platform']}] {s['trust_badge']}"
        )
    await message.answer("\n".join(lines), disable_web_page_preview=True)


# ── 6. НАСТРОЙКИ И ФОНОВЫЙ АВТОПИЛОТ ─────────────────────────────────────────

@router.message(F.text == "⚙️ Настройки поиска и Автопилот")
@router.message(Command("settings"))
async def menu_settings(message: Message) -> None:
    prof = db.get_profile(message.from_user.id)
    await message.answer(
        "⚙️ <b>Настройки вашего поискового агента</b>\n\n"
        f"🌍 Текущий гео-регион: <b>{geo_label(prof.get('geo') or 'ru')}</b>\n"
        "Он определяет источники: РФ/СНГ — HH.ru и Telegram, ЕС/Мир — международные площадки, "
        "НКО — профильные фонды и миссии.\n\n"
        "Нажмите на любой параметр ниже, чтобы изменить его:",
        reply_markup=settings_kb(prof),
    )


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(call: CallbackQuery, state: FSMContext) -> None:
    uid = call.from_user.id
    prof = db.get_profile(uid)
    what = call.data.split(":")[1]

    if what == "query":
        await state.set_state(Form.waiting_search_query)
        await call.message.answer("🎯 Введите вашу желаемую должность / стек (например: Frontend React или Бухгалтер удаленно):")
    elif what == "city":
        await state.set_state(Form.waiting_city)
        await call.message.answer("📍 Введите город поиска (или напишите Удаленно / Россия):")
    elif what == "geo":
        await call.message.answer(
            "🌍 <b>Гео-регион поиска</b>\n\n"
            "Регион сам подбирает источники:\n"
            "• 🇷🇺 Россия — HH.ru + Работа России (ИНН) + Telegram (как раньше).\n"
            "• 🇧🇾 Беларусь / 🇰🇿 Казахстан / 🗺 СНГ — HH.ru по стране + Telegram, соцсети.\n"
            "• 🇪🇺 ЕС / 🌍 Весь мир — соцсети и комьюнити + международные площадки (Indeed, EURES, RemoteOK, We Work Remotely, Glassdoor).\n"
            "• 🤝 НКО и фонды — Idealist, ReliefWeb, UNjobs, Impactpool.\n\n"
            "Выберите регион:",
            reply_markup=geo_picker_kb(prof.get("geo") or "ru"),
        )
    elif what == "salary":
        await state.set_state(Form.waiting_min_salary)
        await call.message.answer("💰 Введите минимальную желаемую зарплату в рублях (только число, например 150000, или 0 чтобы не ограничивать):")
    elif what == "tone":
        order = ["natural", "pro", "short_tg"]
        cur = prof.get("tone", "natural")
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "natural"
        db.update_field(uid, "tone", nxt)
        await call.message.edit_reply_markup(reply_markup=settings_kb(db.get_profile(uid)))
    elif what == "channels":
        await state.set_state(Form.waiting_tg_channels)
        await call.message.answer(
            f"📡 <b>Текущие Telegram-каналы для парсинга:</b>\n{prof['tg_channels']}\n\n"
            "Отправьте новый список публичных каналов через запятую (без @),\n"
            "или верните стандартный список одной кнопкой:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Сбросить к стандартному списку", callback_data="set:channels_reset")],
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="set:channels_cancel")],
                ]
            ),
        )
    elif what == "channels_reset":
        await state.clear()
        db.update_field(uid, "tg_channels", ",".join(DEFAULT_TG_CHANNELS))
        prof = db.get_profile(uid)
        await call.message.answer(
            f"✅ Каналы сброшены к стандартному списку (включая вакансии юристов):\n"
            f"{prof['tg_channels']}",
            reply_markup=settings_kb(prof),
        )
    elif what == "channels_cancel":
        await state.clear()
        await call.message.answer("Отменено — каналы не изменились.", reply_markup=settings_kb(prof))
    elif what == "autopilot":
        new_val = 0 if prof.get("autopilot") else 1
        db.update_field(uid, "autopilot", new_val)
        await call.message.edit_reply_markup(reply_markup=settings_kb(db.get_profile(uid)))
        await call.answer("Автопилот включён!" if new_val else "Автопилот поставлен на паузу.")
    await call.answer()


@router.callback_query(F.data.startswith("geo:"))
async def cb_set_geo(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    if key not in GEO_CONFIG:
        await call.answer("Неизвестный регион")
        return
    uid = call.from_user.id
    db.update_field(uid, "geo", key)
    prof = db.get_profile(uid)
    await call.message.edit_text(
        f"✅ Гео-регион: <b>{geo_label(key)}</b>\n\n"
        "Поиск и карточки будут учитывать выбранный регион. Настройки ниже:",
        reply_markup=settings_kb(prof),
    )
    await call.answer(f"Гео: {geo_label(key)}")


@router.message(Form.waiting_search_query)
async def save_query(message: Message, state: FSMContext) -> None:
    await state.clear()
    q = (message.text or "").strip()
    if q:
        db.update_field(message.from_user.id, "search_query", q)
    prof = db.get_profile(message.from_user.id)
    await message.answer(f"✅ Поисковый запрос обновлён на: <b>{prof['search_query']}</b>", reply_markup=search_sources_kb())


@router.message(Form.waiting_city)
async def save_city(message: Message, state: FSMContext) -> None:
    await state.clear()
    c = (message.text or "").strip()
    if c:
        db.update_field(message.from_user.id, "city", c)
    prof = db.get_profile(message.from_user.id)
    await message.answer(f"✅ Город поиска обновлён: <b>{prof['city']}</b>", reply_markup=settings_kb(prof))


@router.message(Form.waiting_min_salary)
async def save_salary(message: Message, state: FSMContext) -> None:
    await state.clear()
    digits = "".join(ch for ch in (message.text or "") if ch.isdigit())
    val = int(digits) if digits else 0
    db.update_field(message.from_user.id, "min_salary", val)
    prof = db.get_profile(message.from_user.id)
    await message.answer(f"✅ Минимальная зарплата установлена: <b>{val} ₽</b>", reply_markup=settings_kb(prof))


@router.message(Form.waiting_tg_channels)
async def save_channels(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = (message.text or "").replace("@", "").replace(" ", "")
    if raw:
        db.update_field(message.from_user.id, "tg_channels", raw)
    prof = db.get_profile(message.from_user.id)
    await message.answer(f"✅ Каналы обновлены: {prof['tg_channels']}", reply_markup=settings_kb(prof))


# ─────────────────────────────────────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

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
    print("🚀 Вариант А (Personal Job Hunter Bot) запущен в webhook-режиме!")

    try:
        # Держим процесс живым: aiohttp-сервер крутится в текущем event loop.
        await asyncio.Event().wait()
    finally:
        await bot.delete_webhook(drop_pending_updates=True)
        await runner.cleanup()
        logging.info("Webhook удалён, сервер остановлен.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    token = os.environ.get("PERSONAL_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token:
        print(
            "\n[!] ОШИБКА: Не задан токен Telegram-бота!\n"
            "Задайте переменную окружения PERSONAL_BOT_TOKEN (или BOT_TOKEN) от @BotFather:\n"
            "PowerShell:  $env:PERSONAL_BOT_TOKEN='ваш_токен'; python bot_personal.py\n"
        )
        return

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    # Кнопка-меню «Открыть приложение»: Mini App появляется в меню чата сразу,
    # без необходимости писать /start. Нужен публичный HTTPS-адрес (WEBAPP_URL).
    try:
        _menu_url = webapp_url("personal")
        if _menu_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🕊 Открыть приложение", web_app=WebAppInfo(url=_menu_url))
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] Не удалось установить кнопку-меню: {exc}")

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        await _run_webhook(bot, dp, webhook_url)
    else:
        # Сбрасываем вебхук, если он остался от предыдущего запуска, — иначе
        # long polling конфликтует («can't use getUpdates while webhook is active»).
        await bot.delete_webhook(drop_pending_updates=True)
        print("🚀 Вариант А (Personal Job Hunter Bot) запущен и готов к работе!")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
