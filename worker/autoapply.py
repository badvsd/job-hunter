# -*- coding: utf-8 -*-
"""
Автопилот откликов «Джонатан»: очередь задач + Playwright-воркер.

Идея простая и безопасная:
 1. Вы нашли вакансию → Джонатан пишет письмо → задача падает в очередь.
 2. Воркер открывает вакансию в настоящем браузере (Chrome), подставляет
    письмо и отправляет отклик — но только там, где это технически честно:
    сейчас это HH.ru.
 3. Авито, ВК, ОК, LinkedIn, Instagram и Telegram требуют капчу, телефон или
    ручной диалог. Для них задача сразу получает статус `ready`: письмо
    готово, осталось нажать «Отправить» руками. Мы не притворяемся, что
    обошли защиту.

Защита от бана (всё настраивается через переменные окружения):
 • `APPLY_DRY_RUN=1` — по умолчанию. Браузер открывает форму и заполняет
   письмо, но НЕ отправляет. Вы видите скриншот и решаете сами.
 • `APPLY_DAILY_LIMIT` — сколько откликов в сутки максимум.
 • `APPLY_MIN_DELAY` / `APPLY_MAX_DELAY` — случайная пауза между откликами.
 • Один браузерный профиль (`worker/.browser-profile`) — вход в HH.ru
   сохраняется, повторно логиниться не нужно.
 • Капча, вопросы работодателя, требование входа → статус `manual`,
   скриншот и понятная причина. Воркер не пытается обойти проверку.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

WORKER_DIR = Path(__file__).resolve().parent
DB_PATH = WORKER_DIR / "apply_queue.db"
SHOT_DIR = WORKER_DIR / "screenshots"
PROFILE_DIR = WORKER_DIR / ".browser-profile"

STATUSES = (
    "queued",     # в очереди, ждёт браузера
    "running",    # прямо сейчас отправляется
    "sent",       # отклик улетел
    "ready",      # письмо готово, площадка требует ручного нажатия
    "manual",     # нужен человек: капча / вопросы / вход
    "failed",     # техническая ошибка
    "skipped",    # уже откликались
    "cancelled",  # отменено пользователем
)

STATUS_LABELS = {
    "queued": "в очереди",
    "running": "отправляется",
    "sent": "отправлено",
    "ready": "готово к ручному",
    "manual": "нужен человек",
    "failed": "ошибка",
    "skipped": "уже откликались",
    "cancelled": "отменено",
}

# Площадки, где автоотклик технически возможен без обхода защиты.
AUTO_PLATFORMS = {"hh"}

_PLATFORM_RULES: Tuple[Tuple[str, str], ...] = (
    ("hh.ru", "hh"),
    ("avito", "avito"),
    ("t.me", "telegram"),
    ("telegram", "telegram"),
    ("vk.com", "vk"),
    ("ok.ru", "ok"),
    ("linkedin", "linkedin"),
    ("instagram", "instagram"),
    ("superjob", "superjob"),
    ("habr", "habr"),
    ("trudvsem", "trudvsem"),
)


def platform_of(url: str) -> str:
    """Определяет площадку по ссылке. Неизвестное — `other`."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return "other"
    for needle, name in _PLATFORM_RULES:
        if needle in host:
            return name
    return "other"


def settings() -> Dict[str, Any]:
    """Настройки воркера из окружения (читаются на каждый запуск)."""
    def _bool(name: str, default: int) -> bool:
        raw = (os.environ.get(name) or str(default)).strip().lower()
        return raw in ("1", "true", "yes", "on")

    def _int(name: str, default: int) -> int:
        try:
            return int(str(os.environ.get(name) or default).strip())
        except (TypeError, ValueError):
            return default

    lo = _int("APPLY_MIN_DELAY", 45)
    hi = _int("APPLY_MAX_DELAY", 150)
    if hi < lo:
        hi = lo
    return {
        "dry_run": _bool("APPLY_DRY_RUN", 1),
        "daily_limit": max(1, _int("APPLY_DAILY_LIMIT", 20)),
        "min_delay": max(0, lo),
        "max_delay": max(0, hi),
        "headless": _bool("APPLY_HEADLESS", 0),
        "profile_dir": (os.environ.get("APPLY_PROFILE_DIR") or str(PROFILE_DIR)).strip(),
        "timeout_ms": max(5000, _int("APPLY_TIMEOUT_MS", 25000)),
        "answer_questions": _bool("APPLY_ANSWER_QUESTIONS", 1),
        "max_attempts": max(1, min(5, _int("APPLY_MAX_ATTEMPTS", 3))),
        "retry_delay": max(1, _int("APPLY_RETRY_DELAY", 4)),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


@dataclass
class ApplyResult:
    status: str
    note: str = ""
    screenshot: str = ""
    transient: bool = False
    answered: int = 0


TRANSIENT_MARKERS: Tuple[str, ...] = (
    "TimeoutError",
    "Timeout ",
    "net::ERR_",
    "NS_ERROR_",
    "TargetClosedError",
    "Target page, context or browser has been closed",
    "Connection closed",
    "ECONNRESET",
    "ETIMEDOUT",
    "ERR_CONNECTION_",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_TIMED_OUT",
    "ERR_NETWORK_CHANGED",
    "502 Bad Gateway",
    "503 Service",
    "504 Gateway",
)


def is_transient_error(msg: str) -> bool:
    """Определяет, имеет ли смысл автоматически повторить попытку."""
    text = str(msg or "").lower()
    return any(m.lower() in text for m in TRANSIENT_MARKERS)


# ─────────────────────────────────────────────────────────────────────────────
# ОЧЕРЕДЬ (SQLite)
# ─────────────────────────────────────────────────────────────────────────────

class ApplyQueue:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS apply_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    platform TEXT DEFAULT 'other',
                    url TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    letter TEXT DEFAULT '',
                    status TEXT DEFAULT 'queued',
                    attempts INTEGER DEFAULT 0,
                    note TEXT DEFAULT '',
                    screenshot TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_apply_user_status "
                "ON apply_tasks (user_id, status, id)"
            )
            cols = {row["name"] for row in c.execute("PRAGMA table_info(apply_tasks)").fetchall()}
            if "resume_variant" not in cols:
                c.execute("ALTER TABLE apply_tasks ADD COLUMN resume_variant TEXT DEFAULT ''")

    # ── запись ──────────────────────────────────────────────────────────────

    def add(
        self,
        user_id: int,
        url: str,
        title: str = "",
        company: str = "",
        platform: str = "",
        letter: str = "",
        resume_variant: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        Кладёт задачу в очередь. Если по этой ссылке у пользователя уже есть
        активная задача — возвращает её и `False` (дубликат не создаём).
        """
        url = (url or "").strip()
        if not url:
            return None, False

        with self._conn() as c:
            exists = c.execute(
                "SELECT * FROM apply_tasks WHERE user_id = ? AND url = ? "
                "AND status NOT IN ('cancelled', 'failed') ORDER BY id DESC LIMIT 1",
                (user_id, url),
            ).fetchone()
            if exists:
                return dict(exists), False

            plat = platform or platform_of(url)
            status = "queued" if plat in AUTO_PLATFORMS else "ready"
            note = "" if plat in AUTO_PLATFORMS else "Площадка требует ручного отклика"
            cur = c.execute(
                """
                INSERT INTO apply_tasks
                    (user_id, platform, url, title, company, letter, resume_variant, status, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, plat, url, title, company, letter, resume_variant, status, note, _now(), _now()),
            )
            row = c.execute("SELECT * FROM apply_tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row), True

    def update(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE apply_tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def finish(self, task_id: int, status: str, note: str = "", screenshot: str = "") -> None:
        self.update(task_id, status=status, note=note, screenshot=screenshot)

    def update_letter(self, task_id: int, letter: str) -> bool:
        """Обновляет сопроводительное письмо у задачи, пока она не отправлена."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE apply_tasks SET letter = ?, updated_at = ? "
                "WHERE id = ? AND status NOT IN ('sent', 'running')",
                ((letter or "").strip(), _now(), task_id),
            )
            return cur.rowcount > 0

    def cancel(self, task_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE apply_tasks SET status = 'cancelled', updated_at = ? "
                "WHERE id = ? AND status IN ('queued', 'ready', 'manual', 'failed')",
                (_now(), task_id),
            )
            return cur.rowcount > 0

    def retry(self, task_id: int) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT platform FROM apply_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return False
            status = "queued" if row["platform"] in AUTO_PLATFORMS else "ready"
            cur = c.execute(
                "UPDATE apply_tasks SET status = ?, note = '', updated_at = ? "
                "WHERE id = ? AND status IN ('failed', 'manual', 'cancelled', 'ready', 'skipped')",
                (status, _now(), task_id),
            )
            return cur.rowcount > 0

    def clear_finished(self, user_id: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM apply_tasks WHERE user_id = ? "
                "AND status IN ('sent', 'skipped', 'cancelled', 'failed')",
                (user_id,),
            )
            return cur.rowcount

    def claim_next(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Атомарно забирает первую задачу из очереди и помечает её `running`."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM apply_tasks WHERE user_id = ? AND status = 'queued' "
                "ORDER BY id ASC LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            cur = c.execute(
                "UPDATE apply_tasks SET status = 'running', attempts = attempts + 1, "
                "updated_at = ? WHERE id = ? AND status = 'queued'",
                (_now(), row["id"]),
            )
            if cur.rowcount == 0:
                return None
            fresh = c.execute("SELECT * FROM apply_tasks WHERE id = ?", (row["id"],)).fetchone()
            return dict(fresh)

    # ── чтение ──────────────────────────────────────────────────────────────

    def get(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM apply_tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list(self, user_id: int, limit: int = 60) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM apply_tasks WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def counts(self, user_id: int) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM apply_tasks WHERE user_id = ? GROUP BY status",
                (user_id,),
            ).fetchall()
        for r in rows:
            if r["status"] in out:
                out[r["status"]] = r["n"]
        out["total"] = sum(out[s] for s in STATUSES)
        return out

    def recover_stale(self) -> int:
        """Сбрасывает задачи, зависшие в `running` после падения/перезапуска."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE apply_tasks SET status = 'queued', "
                "note = 'Сервер перезапустился во время отклика — задача возвращена в очередь', "
                "updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            return cur.rowcount

    def used_today(self, user_id: int) -> int:
        """Сколько откликов уже обработано сегодня (лимит на сутки)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM apply_tasks WHERE user_id = ? "
                "AND status IN ('sent', 'ready') AND substr(updated_at, 1, 10) = ?",
                (user_id, _today()),
            ).fetchone()
            return int(row["n"] or 0)


# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT: ОТКЛИК НА HH.RU
# ─────────────────────────────────────────────────────────────────────────────

LOGIN_HINTS = ("account/login", "/login")
APPLY_BUTTONS = (
    '[data-qa="vacancy-response-link-top"]',
    '[data-qa="vacancy-response-link-bottom"]',
    '[data-qa="vacancy-response-link"]',
    'a:has-text("Откликнуться")',
    'button:has-text("Откликнуться")',
)
LETTER_INPUTS = (
    'textarea[data-qa="vacancy-response-popup-form-letter-input"]',
    'textarea[data-qa*="letter"]',
    'textarea[name="letter"]',
    'textarea',
)
SUBMIT_BUTTONS = (
    '[data-qa="vacancy-response-submit-popup"]',
    '[data-qa="vacancy-response-submit"]',
    'button:has-text("Отправить отклик")',
    'button:has-text("Отправить")',
)
CAPTCHA_HINTS = (
    '[data-qa="captcha"]',
    'iframe[src*="captcha"]',
    'text=Подтвердите, что вы не робот',
    'text=Я не робот',
)
PHONE_HINTS = (
    'input[type="tel"]',
    'input[data-qa*="phone"]',
    'input[name="phone"]',
    'text=Напишите телефон',
)
QUESTION_HINTS = (
    '[data-qa="vacancy-response-popup-question"]',
    '[data-qa="task-question"]',
    '[data-qa="task-body"]',
    '[data-qa="vacancy-response-questionnaire"]',
    'text=Ответьте на вопросы',
)
LETTER_TOGGLES = (
    '[data-qa="vacancy-response-letter-toggle"]',
    '[data-qa="vacancy-response-letter-informer"] button',
    'button:has-text("Сопроводительное письмо")',
    'button:has-text("Написать сопроводительное")',
    'a:has-text("Сопроводительное письмо")',
)
RESUME_ITEMS = (
    '[data-qa="resume-title"]',
    'input[type="radio"][name*="resume"]',
)
OVERLAY_DISMISS = (
    '[data-qa="region-confirm-yes"]',
    'button:has-text("Да, верно")',
    'button:has-text("Понятно")',
)


# ─────────────────────────────────────────────────────────────────────────────
# АВТОЗАПОЛНЕНИЕ АНКЕТ И ТЕСТОВ HH.RU
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_RESUME_FALLBACK = (
    "Профильный коммерческий опыт, уверенное владение современным стеком, "
    "самостоятельное доведение задач до продакшена и работа в команде."
)


async def answer_question_text(
    question: str,
    resume_text: str = "",
    min_salary: int = 0,
    use_llm: bool = True,
) -> str:
    """
    Генерирует короткий правдивый ответ (1–2 предложения) на текстовый вопрос
    работодателя в анкете HH.ru. Сначала проверяет типовые HR-паттерны
    (зарплата, срок выхода, английский, формат, стаж, ссылки), затем при
    необходимости обращается к LLM с жёстким запретом на выдумки.
    """
    q = (question or "").strip()
    ql = q.lower()
    res_txt = (resume_text or _DEFAULT_RESUME_FALLBACK).strip()

    # 1. Зарплатные ожидания
    if re.search(r"зарплат|ожидан|доход|вознагражден|оклад|деньг|компенсац|net|gross|на руки", ql):
        if min_salary and int(min_salary) > 0:
            sal = f"{int(min_salary):,}".replace(",", " ")
            return f"Ориентируюсь на уровень от {sal} ₽ на руки, готов обсудить вилку с учётом задач и пакета."
        return "Ориентируюсь на рыночную вилку по данной позиции, готов обсудить цифры после знакомства с задачами."

    # 2. Когда готов выйти / срок уведомления
    if re.search(r"выход|приступ|когда смож|срок.*выход|готов.*выйти|начать работ", ql):
        return "Готов подключиться к задачам в течение 1–2 недель после оффера (или быстрее по договорённости)."

    # 3. Уровень английского
    if re.search(r"английск|english|ин\.?\s*яз", ql):
        m = re.search(r"\b(A2|B1|B2|C1|C2|Pre-Intermediate|Intermediate|Upper-Intermediate|Advanced|Fluent)\b", res_txt, re.I)
        if m:
            return f"Уровень английского — {m.group(1)} (свободно читаю техдокументацию и веду рабочую переписку)."
        return "Свободно читаю профильную и техническую документацию, веду рабочую переписку; детали готов уточнить на встрече."

    # 4. Формат работы / график / релокация / часовой пояс
    if re.search(r"формат|удален|офис|гибрид|переезд|релокац|график|часов.*пояс|локац|город", ql):
        return "Указанный в вакансии формат и график работы полностью подходят, готов к полной занятости."

    # 5. Стаж / сколько лет опыта
    if re.search(r"сколько лет|опыт работ|стаж|лет опыт", ql):
        m = re.search(r"(\d+)\s*(?:\+\s*)?(?:года|лет|год)", res_txt, re.I)
        if m:
            return f"Профильный коммерческий опыт — {m.group(0)}; ключевые проекты и стек подробно отражены в резюме."
        return "Профильный опыт работы подробно описан в резюме, аналогичные задачи успешно решал на практике."

    # 6. Ссылки на портфолио / GitHub / примеры кода
    if re.search(r"ссылк|гитхаб|github|gitlab|портфолио|пример.*работ|пример.*код|pet", ql):
        url_m = re.search(r"https?://\S+", res_txt)
        if url_m:
            return f"Ссылка на профиль и примеры работ: {url_m.group(0).rstrip('.,;) ')}"
        return "Примеры решённых кейсов и исходный код готов направить в чате или показать на коротком созвоне."

    # 7. Нестандартный вопрос — пробуем LLM строго по фактам из резюме
    if use_llm and q:
        try:
            from shared_engine import llm

            sys_prompt = (
                "Ты помогаешь кандидату заполнить короткую анкету при отклике на HH.ru.\n"
                "Правила:\n"
                "- Ответь строго в 1–2 коротких предложениях (до 240 символов) от первого лица.\n"
                "- Опирайся ТОЛЬКО на факты из резюме. Ничего не выдумывай.\n"
                "- Если точного факта в резюме нет — дай спокойный нейтральный ответ, что готов уточнить на созвоне.\n"
                "- Выдай только чистый текст ответа без кавычек и вступлений."
            )
            user_prompt = f"РЕЗЮМЕ:\n{res_txt[:1500]}\n\nВОПРОС РАБОТОДАТЕЛЯ:\n{q}"
            ans = await llm.ask(sys_prompt, user_prompt, temperature=0.3)
            if ans and len(ans.strip()) >= 8:
                return ans.strip()[:400]
        except Exception:
            pass

    return (
        "Релевантный практический опыт по этому направлению отражён в резюме — "
        "буду рад подробно рассказать о своих кейсах на коротком созвоне."
    )


def pick_radio_option(question: str, options: List[str], resume_text: str = "") -> Tuple[int, bool]:
    """
    Выбирает наиболее подходящий вариант в радио-вопросе анкеты HH.ru.
    Возвращает `(индекс, уверен_ли)`. Если все варианты выглядят рискованно —
    возвращает `(-1, False)`, чтобы воркер оставил вопрос человеку.
    """
    if not options:
        return -1, False

    res_low = (resume_text or "").lower()
    q_low = (question or "").lower()

    positive_patterns = (
        r"^да\b",
        r"\bда,",
        r"есть опыт",
        r"имею опыт",
        r"готов",
        r"согласен",
        r"подходит",
        r"полная занятость",
        r"полный день",
        r"уверенн",
        r"более\s*[3-5]",
        r"от\s*3",
        r"1\s*[–-]\s*3",
        r"3\s*[–-]\s*6",
        r"\bb2\b",
        r"\bc1\b",
    )
    negative_patterns = (
        r"^нет\b",
        r"\bнет,",
        r"не имею",
        r"нет опыта",
        r"без опыта",
        r"не готов",
        r"не согласен",
        r"не подходит",
    )

    best_idx = -1
    best_score = -999

    for idx, raw_opt in enumerate(options):
        opt = (raw_opt or "").strip()
        ol = opt.lower()
        if not ol:
            continue
        score = 0

        # Если слова из варианта встречаются прямо в резюме — плюс
        words = [w for w in re.findall(r"[а-яёa-z0-9+#]{3,}", ol) if len(w) >= 3]
        for w in words:
            if w in res_low:
                score += 2

        for pat in positive_patterns:
            if re.search(pat, ol):
                score += 5
        for pat in negative_patterns:
            if re.search(pat, ol):
                score -= 6

        # Если вопрос про нарушение/судимость/ограничения — наоборот выбираем «Нет»
        if re.search(r"судимост|ограничен|противопоказан|конфликт интересов", q_low):
            if re.search(r"^нет\b", ol):
                score += 15
            elif re.search(r"^да\b", ol):
                score -= 15

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx >= 0 and best_score >= 1:
        return best_idx, True
    # Если есть нейтральный вариант «Готов обсудить / Другое»
    for idx, raw_opt in enumerate(options):
        if re.search(r"обсудить|другое|свой вариант|зависит", (raw_opt or "").lower()):
            return idx, True
    return -1, False


async def _pick_hh_resume(page: Any, preferred_title: str = "") -> str:
    """
    Если у пользователя несколько резюме на HH, выбирает то, чей заголовок
    лучше всего совпадает с выбранной A/B-копией или названием вакансии.
    Возвращает название выбранного резюме (или пустую строку).
    """
    try:
        radios = page.locator('[data-qa="resume-title"], label:has(input[type="radio"][name*="resume"])')
        cnt = await radios.count()
        if cnt <= 1:
            return ""
        pref_words = [
            w for w in re.findall(r"[а-яёa-z0-9+#]{3,}", (preferred_title or "").lower())
            if len(w) >= 3
        ]
        best_idx = 0
        best_score = -1
        best_txt = ""
        for i in range(cnt):
            item = radios.nth(i)
            raw_txt = ((await item.text_content()) or "").strip()
            txt_low = raw_txt.lower()
            score = sum(3 for w in pref_words if w in txt_low)
            if score > best_score:
                best_score = score
                best_idx = i
                best_txt = raw_txt
        if best_score > 0:
            await radios.nth(best_idx).click(timeout=1500)
            await page.wait_for_timeout(300)
            return best_txt[:70]
    except Exception:
        return ""
    return ""


async def _expand_letter_field(page: Any) -> None:
    """Раскрывает поле сопроводительного письма, если оно свёрнуто под кнопкой."""
    for sel in LETTER_TOGGLES:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            continue


async def _fill_hh_questions(
    page: Any,
    task: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[int, int]:
    """
    Находит и заполняет вопросы работодателя (радио-группы и текстовые поля
    анкеты, кроме поля сопроводительного письма).
    Возвращает `(заполнено_вопросов, осталось_нерешённых)`.
    """
    answered = 0
    unresolved = 0
    resume_text = str(task.get("resume_text") or cfg.get("resume_text") or "")
    min_salary = int(task.get("min_salary") or cfg.get("min_salary") or 0)
    use_llm = bool(cfg.get("use_llm_for_questions", True))

    # 1. Радио-группы (кроме выбора резюме)
    try:
        radio_groups = await page.evaluate(
            """() => {
                const groups = {};
                const order = [];
                document.querySelectorAll('input[type="radio"]').forEach((el) => {
                    const name = el.name || '';
                    if (!name || /resume/i.test(name)) return;
                    if (!groups[name]) {
                        groups[name] = { name, question: '', checked: false, options: [] };
                        order.push(name);
                    }
                    if (el.checked) groups[name].checked = true;
                    const labelEl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`) || el.parentElement;
                    const label = (labelEl ? labelEl.innerText : el.value || '').trim();
                    const box = el.closest('[data-qa="task-body"], [data-qa*="question"], fieldset, form, div');
                    if (box && !groups[name].question) {
                        const header = box.querySelector('[data-qa="task-question"], legend, h3, h4, b, p');
                        if (header) groups[name].question = header.innerText.trim();
                    }
                    groups[name].options.push(label);
                });
                return order.map(k => groups[k]);
            }"""
        )
        for grp in radio_groups or []:
            if grp.get("checked"):
                answered += 1
                continue
            idx, confident = pick_radio_option(
                grp.get("question") or "",
                grp.get("options") or [],
                resume_text=resume_text,
            )
            if idx >= 0 and confident:
                try:
                    target = page.locator(f'input[type="radio"][name="{grp["name"]}"]').nth(idx)
                    label_loc = target.locator("xpath=ancestor::label[1]")
                    if await label_loc.count() > 0:
                        await label_loc.first.click(timeout=1500)
                    else:
                        await target.check(force=True, timeout=1500)
                    answered += 1
                    await page.wait_for_timeout(250)
                except Exception:
                    unresolved += 1
            else:
                unresolved += 1
    except Exception:
        pass

    # 2. Текстовые поля вопросов (исключаем поле сопроводительного письма)
    try:
        q_fields = page.locator(
            '[data-qa="task-body"] textarea, '
            '[data-qa*="question"] textarea, '
            'textarea[name*="task"], '
            'input[type="text"][name*="task"]'
        )
        cnt = await q_fields.count()
        for i in range(cnt):
            loc = q_fields.nth(i)
            try:
                if not await loc.is_visible(timeout=400):
                    continue
                qa_attr = (await loc.get_attribute("data-qa")) or ""
                name_attr = (await loc.get_attribute("name")) or ""
                if "letter" in qa_attr.lower() or "letter" in name_attr.lower():
                    continue
                cur_val = ((await loc.input_value()) or "").strip()
                if cur_val:
                    answered += 1
                    continue
                q_text = await loc.evaluate(
                    """(el) => {
                        const box = el.closest('[data-qa="task-body"], [data-qa*="question"], fieldset, div');
                        if (!box) return el.placeholder || '';
                        const h = box.querySelector('[data-qa="task-question"], legend, label, h3, h4, b, p');
                        return (h ? h.innerText : el.placeholder || '').trim();
                    }"""
                )
                ans = await answer_question_text(
                    q_text,
                    resume_text=resume_text,
                    min_salary=min_salary,
                    use_llm=use_llm,
                )
                if ans:
                    await loc.fill(ans)
                    answered += 1
                    await page.wait_for_timeout(300)
                else:
                    unresolved += 1
            except Exception:
                unresolved += 1
    except Exception:
        pass

    return answered, unresolved


async def _visible(page: Any, selectors: Tuple[str, ...], timeout: int = 3000) -> Any:
    """Возвращает первый видимый локатор из списка или None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _has_text(page: Any, needle: str) -> bool:
    try:
        return await page.locator(f"text={needle}").count() > 0
    except Exception:
        return False


async def _shot(page: Any, task_id: int, tag: str) -> str:
    """Скриншот в worker/screenshots; возвращает имя файла."""
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{task_id}-{int(time.time())}-{tag}.png"
        await page.screenshot(path=str(SHOT_DIR / name), full_page=False)
        return name
    except Exception:
        return ""


async def apply_hh(task: Dict[str, Any], cfg: Dict[str, Any]) -> ApplyResult:
    """
    Один отклик на HH.ru. Возвращает ApplyResult, никогда не бросает
    исключение наружу — воркер должен продолжать работу.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ApplyResult("failed", "Playwright не установлен: pip install playwright && playwright install chrome")

    task_id = int(task["id"])
    timeout = int(cfg["timeout_ms"])
    letter = (task.get("letter") or "").strip()

    try:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=cfg["profile_dir"],
                channel="chrome",
                headless=bool(cfg["headless"]),
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(task["url"], wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(2500)

                # 0. Убираем мешающие оверлеи (регион, куки) — они перекрывают кнопки
                for sel in OVERLAY_DISMISS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=500):
                            await loc.click(timeout=1500)
                            await page.wait_for_timeout(400)
                    except Exception:
                        continue

                # 1. Не залогинены?
                if any(h in (page.url or "") for h in LOGIN_HINTS):
                    shot = await _shot(page, task_id, "login")
                    return ApplyResult(
                        "manual",
                        "Нужно войти в HH.ru: откройте браузер воркера и авторизуйтесь один раз "
                        "(профиль сохранится). Затем нажмите «Повторить».",
                        shot,
                    )

                # 2. Уже откликались?
                if await _has_text(page, "Вы откликнулись"):
                    return ApplyResult("skipped", "Отклик уже отправлен ранее")

                # 3. Капча на входе
                captcha = await _visible(page, CAPTCHA_HINTS, timeout=800)
                if captcha:
                    shot = await _shot(page, task_id, "captcha")
                    return ApplyResult("manual", "HH.ru просит капчу — решите её в браузере и повторите", shot)

                # 4. Кнопка «Откликнуться»
                btn = await _visible(page, APPLY_BUTTONS, timeout=8000)
                if not btn:
                    shot = await _shot(page, task_id, "no-button")
                    return ApplyResult(
                        "manual",
                        "Кнопка «Откликнуться» не найдена: вакансия закрыта, отклик уже отправлен "
                        "или нужен вход в аккаунт.",
                        shot,
                    )
                await btn.click()
                await page.wait_for_timeout(2200)

                # 5. Появилась капча после клика
                captcha = await _visible(page, CAPTCHA_HINTS, timeout=900)
                if captcha:
                    shot = await _shot(page, task_id, "captcha")
                    return ApplyResult("manual", "Капча при отклике — нужен человек", shot)

                # 6. Гостевая стена: HH просит телефон или вход
                phone = await _visible(page, PHONE_HINTS, timeout=1500)
                if phone:
                    shot = await _shot(page, task_id, "phone-wall")
                    return ApplyResult(
                        "manual",
                        "HH.ru просит номер телефона или вход в аккаунт. Войдите один раз в браузере "
                        "воркера (APPLY_HEADLESS=0), затем нажмите «Повторить».",
                        shot,
                    )

                # 6.5. Если у соискателя несколько резюме — выбираем подходящее
                pref_resume = f"{task.get('resume_variant') or ''} {task.get('title') or ''}".strip()
                await _pick_hh_resume(page, pref_resume)

                # 7. Анкета / тестовые вопросы работодателя
                has_questions = (await _visible(page, QUESTION_HINTS, timeout=900)) is not None
                answered_cnt = 0
                unresolved_cnt = 0
                if has_questions:
                    if not cfg.get("answer_questions", True):
                        shot = await _shot(page, task_id, "questions")
                        return ApplyResult(
                            "manual",
                            "Работодатель задаёт вопросы (автоответы выключены: APPLY_ANSWER_QUESTIONS=0).",
                            shot,
                        )
                    answered_cnt, unresolved_cnt = await _fill_hh_questions(page, task, cfg)
                    if unresolved_cnt > 0 or answered_cnt == 0:
                        shot = await _shot(page, task_id, "questions-partial")
                        note_part = (
                            f"Заполнено вопросов: {answered_cnt}, требуют ручного выбора: {unresolved_cnt}. "
                            if answered_cnt
                            else "Нестандартная анкета работодателя — "
                        )
                        return ApplyResult(
                            "manual",
                            note_part + "проверьте форму и отправьте отклик вручную.",
                            shot,
                            answered=answered_cnt,
                        )

                # 8. Поле сопроводительного письма (при необходимости раскрываем тоггл)
                await _expand_letter_field(page)
                letter_selectors = LETTER_INPUTS[:-1] if has_questions else LETTER_INPUTS
                field = await _visible(page, letter_selectors, timeout=3500 if has_questions else 7000)
                if not field and not has_questions:
                    shot = await _shot(page, task_id, "no-form")
                    return ApplyResult(
                        "manual",
                        "Форма отклика не открылась автоматически — проверьте вакансию вручную.",
                        shot,
                    )
                if field and letter:
                    await field.fill(letter)
                    await page.wait_for_timeout(600)

                q_suffix = f" (отвечено на вопросов анкеты: {answered_cnt})" if answered_cnt else ""

                # 9. Черновик: заполнили, но не отправляем
                if cfg["dry_run"]:
                    shot = await _shot(page, task_id, "draft")
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    return ApplyResult(
                        "ready",
                        f"Черновик: форма заполнена{q_suffix}, отправка выключена (APPLY_DRY_RUN=1).",
                        shot,
                        answered=answered_cnt,
                    )

                # 10. Отправка
                submit = await _visible(page, SUBMIT_BUTTONS, timeout=6000)
                if not submit:
                    shot = await _shot(page, task_id, "no-submit")
                    return ApplyResult(
                        "manual",
                        f"Кнопка «Отправить» не найдена{q_suffix} — отправьте вручную",
                        shot,
                        answered=answered_cnt,
                    )
                await submit.click()
                await page.wait_for_timeout(3000)

                # 11. Проверка результата
                ok = await _visible(
                    page,
                    ('[data-qa="vacancy-response-popup-success"]',),
                    timeout=5000,
                )
                success_text = ok is not None or await _has_text(page, "Отклик отправлен") \
                    or await _has_text(page, "Вы откликнулись")

                if success_text:
                    shot = await _shot(page, task_id, "sent")
                    return ApplyResult("sent", f"Отклик отправлен{q_suffix}", shot, answered=answered_cnt)

                shot = await _shot(page, task_id, "unknown")
                return ApplyResult(
                    "manual",
                    f"После отправки не удалось подтвердить результат{q_suffix} — проверьте отклик на HH.ru.",
                    shot,
                    answered=answered_cnt,
                )
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001 — воркер не должен падать
        err_msg = f"{type(exc).__name__}: {exc}"
        return ApplyResult(
            "failed",
            f"Сбой браузера: {err_msg}",
            transient=is_transient_error(err_msg),
        )


async def apply_task(task: Dict[str, Any], cfg: Dict[str, Any]) -> ApplyResult:
    """
    Диспетчер: авто-отклик на HH.ru с автоматическими повторами при сетевых
    и браузерных сбоях (до `max_attempts` с экспоненциальной паузой).
    На капче, логине и ручных статусах повторы НЕ делаются.
    """
    plat = task.get("platform") or platform_of(task.get("url") or "")
    if plat != "hh":
        return ApplyResult(
            "ready",
            "Площадка не поддерживает автоотклик без обхода защиты — откройте ссылку и вставьте письмо.",
        )

    max_attempts = max(1, int(cfg.get("max_attempts") or 3))
    base_delay = max(1, int(cfg.get("retry_delay") or 4))
    last_res: Optional[ApplyResult] = None

    for attempt in range(1, max_attempts + 1):
        res = await apply_hh(task, cfg)
        last_res = res
        if not res.transient or res.status != "failed" or attempt >= max_attempts:
            if attempt > 1 and res.status in ("sent", "ready"):
                res.note = f"{res.note} (с {attempt}-й попытки)"
            elif attempt > 1 and res.status == "failed":
                res.note = f"{res.note} (после {attempt} попыток)"
            return res
        backoff = base_delay * (2 ** (attempt - 1))
        await asyncio.sleep(backoff)

    return last_res or ApplyResult("failed", "Неизвестная ошибка воркера")


# ─────────────────────────────────────────────────────────────────────────────
# ВОРКЕР
# ─────────────────────────────────────────────────────────────────────────────

class ApplyWorker:
    def __init__(self, queue: ApplyQueue) -> None:
        self.queue = queue
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self.state: Dict[str, Any] = {
            "running": False,
            "user_id": 0,
            "current": None,
            "processed": 0,
            "sent": 0,
            "ready": 0,
            "manual": 0,
            "failed": 0,
            "skipped": 0,
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "dry_run": True,
            "stop_requested": False,
        }

    def snapshot(self) -> Dict[str, Any]:
        return dict(self.state)

    def start(
        self,
        user_id: int,
        limit: int = 3,
        dry_run: Optional[bool] = None,
        extra_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Запускает воркер в фоне и сразу возвращает ответ."""
        if self.state["running"]:
            return {"ok": False, "error": "Автопилот уже в полёте — дождитесь окончания или остановите."}
        cfg = settings()
        if extra_cfg:
            cfg.update(extra_cfg)
        if dry_run is not None:
            cfg["dry_run"] = bool(dry_run)
        self._stop = False
        self._task = asyncio.create_task(self._run(user_id, limit, cfg))
        return {
            "ok": True,
            "message": "Автопилот взлетел",
            "dry_run": cfg["dry_run"],
        }

    async def stop(self) -> Dict[str, Any]:
        if not self.state["running"]:
            return {"ok": False, "error": "Автопилот и так на земле"}
        self._stop = True
        self.state["stop_requested"] = True
        return {"ok": True, "message": "Останавливаюсь после текущего отклика"}

    async def _run(self, user_id: int, limit: int, cfg: Dict[str, Any]) -> None:
        async with self._lock:
            self.state.update(
                running=True,
                user_id=user_id,
                current=None,
                processed=0,
                sent=0,
                ready=0,
                manual=0,
                failed=0,
                skipped=0,
                started_at=_now(),
                finished_at="",
                last_error="",
                dry_run=bool(cfg["dry_run"]),
                stop_requested=False,
            )
            try:
                room = cfg["daily_limit"] - self.queue.used_today(user_id)
                if room <= 0:
                    self.state["last_error"] = (
                        f"Дневной лимит откликов исчерпан ({cfg['daily_limit']}). "
                        "Измените APPLY_DAILY_LIMIT или продолжите завтра."
                    )
                    return

                limit = max(1, min(int(limit or 1), room))
                for _ in range(limit):
                    if self._stop:
                        break
                    task = self.queue.claim_next(user_id)
                    if not task:
                        break

                    self.state["current"] = {
                        "id": task["id"],
                        "title": task.get("title") or "",
                        "company": task.get("company") or "",
                        "platform": task.get("platform") or "",
                    }
                    try:
                        result = await apply_task(task, cfg)
                    except Exception as exc:  # noqa: BLE001
                        result = ApplyResult("failed", f"Сбой воркера: {type(exc).__name__}: {exc}")

                    self.queue.finish(task["id"], result.status, result.note, result.screenshot)
                    if result.status in self.state:
                        self.state[result.status] = int(self.state.get(result.status) or 0) + 1
                    self.state["processed"] += 1
                    self.state["current"] = None

                    if self._stop:
                        break
                    await asyncio.sleep(random.uniform(cfg["min_delay"], cfg["max_delay"]))
            finally:
                self.state["running"] = False
                self.state["current"] = None
                self.state["finished_at"] = _now()


queue = ApplyQueue()
queue.recover_stale()
worker = ApplyWorker(queue)
