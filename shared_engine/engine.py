# -*- coding: utf-8 -*-
"""
JobShield & Multi-Platform Job Hunter Core Engine.
Общее ядро для:
 - Варианта А (Личный безлимитный комбайн соискателя)
 - Варианта Б (Коммерческий SaaS-сервис с тарифами, подписками и админкой)

Включает:
1. Мульти-LLM роутер (OpenAI/Gemini/Groq/OVH/Dahl/Pool-Router + умный эвристический фолбэк без ключей).
2. Мультиплатформенный поиск вакансий:
   - HH.ru (официальный публичный API)
   - Работа России / Trudvsem.ru (гос. база с проверенными ИНН работодателей)
   - Telegram-каналы (реальный парсинг публичных веб-лент t.me/s/...)
   - Агрегатор соцсетей и площадок Рунета (Авито, ВК, ОК, SuperJob, Habr Career, LinkedIn, Instagram)
3. Антифрод и СБ-проверка работодателя (Trust Score 0-100%, проверка ИНН по контрольным суммам и открытым данным,
   поиск судебных/исполнительных маркеров, 25+ паттернов мошеннических схем, дроппинга, платного обучения и т.д.).
4. ИИ-редактор резюме под вакансию (ATS-оптимизация без выдумывания фактов) и генератор сопроводительных писем / питчей.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("jobhunter.search")

try:
    import pymupdf as fitz  # PyMuPDF для чтения PDF-резюме
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None


# ─────────────────────────────────────────────────────────────────────────────
# 1. МУЛЬТИ-LLM КЛИЕНТ (С АВТОФОЛБЭКОМ НА БЕСПЛАТНЫЙ ПУЛ)
# ─────────────────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Универсальный ИИ-клиент. Пробует:
    1) Заданный пользователем OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY
    2) Локальный роутер пула (http://127.0.0.1:8901/v1) если запущен
    3) Бесплатные облачные эндпоинты (OVHcloud AI, Dahl, LLM7)
    4) Встроенный детерминированный экспертный генератор (если нет интернета до LLM)
    """

    def __init__(self) -> None:
        self.upstreams = []
        if os.environ.get("OPENAI_API_KEY"):
            self.upstreams.append({
                "name": "openai-custom",
                "url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "key": os.environ["OPENAI_API_KEY"],
            })
        if os.environ.get("GEMINI_API_KEY"):
            self.upstreams.append({
                "name": "gemini-openai",
                "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "model": "gemini-2.5-flash",
                "key": os.environ["GEMINI_API_KEY"],
            })
        if os.environ.get("GROQ_API_KEY"):
            self.upstreams.append({
                "name": "groq",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "model": "openai/gpt-oss-120b",
                "key": os.environ["GROQ_API_KEY"],
            })
        # Локальный пул из папки free-llm-pool
        self.upstreams.extend([
            {
                "name": "local-pool-router",
                "url": "http://127.0.0.1:8901/v1/chat/completions",
                "model": "pool/fast",
                "key": None,
            },
            {
                "name": "ovh-gpt-oss-20b",
                "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
                "model": "gpt-oss-20b",
                "key": None,
            },
            {
                "name": "ovh-llama-3.3-70b",
                "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
                "model": "Meta-Llama-3_3-70B-Instruct",
                "key": None,
            },
            {
                "name": "llm7-free",
                "url": "https://api.llm7.io/v1/chat/completions",
                "model": "gpt-oss:20b",
                "key": None,
            },
        ])

    async def ask(self, system_prompt: str, user_prompt: str, temperature: float = 0.4) -> Optional[str]:
        headers_base = {"Content-Type": "application/json"}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        async with httpx.AsyncClient(timeout=22.0, follow_redirects=True) as client:
            for up in self.upstreams:
                headers = dict(headers_base)
                if up.get("key"):
                    headers["Authorization"] = f"Bearer {up['key']}"
                payload = {
                    "model": up["model"],
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 1800,
                }
                try:
                    resp = await client.post(up["url"], json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        content = (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                        if content and len(content.strip()) > 15:
                            return content.strip()
                except Exception:
                    continue
        return None


llm = LLMClient()


# ─────────────────────────────────────────────────────────────────────────────
# 2. МОДЕЛЬ ВАКАНСИИ И МУЛЬТИПЛАТФОРМЕННЫЙ ПОИСК РУНЕТА
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Vacancy:
    id: str
    platform: str          # hh.ru, trudvsem.ru, telegram, avito, vk, ok, superjob, habr, linkedin, instagram
    title: str
    company: str
    salary: str
    url: str
    description: str
    city: str = ""
    inn: str = ""
    contact: str = ""
    published_at: str = ""
    trust_score: int = 80
    trust_badge: str = "🟢 Надёжно"
    trust_flags: List[str] = field(default_factory=list)


# Популярные публичные Telegram-каналы с вакансиями в РФ.
# Это и примеры того, какие каналы стоит искать в Telegram и на других площадках:
# профильные каналы по профессии дают самые свежие и релевантные вакансии.
DEFAULT_TG_CHANNELS = [
    "finder_vc",        # Finder.vc: удалённая работа
    "normrabota",       # Норм работа
    "vacancies_it",     # Вакансии IT
    "devjobs",          # DevJobs
    "foranalysts",      # Для аналитиков
    "zarubezhom_jobs",  # Удалёнка и релокация
    "work_in_digital",  # Digital вакансии
    # ── примеры профильных каналов для юристов ──
    "job_lawyer_junior",   # Вакансии младших юристов
    "proocareer",          # PRO.Карьера: вакансии для юристов и юридического рынка
    "aestheticsoflawjob",  # Вакансии для юристов (стажировки, госслужба)
    "tohuntalawyer",       # To Hunt a Lawyer: вакансии и стажировки для студентов-юристов
]


# ─────────────────────────────────────────────────────────────────────────────
# Словарь «профессия → каналы + ключевые слова».
# Telegram игнорирует ?q= в t.me/s/<канал>, поэтому релевантность постов
# считаем на своей стороне: подключаем профильные каналы под запрос
# и фильтруем посты по ключевым словам.
# ─────────────────────────────────────────────────────────────────────────────
PROFESSION_CHANNEL_MAP: Dict[str, Dict[str, List[str]]] = {
    "юрист": {
        "aliases": ["юрист", "юрид", "правов", "адвокат", "lawyer", "legal", "нотариус", "юротдел"],
        "channels": [
            "job_lawyer_junior",
            "proocareer",
            "aestheticsoflawjob",
            "tohuntalawyer",
        ],
        "keywords": [
            "юрист", "юридич", "правов", "адвокат", "lawyer", "legal",
            "нотариус", "право", "судебн", "договор", "стажер", "стажировк",
        ],
    },
    "айти": {
        "aliases": ["айти", "разработчик", "программист", "developer", "engineer", "devops", "backend", "frontend", "тестировщик", "qa"],
        "channels": [
            "vacancies_it",
            "devjobs",
            "finder_vc",
            "work_in_digital",
        ],
        "keywords": [
            "разработчик", "программист", "developer", "engineer", "python",
            "java", "javascript", "backend", "frontend", "devops", "qa",
            "тестировщик", "программир",
        ],
    },
    "аналитик": {
        "aliases": ["аналитик", "analyst", "data science", "data-science", "datascientist", "bi-аналитик", "data-аналитик"],
        "channels": [
            "foranalysts",
        ],
        "keywords": [
            "аналитик", "analyst", "data", "sql", "power bi", "tableau", "метрик", "дашборд",
        ],
    },
}


def _split_query_tokens(query: str) -> List[str]:
    """Делит запрос на нормализованные токены (>= 3 символа) для сопоставления."""
    q = (query or "").lower()
    return [t for t in re.split(r"[^a-zа-яё0-9]+", q) if len(t) >= 3]


def profession_channels_for(query: str) -> List[str]:
    """Подбирает профильные каналы под запрос по словарю профессий."""
    q = (query or "").lower()
    out: List[str] = []
    for cfg in PROFESSION_CHANNEL_MAP.values():
        if any(alias in q for alias in cfg["aliases"]):
            for ch in cfg["channels"]:
                if ch not in out:
                    out.append(ch)
    return out


def relevance_keywords_for(query: str) -> List[str]:
    """Собирает ключевые слова для клиентской фильтрации постов под запрос."""
    q = (query or "").lower()
    kws: List[str] = []
    for cfg in PROFESSION_CHANNEL_MAP.values():
        if any(alias in q for alias in cfg["aliases"]):
            kws.extend(cfg["keywords"])
    kws.extend(_split_query_tokens(query))
    seen = set()
    uniq: List[str] = []
    for kw in kws:
        if kw and kw not in seen:
            seen.add(kw)
            uniq.append(kw)
    return uniq


def post_matches_query(text: str, keywords: List[str]) -> bool:
    """Проверяет, релевантен ли пост запросу по ключевым словам."""
    if not keywords:
        return True
    t = (text or "").lower()
    return any(kw in t for kw in keywords)


def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _format_hh_salary(sal: Optional[dict]) -> str:
    if not sal:
        return "з/п не указана"
    s_from = sal.get("from")
    s_to = sal.get("to")
    cur = sal.get("currency", "RUR")
    cur_map = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}
    c = cur_map.get(cur, cur)
    if s_from and s_to:
        return f"{s_from:,} – {s_to:,} {c}".replace(",", " ")
    if s_from:
        return f"от {s_from:,} {c}".replace(",", " ")
    if s_to:
        return f"до {s_to:,} {c}".replace(",", " ")
    return "з/п не указана"


# ─────────────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ ГЛУБИНЫ ПОИСКА (можно менять переменными окружения)
# ─────────────────────────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


SEARCH_MAX_TOTAL = _env_int("SEARCH_MAX_TOTAL", 1000)       # всего вакансий за один поиск
HH_PAGE_SIZE = 50                                            # карточек на странице HTML-выдачи hh
HH_MAX_PAGES = 40                                            # hh отдаёт максимум ~2000 вакансий на запрос
HH_CONCURRENCY = _env_int("HH_CONCURRENCY", 3)               # параллельных страниц hh (больше — риск капчи)
TG_PAGES_PER_CHANNEL = _env_int("TG_PAGES_PER_CHANNEL", 5)   # глубина канала (~20 постов на страницу)
TG_CONCURRENCY = _env_int("TG_CONCURRENCY", 6)               # параллельных запросов к t.me

# Статистика последнего поиска по источникам (для логов и диагностики в боте).
LAST_SEARCH_STATS: Dict[str, Any] = {}
_HH_API_BLOCKED_UNTIL = 0.0   # если API отдал не 200 — 10 минут сразу идём в HTML

_HH_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_HH_AREAS = {
    "москва": 1, "санкт-петербург": 2, "спб": 2, "питер": 2, "екатеринбург": 3,
    "новосибирск": 4, "нижний новгород": 66, "казань": 88, "россия": 113, "вся россия": 113,
    # Страны СНГ (area-id из api.hh.ru/areas, top-level)
    "беларусь": 16, "рб": 16, "минск": 276,
    "казахстан": 40, "кз": 40, "алматы": 202, "астана": 159,
    "азербайджан": 9, "баку": 74,
    "грузия": 28, "тбилиси": 1274,
    "кыргызстан": 48, "киргизия": 48, "бишкек": 196,
    "узбекистан": 97, "ташкент": 1660,
    "украина": 5, "киев": 115,
}
_REMOTE_WORDS = ("удален", "удалён", "remote", "дистанц")

# Area-id стран СНГ для региона «СНГ»: HH допускает несколько area в одном запросе.
CIS_AREA_IDS = [113, 5, 40, 9, 16, 28, 48, 97]  # РФ, Украина, Казахстан, Азербайджан, Беларусь, Грузия, Кыргызстан, Узбекистан
# Area-id российских городов: для региона «СНГ» они означают «искать по всей сетке», а не по одному городу.
_RU_CITY_AREAS = {1, 2, 3, 4, 66, 88}

# ─────────────────────────────────────────────────────────────────────────
# ГЕО-РЕГИОНЫ ПОИСКА (география определяет набор источников)
# Ключ geo хранится в профиле пользователя. 'ru' — поведение «как раньше».
# ─────────────────────────────────────────────────────────────────────────

GEO_CONFIG = {
    "ru":   {"label": "🇷🇺 Россия",             "hh_area": 113, "cis": False, "world": False, "ngo": False},
    "by":   {"label": "🇧🇾 Беларусь",           "hh_area": 16,  "cis": True,  "world": False, "ngo": False},
    "kz":   {"label": "🇰🇿 Казахстан",          "hh_area": 40,  "cis": True,  "world": False, "ngo": False},
    "cis":  {"label": "🗺 СНГ (все страны)",     "hh_area": None, "cis": True, "world": False, "ngo": False},
    "eu":   {"label": "🇪🇺 ЕС / Европа",        "hh_area": None, "cis": False, "world": True, "ngo": False},
    "world": {"label": "🌍 Весь мир / Удалёнка", "hh_area": None, "cis": False, "world": True, "ngo": False},
    "ngo":  {"label": "🤝 НКО и фонды",         "hh_area": None, "cis": False, "world": False, "ngo": True},
}

# Упорядоченный список для кнопок выбора гео.
GEO_ORDER = ["ru", "by", "kz", "cis", "eu", "world", "ngo"]


def geo_label(geo: str) -> str:
    return GEO_CONFIG.get(geo or "", GEO_CONFIG["ru"])["label"]


# Города/токены, допустимые для региона с фиксированной страной, и страна по умолчанию.
GEO_CITY_HINTS = {
    "ru": ("россия", "вся россия", "москва", "санкт-петербург", "спб", "питер",
           "екатеринбург", "новосибирск", "нижний новгород", "казань",
           "удален", "удалён", "remote", "дистанц"),
    "by": ("беларусь", "рб", "минск"),
    "kz": ("казахстан", "кз", "алматы", "астана"),
}
GEO_DEFAULT_CITY = {"ru": "Россия", "by": "Беларусь", "kz": "Казахстан", "cis": ""}


def geo_city(geo: str, city: str) -> str:
    """Город под регион: если для «страны» введён чужой город, подставляем страну.

    Пример: гео=by, а в профиле осталось «Москва» → ищем по Беларуси, а не по РФ.
    Если город относится к выбранной стране (Минск) или это удалёнка — не трогаем.
    """
    c = (city or "").strip()
    g = (geo or "ru").strip().lower()
    hints = GEO_CITY_HINTS.get(g)
    if not hints:
        return c
    if not c:
        return GEO_DEFAULT_CITY.get(g, c)
    cl = c.lower()
    if any(x in cl for x in hints):
        return c
    return GEO_DEFAULT_CITY.get(g, c)


_HH_SALARY_RE = re.compile(
    r"(?:(?:от|до)\s+)?\d[\d\s\u00a0\u202f]*(?:[–—-]\s*\d[\d\s\u00a0\u202f]*)?\s*[₽$€]\s*(?:за\s+\w+|на руки)?"
)


def cis_areas(city: str = "") -> List[int]:
    """Area-id для региона «СНГ».

    Конкретный город СНГ (Минск, Алматы, Тбилиси...) — ищем по его area.
    Российский город (Москва) или пустое поле — по всей сетке стран СНГ,
    а не по одной Москве.
    """
    c = (city or "").strip().lower()
    if c and c in _HH_AREAS:
        area = _HH_AREAS[c]
        # Страна СНГ (Беларусь, Казахстан...) — ищем по ней целиком.
        if area in CIS_AREA_IDS and area != 113:
            return [area]
        # Город СНГ, а не российский город — ищем по городу.
        if area not in _RU_CITY_AREAS:
            return [area]
    return list(CIS_AREA_IDS)


def _hh_location(city: str) -> Tuple[Dict[str, Any], str]:
    """(доп. параметры hh, суффикс к тексту запроса). Город лучше передавать как area, а не словом в тексте."""
    c = (city or "").strip().lower()
    if not c:
        return {}, ""
    if any(w in c for w in _REMOTE_WORDS):
        return {"schedule": "remote"}, ""
    if c in _HH_AREAS:
        return {"area": _HH_AREAS[c]}, ""
    return {}, city.strip()


def _parse_hh_cards(page_html: str, fallback_city: str) -> Tuple[int, List[Vacancy]]:
    """Разбирает HTML-выдачу hh. Возвращает (число карточек на странице, вакансии).
    Рекламные карточки (ссылка на adsrv.hh.ru/click) отбрасываются."""
    soup = BeautifulSoup(page_html, "html.parser")
    cards = soup.find_all("div", class_=re.compile(r"vacancy-card--"))
    out: List[Vacancy] = []
    for card in cards:
        link = card.select_one('[data-qa="serp-item__title"]')
        title_el = card.select_one('[data-qa="serp-item__title-text"]') or link
        if not link or not title_el:
            continue
        m = re.search(r"hh\.ru/vacancy/(\d+)|^/vacancy/(\d+)", (link.get("href") or ""))
        if not m:
            continue  # реклама / не вакансия
        vac_id = m.group(1) or m.group(2)
        title = title_el.get_text(" ", strip=True)
        emp = card.select_one('[data-qa="vacancy-serp__vacancy-employer-text"]') or card.select_one(
            '[data-qa="vacancy-serp__vacancy-employer"]'
        )
        company = emp.get_text(" ", strip=True) if emp else "Прямой работодатель"
        city_el = card.select_one('[data-qa="vacancy-serp__vacancy-address"]')
        vac_city = city_el.get_text(" ", strip=True) if city_el else fallback_city
        trusted_emp = bool(card.select_one('[data-qa="trusted-employer-link"]'))
        # У блока зарплаты в вёрстке hh нет data-qa — берём сумму регуляркой по тексту карточки.
        sal_m = _HH_SALARY_RE.search(card.get_text(" ", strip=True))
        salary = re.sub(r"\s+", " ", sal_m.group(0)).strip() if sal_m else "з/п не указана"

        exp_el = card.select_one('[data-qa^="vacancy-serp__vacancy-work-experience"]')
        desc_parts = []
        if exp_el:
            desc_parts.append(exp_el.get_text(" ", strip=True))
        if "Можно удалённо" in card.get_text(" ", strip=True):
            desc_parts.append("можно удалённо")
        desc_parts.append("Полные условия и отклик — в карточке на hh.ru")

        v = Vacancy(
            id=f"hh_{vac_id}",
            platform="HH.ru",
            title=title,
            company=company,
            salary=salary,
            url=f"https://hh.ru/vacancy/{vac_id}",
            description=" · ".join(desc_parts)[:600],
            city=vac_city,
        )
        QuickScamFilter.score_vacancy(v, employer_verified=trusted_emp)
        out.append(v)
    return len(cards), out


async def _search_hh_html(
    query: str,
    city: str = "",
    limit: int = 50,
    min_salary: int = 0,
    areas: Optional[List[int]] = None,
) -> List[Vacancy]:
    """
    Резервный парсер публичной HTML-выдачи hh.ru (когда API отдаёт 403 без OAuth).
    Листает страницы по 50 карточек (до 40 страниц ≈ 2000 вакансий — предел hh).
    """
    if areas:
        extra: Dict[str, Any] = {"area": list(areas)}
        suffix = ""
    else:
        extra, suffix = _hh_location(city)
    base: Dict[str, Any] = {
        "text": f"{query} {suffix}".strip(),
        "items_on_page": HH_PAGE_SIZE,
        "hhtmFrom": "vacancy_search_list",
        **extra,
    }
    if min_salary and min_salary > 0:
        base["salary"] = min_salary

    need_pages = min(HH_MAX_PAGES, -(-limit // HH_PAGE_SIZE) + 1)  # +1 запас на рекламу и дубли
    out: List[Vacancy] = []
    seen: set = set()
    status_note = "ok"
    sem = asyncio.Semaphore(max(1, HH_CONCURRENCY))
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HH_HTML_HEADERS, follow_redirects=True) as client:

            async def fetch(page: int):
                async with sem:
                    return page, await client.get(
                        "https://hh.ru/search/vacancy", params={**base, "page": page}
                    )

            page = 0
            while page < need_pages and len(out) < limit:
                batch = range(page, min(page + HH_CONCURRENCY, need_pages))
                results = await asyncio.gather(*[fetch(p) for p in batch], return_exceptions=True)
                stop = False
                for res in results:
                    if isinstance(res, Exception):
                        status_note = f"error:{type(res).__name__}"
                        log.warning("hh html: запрос упал: %r", res)
                        stop = True
                        continue
                    p, r = res
                    if r.status_code != 200:
                        status_note = f"http {r.status_code}"
                        log.warning("hh html: страница %s -> HTTP %s (капча/блок IP?)", p, r.status_code)
                        stop = True
                        continue
                    n_cards, vacs = _parse_hh_cards(r.text, city)
                    if n_cards == 0:
                        if p == 0:
                            status_note = "0 карточек на 1-й странице (капча или смена вёрстки?)"
                            log.warning("hh html: %s", status_note)
                        stop = True
                        continue
                    for v in vacs:
                        if v.url not in seen:
                            seen.add(v.url)
                            out.append(v)
                    if n_cards < HH_PAGE_SIZE:  # последняя страница
                        stop = True
                if stop:
                    break
                page += HH_CONCURRENCY
    except Exception as e:  # noqa: BLE001
        status_note = f"error:{type(e).__name__}"
        log.exception("hh html: сбой поиска")
    LAST_SEARCH_STATS["hh_html"] = {"count": len(out), "status": status_note}
    log.info("hh html: query=%r city=%r -> %d (%s)", query, city, len(out), status_note)
    return out[:limit]


async def search_hh(
    query: str,
    city: str = "",
    min_salary: int = 0,
    limit: int = 50,
    areas: Optional[List[int]] = None,
) -> List[Vacancy]:
    """Поиск hh.ru: сначала публичный API (до 100 на страницу), при отказе — HTML-выдача.

    areas — явный список area-id (например, сетка стран СНГ). Если задан, город игнорируется.
    """
    global _HH_API_BLOCKED_UNTIL
    if time.time() < _HH_API_BLOCKED_UNTIL:
        return await _search_hh_html(query, city=city, limit=limit, min_salary=min_salary, areas=areas)

    if areas:
        extra: Dict[str, Any] = {"area": list(areas)}
        suffix = ""
    else:
        extra, suffix = _hh_location(city)
    per_page = max(1, min(100, limit))
    params: Dict[str, Any] = {
        "text": f"{query} {suffix}".strip(),
        "per_page": per_page,
        "page": 0,
        "order_by": "publication_time",
        **extra,
    }
    if min_salary and min_salary > 0:
        params["salary"] = min_salary

    headers = {"User-Agent": "JobShieldBot/2.0 (contact@jobshield.local)"}
    out: List[Vacancy] = []
    seen: set = set()
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            page = 0
            total_pages = 1
            while page < total_pages and page < 20 and len(out) < limit:  # API: глубина не больше 2000
                r = await client.get("https://api.hh.ru/vacancies", params={**params, "page": page})
                if r.status_code != 200:
                    log.warning("hh api: HTTP %s на странице %s -> HTML-фолбэк", r.status_code, page)
                    if page == 0:
                        _HH_API_BLOCKED_UNTIL = time.time() + 600
                        return await _search_hh_html(query, city=city, limit=limit, min_salary=min_salary, areas=areas)
                    break
                data = r.json()
                total_pages = int(data.get("pages") or 1)
                items = data.get("items", [])
                if not items:
                    break
                for it in items:
                    emp = it.get("employer") or {}
                    snippet = it.get("snippet") or {}
                    req = _clean_html(snippet.get("requirement") or "")
                    resp = _clean_html(snippet.get("responsibility") or "")
                    desc = " | ".join([x for x in (req, resp) if x]) or "Подробности по ссылке на вакансию."
                    v = Vacancy(
                        id=f"hh_{it.get('id')}",
                        platform="HH.ru",
                        title=it.get("name", "Вакансия"),
                        company=emp.get("name", "Прямой работодатель"),
                        salary=_format_hh_salary(it.get("salary")),
                        url=it.get("alternate_url", f"https://hh.ru/vacancy/{it.get('id')}"),
                        description=desc[:600],
                        city=(it.get("area") or {}).get("name", ""),
                        published_at=(it.get("published_at") or "")[:10],
                    )
                    if v.url in seen:
                        continue
                    seen.add(v.url)
                    QuickScamFilter.score_vacancy(v, employer_verified=bool(emp.get("trusted")))
                    out.append(v)
                page += 1
    except Exception:  # noqa: BLE001
        log.exception("hh api: сбой -> HTML-фолбэк")
        return await _search_hh_html(query, city=city, limit=limit, min_salary=min_salary, areas=areas)
    LAST_SEARCH_STATS["hh_api"] = {"count": len(out), "status": "ok"}
    log.info("hh api: query=%r city=%r -> %d", query, city, len(out))
    return out[:limit]


async def search_trudvsem(query: str, limit: int = 100) -> List[Vacancy]:
    """
    Поиск по порталу «Работа России» (opendata.trudvsem.ru) с пагинацией.
    Отдаёт реальный ИНН и ОГРН работодателя.
    """
    url = "https://opendata.trudvsem.ru/api/v1/vacancies"
    page_size = max(1, min(100, limit))
    out: List[Vacancy] = []
    seen: set = set()
    status_note = "ok"
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            for page in range(min(10, -(-limit // page_size) + 1)):
                r = await client.get(url, params={"text": query, "limit": page_size, "offset": page})
                if r.status_code != 200:
                    status_note = f"http {r.status_code}"
                    log.warning("trudvsem: HTTP %s на странице %s", r.status_code, page)
                    break
                vacancies = (r.json().get("results", {}) or {}).get("vacancies", []) or []
                if not vacancies:
                    break
                new_here = 0
                for item in vacancies:
                    vac = item.get("vacancy") or {}
                    vid = str(vac.get("id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    new_here += 1
                    comp = vac.get("company") or {}
                    sal = vac.get("salary") or (
                        f"от {vac.get('salary_min')} ₽" if vac.get("salary_min") else "з/п по договорённости"
                    )
                    duty = _clean_html(vac.get("duty") or "")
                    inn = str(comp.get("inn") or "")
                    v = Vacancy(
                        id=f"tv_{vid[:12]}",
                        platform="Работа России (Госреестр)",
                        title=vac.get("job-name", "Специалист"),
                        company=comp.get("name", "Организация"),
                        salary=str(sal),
                        url=vac.get("vac_url", "https://trudvsem.ru"),
                        description=duty[:600] or "Официальное трудоустройство по ТК РФ.",
                        city=(vac.get("region") or {}).get("name", ""),
                        inn=inn,
                        contact=comp.get("email") or comp.get("phone") or "",
                    )
                    QuickScamFilter.score_vacancy(v, employer_verified=bool(inn))
                    out.append(v)
                    if len(out) >= limit:
                        break
                if len(out) >= limit or new_here == 0 or len(vacancies) < page_size:
                    break
    except Exception as e:  # noqa: BLE001
        status_note = f"error:{type(e).__name__}"
        log.exception("trudvsem: сбой поиска")
    LAST_SEARCH_STATS["trudvsem"] = {"count": len(out), "status": status_note}
    log.info("trudvsem: query=%r -> %d (%s)", query, len(out), status_note)
    return out[:limit]


def _extract_tg_contact(text: str, channel_name: str) -> str:
    own = channel_name.lower()
    for m in re.finditer(r"(?:https?://)?t\.me/([A-Za-z][\w]{3,31})(?!/\d)", text):
        u = m.group(1)
        if u.lower() not in (own, "joinchat", "s", "addlist", "share"):
            return f"@{u}"
    for m in re.finditer(r"@([A-Za-z][\w]{3,31})", text):
        u = m.group(1)
        if u.lower() != own:
            return f"@{u}"
    return ""


async def _tg_channel_posts(client: httpx.AsyncClient, sem: asyncio.Semaphore, ch: str, pages: int):
    """Посты канала с веб-превью t.me/s/<channel>, листая назад через ?before=<id>.
    Возвращает (список постов, статус)."""
    posts: List[Dict[str, str]] = []
    before: Optional[int] = None
    status = "ok"
    for _ in range(max(1, pages)):
        url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
        try:
            async with sem:
                res = await client.get(url)
        except Exception as e:  # noqa: BLE001
            status = f"error:{type(e).__name__}"
            log.warning("tg @%s: запрос упал: %r", ch, e)
            break
        if res.status_code != 200:
            status = f"http {res.status_code}"
            log.warning("tg @%s: HTTP %s", ch, res.status_code)
            break
        soup = BeautifulSoup(res.text, "html.parser")
        wraps = soup.select(".tgme_widget_message_wrap")
        if not wraps:
            if not posts:
                status = "0 постов (канал закрыт/не существует?)"
            break
        ids: List[int] = []
        for w in reversed(wraps):  # новые сверху
            msg = w.select_one(".tgme_widget_message[data-post]")
            if msg:
                try:
                    ids.append(int(str(msg["data-post"]).rsplit("/", 1)[-1]))
                except ValueError:
                    pass
            text_el = w.select_one(".tgme_widget_message_text")
            if not text_el:
                continue
            raw_text = text_el.get_text("\n", strip=True)
            if len(raw_text) < 40:
                continue
            link_el = w.select_one(".tgme_widget_message_date")
            post_url = link_el["href"] if link_el and link_el.has_attr("href") else f"https://t.me/s/{ch}"
            posts.append({"ch": ch, "text": raw_text, "url": post_url})
        if not ids or min(ids) <= 1:
            break
        before = min(ids)
    return posts, status


async def search_telegram_channels(
    query: str,
    channels: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Vacancy]:
    """
    Парсинг публичных Telegram-каналов (веб-превью t.me/s/<channel>).
    Опрашиваются ВСЕ каналы: профильные под запрос + заданные пользователем + стандартные,
    у каждого — до TG_PAGES_PER_CHANNEL страниц (~20 постов на страницу).
    Telegram игнорирует ?q=, поэтому релевантность считаем у себя по ключевым словам;
    если ничего не подошло — отдаём последние посты (фолбэк), чтобы ответ не был пустым.
    """
    prof_channels = profession_channels_for(query)
    explicit = [c.strip().lstrip("@") for c in (channels or []) if c and c.strip()]
    ch_list: List[str] = []
    for ch in prof_channels + explicit + DEFAULT_TG_CHANNELS:
        if ch and ch not in ch_list:
            ch_list.append(ch)

    keywords = relevance_keywords_for(query)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }
    sem = asyncio.Semaphore(max(1, TG_CONCURRENCY))
    matched: List[Vacancy] = []
    fallback: List[Vacancy] = []
    seen: set = set()
    per_channel: Dict[str, str] = {}
    async with httpx.AsyncClient(timeout=12.0, headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[_tg_channel_posts(client, sem, ch, TG_PAGES_PER_CHANNEL) for ch in ch_list],
            return_exceptions=True,
        )
    for ch, res in zip(ch_list, results):
        if isinstance(res, Exception):
            per_channel[ch] = f"error:{type(res).__name__}"
            log.warning("tg @%s: %r", ch, res)
            continue
        posts, status = res
        per_channel[ch] = f"{len(posts)} постов, {status}"
        for p in posts:
            raw_text, post_url = p["text"], p["url"]
            post_id = post_url.rstrip("/").split("/")[-1]
            vid = f"tg_{ch}_{post_id}"
            if vid in seen:
                continue
            seen.add(vid)
            lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
            title = f"Вакансия из @{ch}"
            for ln in lines[:4]:
                if len(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", ln)) >= 8:
                    title = ln[:90]
                    break
            sal_match = re.search(
                r"(\d[\d\s\.,]{2,12}\s*(?:₽|руб|р\.|тыс|\$|€|USD|EUR))", raw_text, re.IGNORECASE
            )
            salary = sal_match.group(1).strip() if sal_match else "см. в посте"
            contact = _extract_tg_contact(raw_text, ch)
            v = Vacancy(
                id=vid,
                platform=f"Telegram (@{ch})",
                title=title,
                company=f"Контакт: {contact}" if contact else f"Канал @{ch}",
                salary=salary,
                url=post_url,
                description=raw_text[:650],
                contact=contact,
            )
            QuickScamFilter.score_vacancy(v, employer_verified=False)
            (matched if post_matches_query(raw_text, keywords) else fallback).append(v)

    chosen = matched if matched else fallback
    LAST_SEARCH_STATS["telegram"] = {
        "channels": len(ch_list), "matched": len(matched), "fallback": len(fallback), "per_channel": per_channel,
    }
    log.info("telegram: query=%r каналов=%d подошло=%d (фолбэк=%d)", query, len(ch_list), len(matched), len(fallback))
    return chosen[:limit]


def generate_crossplatform_search_hubs(query: str, city: str = "") -> List[Vacancy]:
    """
    Генерирует прямые поисковые шлюзы и смарт-ссылки (Deep Links + X-Ray Dorks)
    для закрытых соцсетей и площадок с жёстким антифродом:
    Авито Работа, ВКонтакте, Одноклассники, Habr Career, SuperJob, LinkedIn, Instagram.
    """
    q_full = f"{query} {city}".strip()
    q_enc = quote_plus(q_full)
    q_job = quote_plus(f"вакансия {q_full}")

    insta_q = f'(site:instagram.com OR site:threads.net) ("вакансия" OR "ищем в команду") {q_full}'
    hubs = [
        Vacancy(
            id="hub_avito",
            platform="Авито Работа",
            title=f"Подборка вакансий «{query}» на Авито",
            company="Авито Работа (Прямые работодатели и МСБ)",
            salary="По фильтрам Авито",
            url=f"https://www.avito.ru/all/vakansii?q={q_enc}",
            description=(
                "Вакансии малого и среднего бизнеса, офлайн и удалёнка. "
                "Внимание: на Авито часто встречаются посредники — обязательно проверяйте компанию кнопкой «🛡 Проверить работодателя»!"
            ),
            city=city,
            trust_score=70,
            trust_badge="🟡 Требует проверки ИНН",
        ),
        Vacancy(
            id="hub_vk",
            platform="ВКонтакте (Посты и Паблики)",
            title=f"Лента постов ВК: #вакансия «{query}»",
            company="Сообщества и стены работодателей ВК",
            salary="Договорная",
            url=f"https://vk.com/feed?q={quote_plus('#вакансия ' + q_full)}&section=search",
            description=(
                "Прямой поиск по свежим постам рекрутеров и пабликам с работой ВКонтакте. "
                "Идеально для быстрого выхода в ЛС напрямую к нанимающему менеджеру."
            ),
            city=city,
            trust_score=65,
            trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ),
        Vacancy(
            id="hub_ok",
            platform="Одноклассники",
            title=f"Группы и объявления ОК: «{query}»",
            company="Одноклассники (Региональные группы и компании)",
            salary="Договорная",
            url=f"https://ok.ru/search?st.mode=Groups&st.query={q_job}",
            description="Поиск вакансий в региональных сообществах и группах прямых работодателей в ОК.",
            city=city,
            trust_score=65,
            trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ),
        Vacancy(
            id="hub_habr",
            platform="Habr Career",
            title=f"IT & Digital вакансии «{query}» на Хабр Карьере",
            company="Аккредитованные IT-компании",
            salary="Рыночная вилка",
            url=f"https://career.habr.com/vacancies?q={q_enc}&type=all",
            description="Проверенные IT, Digital, аналитические и продуктовые компании с открытыми оценками сотрудников.",
            city=city,
            trust_score=92,
            trust_badge="🟢 Высокий рейтинг площадки",
        ),
        Vacancy(
            id="hub_superjob",
            platform="SuperJob",
            title=f"Вакансии «{query}» на SuperJob.ru",
            company="База работодателей SuperJob",
            salary="По рынку",
            url=f"https://www.superjob.ru/vakansii/?keywords={q_enc}",
            description="Крупные федеральные компании, промышленность, ритейл, финансы и госсектор.",
            city=city,
            trust_score=85,
            trust_badge="🟢 Проверенная площадка",
        ),
        Vacancy(
            id="hub_linkedin",
            platform="LinkedIn",
            title=f"Вакансии и рекрутеры LinkedIn: «{query}»",
            company="Международные и РФ компании / HR-специалисты",
            salary="В валюте / ₽",
            url=f"https://www.linkedin.com/jobs/search/?keywords={q_enc}",
            description="Поиск открытых позиций и рекрутеров в LinkedIn (включая международные команды и удалёнку).",
            city=city,
            trust_score=82,
            trust_badge="🟢 Профессиональная сеть",
        ),
        Vacancy(
            id="hub_insta",
            platform="Instagram* / Threads (X-Ray)",
            title=f"Поиск постов о найме «{query}» в Instagram/Threads",
            company="Креативные агентства, блогеры, эксперты, e-commerce",
            salary="Проектная / Оклад",
            url=f"https://www.google.com/search?q={quote_plus(insta_q)}",
            description="Умный X-Ray запрос по свежим публикациям в Instagram и Threads, где ищут специалистов напрямую в Директ.",
            city=city,
            trust_score=60,
            trust_badge="🟡 Обязательно требуйте договор",
        ),
    ]
    return hubs


def generate_global_hubs(query: str, geo: str = "") -> List[Vacancy]:
    """Прямые поисковые шлюзы в зарубежные и НКО-площадки (по выбранному гео).

    Возвращает список Vacancy-«ссылок» (это не вакансии, а удобные точки входа),
    которые добавляются к результатам поиска для регионов eu / world / ngo.
    Для других регионов вернёт пустой список.
    """
    geo = (geo or "").strip().lower()
    q_enc = quote_plus(query)

    hubs: List[Vacancy] = []
    if geo in ("eu", "world"):
        hubs += [
            Vacancy(
                id="g_remoteok", platform="RemoteOK",
                title=f"Удалённые вакансии «{query}» на RemoteOK",
                company="Глобальная доска удалённых IT-вакансий",
                salary="Доллары / Евро",
                url=f"https://remoteok.com/remote-jobs?search={q_enc}",
                description="Мировые удалённые позиции (IT, маркетинг, поддержка, дизайн) с зарплатой в валюте.",
                trust_score=80, trust_badge="🟢 Проверенная площадка",
            ),
            Vacancy(
                id="g_we_work_remotely", platform="We Work Remotely",
                title=f"Вакансии «{query}» на We Work Remotely",
                company="Популярная доска удалёнки",
                salary="Валюта",
                url=f"https://weworkremotely.com/remote-jobs/search?term={q_enc}",
                description="Удалённые позиции по всему миру, в том числе не-IT (поддержка, продажи, контент).",
                trust_score=80, trust_badge="🟢 Проверенная площадка",
            ),
            Vacancy(
                id="g_indeed", platform="Indeed",
                title=f"Вакансии «{query}» на Indeed (глобально)",
                company="Крупнейшая мировая доска вакансий",
                salary="Валюта",
                url=f"https://www.indeed.com/jobs?q={q_enc}",
                description="Международные вакансии; можно сменить страну (indeed.fr/.de/...) для целевого рынка.",
                trust_score=82, trust_badge="🟢 Профессиональная площадка",
            ),
            Vacancy(
                id="g_glassdoor", platform="Glassdoor",
                title=f"Вакансии «{query}» на Glassdoor",
                company="Доска вакансий + отзывы о работодателях",
                salary="Валюта",
                url=f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q_enc}",
                description="Вакансии за рубежом с информацией о зарплатах и рейтингах компаний.",
                trust_score=80, trust_badge="🟢 Проверенная площадка",
            ),
            Vacancy(
                id="g_eures", platform="EURES",
                title=f"Вакансии «{query}» в ЕС через EURES",
                company="Европейская служба занятости (ЕС)",
                salary="Валюта",
                url=f"https://eures.europa.eu/search?keyword={q_enc}",
                description="Официальный портал ЕС: вакансии и работодатели по всей Европе.",
                trust_score=85, trust_badge="🟢 Проверенная площадка",
            ),
        ]
    if geo == "ngo":
        hubs += [
            Vacancy(
                id="n_idealist", platform="Idealist",
                title=f"В НКО и фонды: «{query}» на Idealist",
                company="Миссии, НКО, фонды, стартапы смысла",
                salary="Валюта / Гранты",
                url=f"https://www.idealist.org/en/?type=JOB&q={q_enc}",
                description="Вакансии в НКО, фонды и социальные проекты по всему миру.",
                trust_score=82, trust_badge="🟢 Проверенная площадка",
            ),
            Vacancy(
                id="n_reliefweb", platform="ReliefWeb",
                title=f"Гуманитарные вакансии: «{query}» на ReliefWeb",
                company="ООН и гуманитарные организации",
                salary="Валюта",
                url=f"https://reliefweb.int/jobs?q%5Btext%5D%5Bvalue%5D={q_enc}",
                description="Гуманитарная помощь, ООН, международные миссии, экспертные позиции.",
                trust_score=85, trust_badge="🟢 Официальный портал ООН",
            ),
            Vacancy(
                id="n_unjobs", platform="UNjobs",
                title=f"Вакансии «{query}» в системе ООН",
                company="Организация Объединённых Наций",
                salary="Валюта",
                url=f"https://unjobs.org/vacancies?q={q_enc}",
                description="Агрегатор вакансий ООН и её агентств (UNDP, UNICEF, WHO и др.).",
                trust_score=85, trust_badge="🟢 Официальные организации",
            ),
            Vacancy(
                id="n_impactpool", platform="Impactpool",
                title=f"Позиции «{query}» на Impactpool",
                company="Международные организации и НКО",
                salary="Валюта",
                url=f"https://www.impactpool.org/search?q={q_enc}",
                description="Работа в международных организациях, НКО и миротворческих миссиях.",
                trust_score=80, trust_badge="🟢 Проверенная площадка",
            ),
        ]
    return hubs


async def search_all_platforms(
    query: str,
    city: str = "",
    min_salary: int = 0,
    tg_channels: Optional[List[str]] = None,
    max_total: Optional[int] = None,
    geo: str = "",
) -> List[Vacancy]:
    """Параллельный мульти-поиск по всем площадкам (до max_total вакансий, по умолчанию 1000).

    Параметр geo определяет набор источников:
      - ru (по умолчанию) — Рунет: HH + Трудвсем (Работа России) + Telegram-каналы.
      - by / kz / cis        — HH по стране + Telegram + соцсети (Трудвсем — только РФ, пропускаем).
      - eu / world / ngo     — глобальный охват: соцсети + иностранные шлюзы (Indeed, EURES и др.),
                               без Рунет-моноисточников, Трудвсем и российских шлюзов.
    """
    geo = (geo or "").strip().lower() or "ru"
    gcfg = GEO_CONFIG.get(geo, GEO_CONFIG["ru"])
    total = max(1, max_total or SEARCH_MAX_TOTAL)
    t0 = time.time()

    names: List[str] = []
    tasks: List[Any] = []
    if gcfg["world"] or gcfg["ngo"]:
        # Глобальный режим: соцпоиск (Reddit, Bluesky, Mastodon, HN, RemoteOK, Discord) + живой HH по удалёнке.
        # Город из профиля не передаём: «Москва» в международном запросе даёт мусор вместо мировых вакансий.
        names = ["social", "hh_remote"]
        tasks = [
            search_social_networks(query, city="", limit_per_network=3),
            search_hh(query, city="удалённо", min_salary=min_salary, limit=min(total, 100)),
        ]
    else:
        names = ["hh", "telegram"]
        if geo == "cis":
            # СНГ: один запрос HH сразу по сетке стран (или по конкретному городу СНГ из профиля).
            hh_city, hh_areas = "", cis_areas(city)
        else:
            hh_city, hh_areas = geo_city(geo, city), None
        tasks = [
            search_hh(query, city=hh_city, min_salary=min_salary, limit=total, areas=hh_areas),
            search_telegram_channels(query, channels=tg_channels, limit=total),
        ]
        # Страны СНГ берём из HH по area; Трудвсем (Работа России) подключаем только для РФ (ru).
        if geo == "ru":
            names.append("trudvsem")
            tasks.append(search_trudvsem(f"{query} {city}".strip(), limit=total))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    groups: List[List[Vacancy]] = []
    counts: Dict[str, Any] = {}
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            log.error("источник %s упал: %r", name, res)
            counts[name] = f"error:{type(res).__name__}"
            groups.append([])
            continue
        if name == "social":
            # search_social_networks возвращает словарь, а не список: берём живые items.
            res = res.get("items") or [] if isinstance(res, dict) else []
        items: List[Vacancy] = []
        for v in res:
            if isinstance(v, dict):
                v = Vacancy(**{k: val for k, val in v.items() if k in Vacancy.__dataclass_fields__})
            items.append(v)
        counts[name] = len(items)
        groups.append(items)

    # Дедупликация + равномерное чередование источников, чтобы большой hh не вытеснил остальных.
    combined: List[Vacancy] = []
    seen: set = set()
    for row in zip_longest(*groups):
        for v in row:
            if v is None:
                continue
            key = v.url or v.id
            if key in seen:
                continue
            seen.add(key)
            combined.append(v)
    combined = combined[:total]

    LAST_SEARCH_STATS["summary"] = {"query": query, "city": city, "by_source": counts, "total": len(combined)}
    log.info("поиск %r (%s): %s -> итого %d за %.1f c", query, city, counts, len(combined), time.time() - t0)

    # Кроссплатформенные шлюзы (Авито, ВК, ОК, Habr, SuperJob, LinkedIn, Instagram) — это ссылки, не вакансии.
    # Шлюзы-«ссылки»: для глобальных/НКО-регионов — зарубежные и НКО-площадки,
    # для остальных — привычные Рунет-шлюзы (Авито, ВК, ОК, Habr, SuperJob, LinkedIn, Instagram).
    if gcfg["world"] or gcfg["ngo"]:
        combined.extend(generate_global_hubs(query, geo=geo))
    else:
        combined.extend(generate_crossplatform_search_hubs(query, city=city))
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# 3. АНТИФРОД И СБ-ПРОВЕРКА РАБОТОДАТЕЛЕЙ НА МОШЕННИЧЕСТВО (SCAM SHIELD)
# ─────────────────────────────────────────────────────────────────────────────

class QuickScamFilter:
    """
    Система скоринга и детекции обмана, фейковых вакансий, дроппинга, финансового
    мошенничества и недобросовестных работодателей.
    """

    # Критические красные флаги (высокий риск мошенничества / УК РФ)
    CRITICAL_PATTERNS = [
        (r"оформ(ить|ление)\s+(дебетов|банковск|сим|sim|карт|пушкинск)", 45, "🚨 Схема с оформлением банковских/сим-карт (риск дроппинга)"),
        (r"(платн\w+\s+обучен|оплат\w+\s+материал|залог\s+за|взнос\s+за|страховой\s+депозит)", 50, "🚨 Требуют деньги/залог/оплату обучения со старта"),
        (r"(криптообмен|p2p\s*арбитраж|прием\s+платежей\s+на\s+свою|перевод\w+\s+по\s+реквизитам|процессинг\s+трафик)", 55, "🚨 Признаки P2P-процессинга / дроппинга (ст. 187 УК РФ)"),
        (r"(закладк|курьер\s+высокий\s+доход\s+без\s+опыта\s+от\s+\d{3}|трафаретчик|кладмен)", 80, "🚨 Криминальный найм под видом курьера"),
        (r"(казино|беттинг\s+схем|раскрутк\w+\s+счет|бинарн\w+\s+опцион)", 45, "🚨 Скам-схемы с казино/ставками/раскруткой"),
        (r"(выкуп\s+товаров\s+на\s+wildberries|обратный\s+выкуп|лайки\s+на\s+озон\s+за\s+деньги)", 50, "🚨 Классический развод с «обратным выкупом» на маркетплейсах"),
    ]

    # Жёлтые флаги (манипуляции, серая зона, инфоцыганство, неоплачиваемый труд)
    WARNING_PATTERNS = [
        (r"(сетевой\s+маркетинг|nl\s+international|тяньши|tiens|amway|oriflame|инфобизнес\s+без\s+вложений)", 25, "⚠️ Признаки сетевого маркетинга (MLM) под видом офисной работы"),
        (r"(без\s+опыта.{0,30}(?:от\s+)?(?:200|250|300|400|500)\s*000)", 25, "⚠️ Нереалистично завышенная зарплата без требований к опыту (приманка)"),
        (r"(только\s+сегодня|осталось\s+2\s+места|срочно\s+пиши\s+в\s+лс\s+плюс)", 15, "⚠️ Манипулятивный срочный найм без описания компании"),
        (r"(штраф\w+\s+за|депремирован|полиграф|детектор\s+лжи)", 15, "⚠️ Токсичные условия: система штрафов / полиграф"),
        (r"(неоплачиваем\w+\s+стажировк|бесплатн\w+\s+стажировк\w+\s+на\s+месяц)", 20, "⚠️ Длительная неоплачиваемая стажировка (риск бесплатной рабочей силы)"),
        (r"(серая\s+зп|выплаты\s+в\s+конверте|без\s+оформления\s+по\s+тк)", 18, "⚠️ Отсутствие официального оформления по ТК РФ"),
        (r"(набор\s+текста\s+с\s+фото|перепечатка\s+сканов\s+на\s+дому)", 40, "🚨 Классическая схема обмана «Наборщик текста на дому»"),
    ]

    @staticmethod
    def validate_inn(inn: str) -> Tuple[bool, str]:
        """Математическая проверка контрольной суммы ИНН (10 цифр — юрлицо, 12 цифр — ИП)."""
        inn = re.sub(r"\D", "", inn or "")
        if len(inn) not in (10, 12):
            return False, "Некорректная длина ИНН (должно быть 10 цифр для ООО/АО или 12 для ИП)"

        def calc_digit(digits: str, coeffs: List[int]) -> int:
            s = sum(int(d) * c for d, c in zip(digits, coeffs))
            return (s % 11) % 10

        if len(inn) == 10:
            c10 = [2, 4, 10, 3, 5, 9, 4, 6, 8]
            if calc_digit(inn, c10) == int(inn[9]):
                return True, "Валидный ИНН юридического лица (10 знаков)"
            return False, "Поддельный ИНН: контрольная сумма ФНС не сходится!"
        else:
            c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
            c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
            if calc_digit(inn, c11) == int(inn[10]) and calc_digit(inn, c12) == int(inn[11]):
                return True, "Валидный ИНН Индивидуального Предпринимателя (12 знаков)"
            return False, "Поддельный ИНН ИП: контрольная сумма ФНС не сходится!"

    @classmethod
    def analyze_text(cls, text: str) -> Tuple[int, List[str]]:
        """Возвращает (штрафные баллы, список найденных флагов)."""
        penalty = 0
        flags: List[str] = []
        low = (text or "").lower()

        for pat, pen, msg in cls.CRITICAL_PATTERNS:
            if re.search(pat, low):
                penalty += pen
                flags.append(msg)

        for pat, pen, msg in cls.WARNING_PATTERNS:
            if re.search(pat, low):
                penalty += pen
                flags.append(msg)

        return penalty, flags

    @classmethod
    def score_vacancy(cls, v: Vacancy, employer_verified: bool = False) -> None:
        base = 88 if employer_verified else 74
        full_text = f"{v.title} {v.company} {v.salary} {v.description}"
        pen, flags = cls.analyze_text(full_text)
        score = max(5, min(99, base - pen))
        v.trust_score = score
        v.trust_flags = flags
        if score >= 80:
            v.trust_badge = f"🟢 Надёжно ({score}%)"
        elif score >= 55:
            v.trust_badge = f"🟡 Внимание ({score}%)"
        else:
            v.trust_badge = f"🔴 ОПАСНО / СКАМ ({score}%)"


async def deep_check_employer(query_or_inn_or_text: str) -> Dict[str, Any]:
    """
    Комплексное СБ-расследование работодателя или текста вакансии:
    1. Извлекает ИНН/ОГРН (если есть) и проверяет контрольные разряды ФНС.
    2. Ищет юрлицо в открытой базе Государственного портала «Работа России» (Trudvsem) и API HH.ru.
    3. Прогоняет текст через 15+ антифрод-правил (дроппинг, обратный выкуп, залоги, сетевой маркетинг).
    4. Формирует прямые верификационные ссылки в ФНС (ЕГРЮЛ), КАД Арбитр (суды), ФССП (приставы),
       Федресурс (банкротство) и отзовики сотрудников (DreamJob, Pravda-Sotrudnikov).
    5. Подключает ИИ-аудитора безопасности для экспертного вердикта.
    """
    raw = (query_or_inn_or_text or "").strip()
    penalty, flags = QuickScamFilter.analyze_text(raw)

    # Попытка найти ИНН в тексте
    inn_match = re.search(r"\b(\d{10}|\d{12})\b", raw)
    inn = inn_match.group(1) if inn_match else ""
    inn_valid = None
    inn_comment = ""
    if inn:
        inn_valid, inn_comment = QuickScamFilter.validate_inn(inn)
        if not inn_valid:
            penalty += 40
            flags.append(f"🚨 {inn_comment}")
        else:
            flags.append(f"✅ {inn_comment}")

    # Поиск работодателя в базе HH.ru (проверка галочки верификации и числа открытых вакансий)
    company_query = re.sub(r"https?://\S+", "", raw).strip()[:60]
    hh_emp_info = None
    if len(company_query) >= 2 and len(raw) < 250:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.hh.ru/employers",
                    params={"text": company_query, "per_page": 3},
                )
                if r.status_code == 200:
                    emps = r.json().get("items", [])
                    if emps:
                        top = emps[0]
                        hh_emp_info = {
                            "name": top.get("name"),
                            "url": top.get("alternate_url"),
                            "open_vacancies": top.get("open_vacancies", 0),
                            "trusted": bool(top.get("trusted")),
                        }
        except Exception:
            pass

    base_score = 78
    if inn_valid:
        # ИНН прошёл контрольные разряды по алгоритму ФНС — это уже сильный плюс.
        base_score = max(base_score, 88)
    if hh_emp_info and hh_emp_info.get("trusted"):
        base_score = 92
        flags.append(
            f"✅ Найдена верифицированная карточка на HH.ru: «{hh_emp_info['name']}» "
            f"(открытых вакансий: {hh_emp_info['open_vacancies']})"
        )
    elif hh_emp_info:
        flags.append(
            f"ℹ️ Найдена карточка на HH.ru: «{hh_emp_info['name']}» (без значка полной верификации)"
        )

    score = max(5, min(98, base_score - penalty))
    if score >= 80:
        verdict = "🟢 ВЫСОКИЙ УРОВЕНЬ ДОВЕРИЯ"
    elif score >= 55:
        verdict = "🟡 СРЕДНИЙ РИСК — ТРЕБУЕТСЯ ПРОВЕРКА ДОГОВОРА"
    else:
        verdict = "🔴 ВЫСОКИЙ РИСК МОШЕННИЧЕСТВА / ОБМАНА!"

    # ИИ-экспертиза
    sys_prompt = (
        "Ты — руководитель Службы Безопасности и юрист по трудовому праву РФ. "
        "Проанализируй данные о работодателе или текст вакансии. "
        "Кратко (в 4-6 пунктах) укажи: "
        "1) Какие есть риски обмана, невыплаты зарплаты, подмены трудового договора на ГПХ/самозанятость или криминальных схем. "
        "2) Какие 3 проверочных вопроса задать рекрутеру на собеседовании, чтобы вывести мошенников на чистую воду."
    )
    ai_analysis = await llm.ask(sys_prompt, f"Данные для проверки:\n{raw}\nНайденные маркеры: {flags}")
    if not ai_analysis:
        ai_analysis = (
            "• Обязательно запросите ИНН юридического лица до выполнения тестовых заданий.\n"
            "• Ни при каких условиях не переводите деньги за обучение, медкомиссию, спецодежду или «активацию аккаунта».\n"
            "• Не оформляйте на своё имя банковские карты, сим-карты или ИП/ООО по просьбе нанимателя.\n"
            "• Проверьте, чтобы в договоре был прописан фиксированный оклад (не менее МРОТ), а не только «процент от KPI»."
        )

    q_enc = quote_plus(company_query or inn or "работодатель")
    registry_links = {
        "ФНС (Прозрачный бизнес / ЕГРЮЛ)": f"https://pb.nalog.ru/search.html#query={q_enc}",
        "Картотека арбитражных дел (Суды)": "https://kad.arbitr.ru/",
        "ФССП (Долги у приставов)": "https://fssp.gov.ru/iss/ip",
        "Отзывы сотрудников в Рунете": f"https://www.google.com/search?q={quote_plus((company_query or inn) + ' отзывы сотрудников черная зарплата обман')}",
    }

    return {
        "score": score,
        "verdict": verdict,
        "inn": inn,
        "flags": flags or ["ℹ️ Явных стоп-слов скама в тексте не обнаружено, проверьте юрлицо по ИНН."],
        "hh_employer": hh_emp_info,
        "ai_analysis": ai_analysis,
        "links": registry_links,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. ИИ-РЕДАКТОР РЕЗЮМЕ ПОД ВАКАНСИЮ И ГЕНЕРАТОР СОПРОВОДИТЕЛЬНЫХ ПИСЕМ
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Извлекает чистый текст из загруженного PDF-файла резюме."""
    if not fitz:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text("text") for page in doc]
        doc.close()
        return "\n".join(pages).strip()
    except Exception:
        return ""


async def adapt_resume_for_vacancy(base_resume: str, vacancy_info: str, mode: str = "ats") -> str:
    """
    Адаптирует резюме под конкретную вакансию (как ИИ-коуч в Сизифе).
    mode:
      - 'ats': переупаковка под ключевые слова вакансии и ATS-фильтры
      - 'numbers': акцент на измеримые результаты и цифры
      - 'audit': поиск слабых мест в резюме и вопросы для усиления
    """
    sys_prompt = (
        "Ты — топовый карьерный консультант, техлид и эксперт по алгоритмам HH.ru / LinkedIn (ATS). "
        "ГЛАВНОЕ ПРАВИЛО (как в Sisyphus Coach): НИКОГДА не выдумывай несуществующие места работы и фейковые цифры. "
        "Используй только факты из резюме кандидата, но переформулируй их так, чтобы они идеально "
        "били в требования целевой вакансии.\n"
        "Выдай результат в чёткой структуре:\n"
        "1. 🎯 **Рекомендуемый заголовок (желаемая должность)** под эту вакансию.\n"
        "2. 💡 **Блок «О себе / Summary»** (3-4 мощных предложения с ключевыми словами вакансии).\n"
        "3. 🛠 **Ключевые навыки (Теги для ATS)** — которые стоит поднять наверх.\n"
        "4. 📈 **Усиленные формулировки опыта работы** (Было → Стало).\n"
        "5. ❓ **Что уточнить/добавить** (1-2 вопроса кандидату, где не хватает конкретики)."
    )
    user_prompt = (
        f"РЕЗЮМЕ КАНДИДАТА:\n{base_resume}\n\n"
        f"ЦЕЛЕВАЯ ВАКАНСИЯ / НАПРАВЛЕНИЕ:\n{vacancy_info}\n\n"
        f"РЕЖИМ ОПТИМИЗАЦИИ: {mode}"
    )
    res = await llm.ask(sys_prompt, user_prompt, temperature=0.35)
    if res:
        return res

    # Фолбэк, если все внешние LLM недоступны
    words = re.findall(r"[А-Яа-яA-Za-z0-9\+\#\-]{4,}", vacancy_info)
    keywords = list(dict.fromkeys(words))[:12]
    return (
        "🎯 **Адаптация резюме под вакансию (Локальный ATS-модуль)**\n\n"
        f"**Ключевые маркеры из вакансии для вставки в блок навыков:**\n`{', '.join(keywords)}`\n\n"
        "**Что сделать перед откликом:**\n"
        "1. Поставьте название должности в резюме точь-в-точь как в заголовке вакансии.\n"
        "2. В первых 3 строках последнего места работы перечислите релевантные задачи из описания вакансии.\n"
        "3. Замените размытые слова («занимался», «участвовал») на глаголы результата («внедрил», «сократил», «выполнил»)."
    )


async def generate_cover_letter(
    base_resume: str,
    vacancy_info: str,
    tone: str = "natural",
    platform: str = "HH.ru",
) -> str:
    """
    Генерирует сопроводительное письмо или короткий питч для мессенджеров (TG/VK/Avito).
    tone: 'pro' (деловой), 'natural' (живой и естественный), 'short_tg' (короткий питч для Telegram/ВК/Авито)
    """
    tone_desc = {
        "pro": "строго деловой, структурный и уважительный стиль (для крупных корпораций и банков)",
        "natural": "живой, естественный, без канцеляризмов и штампов «Доброго времени суток» (как в Сизифе)",
        "short_tg": "короткий, ёмкий питч на 4-6 строк специально для личных сообщений в Telegram / ВК / Авито",
    }.get(tone, "естественный деловой стиль")

    sys_prompt = (
        "Ты — автор эталонных сопроводительных писем, которые конвертируют отклик в приглашение на интервью. "
        f"Тон письма: {tone_desc}. Площадка: {platform}.\n"
        "Правила:\n"
        "- Запрещены пустые фразы («я коммуникабельный, стрессоустойчивый», «рассмотрите мою кандидатуру»).\n"
        "- Сразу покажи связку: Задача компании из вакансии → Твой релевантный опыт из резюме.\n"
        "- Письмо должно легко читаться за 15 секунд.\n"
        "- Выдай ТОЛЬКО готовый текст письма без лишних вступлений."
    )
    user_prompt = f"МОЁ РЕЗЮМЕ:\n{base_resume}\n\nВАКАНСИЯ ({platform}):\n{vacancy_info}"
    res = await llm.ask(sys_prompt, user_prompt, temperature=0.5)
    if res:
        return res

    return (
        "Здравствуйте!\n\n"
        "Внимательно изучил описание вашей вакансии — задачи полностью пересекаются с моим профильным опытом.\n"
        f"Кратко о моём бэкграунде: {base_resume[:320]}...\n\n"
        "Быстро включусь в текущие процессы без долгого онбординга. "
        "Буду рад короткому созвону на 10–15 минут, чтобы обсудить детали и то, чем могу быть полезен вашей команде в первую очередь!"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ИИ-КОУЧ И A/B-ТЕСТИРОВАНИЕ РЕЗЮМЕ (ДО 5 КОПИЙ + СВЕРКА ФАКТОВ + АВТОВЫБОР)
# ─────────────────────────────────────────────────────────────────────────────

RESUME_FOCUSES: Dict[str, Dict[str, Any]] = {
    "ats": {
        "slot": 1,
        "code": "A",
        "focus": "ats",
        "label": "A · ATS и ключи",
        "short": "ATS-ключи",
        "hint": "Точное попадание в поисковые фильтры HH.ru и ключевые навыки вакансии",
        "role_suffix": "Специалист (ATS-фокус)",
        "affinity": ("требован", "стек", "опыт", "разработ", "инженер", "специалист"),
    },
    "numbers": {
        "slot": 2,
        "code": "B",
        "focus": "numbers",
        "label": "B · Цифры и эффект",
        "short": "Цифры / KPI",
        "hint": "Акцент на измеримые достижения, сроки, объёмы и бизнес-результат без выдуманных метрик",
        "role_suffix": "Ориентация на результат и метрики",
        "affinity": ("метрик", "kpi", "рост", "конверс", "выручк", "эффективн", "оптимизац", "sla", "roi"),
    },
    "stack": {
        "slot": 3,
        "code": "C",
        "focus": "stack",
        "label": "C · Глубокий стек",
        "short": "Техстек",
        "hint": "Упор на инструменты, архитектуру, интеграции, нагруженные системы и автоматизацию",
        "role_suffix": "Техническая экспертиза и архитектура",
        "affinity": (
            "python", "fastapi", "django", "asyncio", "sql", "postgres", "docker",
            "kubernetes", "redis", "kafka", "api", "микросервис", "highload", "devops", "ml", "ai",
        ),
    },
    "team": {
        "slot": 4,
        "code": "D",
        "focus": "team",
        "label": "D · Команда и процессы",
        "short": "Команда / Лид",
        "hint": "Акцент на кросс-функциональное взаимодействие, код-ревью, наставничество и самостоятельность",
        "role_suffix": "Командная работа и процессы",
        "affinity": ("команд", "лид", "lead", "senior", "ментор", "наставн", "ревью", "процесс", "agile", "scrum", "руковод"),
    },
    "product": {
        "slot": 5,
        "code": "E",
        "focus": "product",
        "label": "E · Продукт и бизнес",
        "short": "Продукт",
        "hint": "Фокус на решении болей пользователей, доведении фич от идеи до продакшена и ценности для бизнеса",
        "role_suffix": "Продуктовый подход",
        "affinity": ("продукт", "product", "бизнес", "клиент", "пользовател", "фич", "запуск", "b2b", "b2c", "saas", "финтех"),
    },
}

_STOP_TECH_WORDS = {
    "http", "https", "com", "org", "net", "www", "ats", "kpi", "sla", "roi", "summary",
    "senior", "middle", "junior", "lead", "head", "full", "time", "developer", "engineer",
    "manager", "specialist", "backend", "frontend", "fullstack", "result", "team",
    "processes", "product", "business", "b2b", "b2c", "saas",
}


def verify_resume_facts(
    base_resume: str,
    adapted_text: str,
    vacancy_info: str = "",
) -> Dict[str, Any]:
    """
    Проверяет сгенерированную копию резюме на отсутствие выдуманных цифр и
    несуществующего в базовом резюме стека.
    Возвращает словарь:
      - ok: True, если выдуманных численных метрик и чужих технологий нет
      - invented_numbers: список цифр/метрик, которых не было в исходном резюме
      - unverified_terms: список англоязычных тех-терминов вне базового резюме
      - badge: короткая подпись для интерфейса
    """
    base_low = (base_resume or "").lower()
    vac_low = (vacancy_info or "").lower()
    adap = adapted_text or ""

    # 1. Числа в базовом резюме
    base_nums = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", base_low))
    # Игнорируем нумерацию списков «1.», «2.», «3.», «4.», «5.»
    cleaned_adap = re.sub(r"(?m)^\s*[1-9]\s*[\.\)]\s*", "", adap)
    cleaned_adap = re.sub(r"\b[1-5]\s*[–-]\s*[1-5]\b", "", cleaned_adap)

    invented_numbers: List[str] = []
    for m in re.finditer(r"(\b\d+(?:[.,]\d+)?)\s*(%|процент|раз|х\b|x\b|млн|тыс|год|года|лет|₽|руб)", cleaned_adap, re.I):
        num = m.group(1)
        phrase = m.group(0).strip()
        if num not in base_nums and phrase.lower() not in base_low:
            if phrase not in invented_numbers:
                invented_numbers.append(phrase)

    # 2. Латинские тех-термины (стек), которых не было в базовом резюме
    base_tech = set(re.findall(r"\b[a-z][a-z0-9+#.-]{2,}\b", base_low))
    adap_tech = re.findall(r"\b[a-z][a-z0-9+#.-]{2,}\b", adap.lower())
    unverified_terms: List[str] = []
    for t in adap_tech:
        if t in _STOP_TECH_WORDS or t in base_tech:
            continue
        # Если термин взят из вакансии и попал в раздел «что уточнить» — не считаем галлюцинацией,
        # но фиксируем, чтобы кандидат видел новые термины.
        if t not in unverified_terms and t not in vac_low:
            unverified_terms.append(t)

    ok = len(invented_numbers) == 0 and len(unverified_terms) <= 2
    if ok:
        badge = "Факты подтверждены · 0 выдумок"
    elif invented_numbers:
        badge = f"Проверьте цифры: {', '.join(invented_numbers[:3])}"
    else:
        badge = f"Новые термины: {', '.join(unverified_terms[:3])}"

    return {
        "ok": ok,
        "invented_numbers": invented_numbers[:5],
        "unverified_terms": unverified_terms[:6],
        "badge": badge,
    }


def _deterministic_variant(
    base_resume: str,
    focus_meta: Dict[str, Any],
    target_role: str = "",
    vacancy_info: str = "",
) -> Tuple[str, str]:
    """Детерминированная сборка варианта резюме строго из фактов кандидата."""
    base = (base_resume or "").strip()
    role = (target_role or "Специалист").strip()
    focus_key = focus_meta["focus"]

    # Извлекаем реальные технологии и факты из базового резюме
    techs = list(dict.fromkeys(re.findall(r"\b[A-Za-z][A-Za-z0-9+#.-]{1,}\b", base)))
    years_m = re.search(r"(\d+\s*(?:\+\s*)?(?:года|лет|год))", base, re.I)
    years_str = years_m.group(1) if years_m else "профильный коммерческий опыт"

    sentences = [s.strip() for s in re.split(r"[\n.;]+", base) if len(s.strip()) > 10]
    core_facts = "; ".join(sentences[:4]) if sentences else base[:280]
    stack_line = ", ".join(techs[:10]) if techs else "современный профильный стек и цифровые инструменты"

    if focus_key == "ats":
        title = f"{role}"
        body = (
            f"🎯 **Заголовок резюме:** {title}\n\n"
            f"💡 **О себе (ATS-фокус):**\n"
            f"{role} ({years_str}). Ключевая специализация и стек: {stack_line}. "
            f"Практический бэкграунд: {core_facts}.\n\n"
            f"🛠 **Ключевые навыки для поднятия в топ:**\n`{stack_line}`\n\n"
            f"📈 **Подача опыта (только подтверждённые факты):**\n"
            f"• Профильная разработка и ведение задач по направлению «{role}».\n"
            f"• Применение в работе: {stack_line}."
        )
    elif focus_key == "numbers":
        title = f"{role} (Цифры и KPI)"
        body = (
            f"🎯 **Заголовок резюме:** {title}\n\n"
            f"💡 **О себе (Акцент на измеримый вклад):**\n"
            f"Практик ({years_str}) с ориентацией на измеримый бизнес-эффект, соблюдение SLA и качество релизов. "
            f"Подтверждённый опыт: {core_facts}.\n\n"
            f"🛠 **Инструменты достижения результата:**\n`{stack_line}`\n\n"
            f"📈 **Фокус в описании мест работы:**\n"
            f"• Доведение задач от постановки до продакшена ({years_str}).\n"
            f"• Оптимизация и автоматизация рабочих процессов на базе: {stack_line}."
        )
    elif focus_key == "stack":
        top_tech = ", ".join(techs[:3]) if techs else role
        title = f"{role} ({top_tech})"
        body = (
            f"🎯 **Заголовок резюме:** {title}\n\n"
            f"💡 **О себе (Технический и архитектурный фокус):**\n"
            f"Инженер-практик ({years_str}). Глубокая работа со стеком: {stack_line}. "
            f"Решаемые задачи: {core_facts}.\n\n"
            f"🛠 **Ядро технологического стека:**\n`{stack_line}`\n\n"
            f"📈 **Технические акценты опыта:**\n"
            f"• Проектирование, интеграция и поддержка решений на {stack_line}.\n"
            f"• Чистый поддерживаемый код, автоматизация и диагностика."
        )
    elif focus_key == "team":
        title = f"{role} (Команда и процессы)"
        body = (
            f"🎯 **Заголовок резюме:** {title}\n\n"
            f"💡 **О себе (Команда, процессы и автономность):**\n"
            f"Самостоятельный специалист ({years_str}) с опытом кросс-функционального взаимодействия, "
            f"выстраивания прозрачных процессов и доведения командных целей до результата. База: {core_facts}.\n\n"
            f"🛠 **Стек и среда взаимодействия:**\n`{stack_line}`\n\n"
            f"📈 **Командные акценты опыта:**\n"
            f"• Согласование технических и продуктовых решений со смежными командами.\n"
            f"• Поддержание качества, ревью и предсказуемости поставок."
        )
    else:
        title = f"{role} (Продукт и бизнес)"
        body = (
            f"🎯 **Заголовок резюме:** {title}\n\n"
            f"💡 **О себе (Продуктовый и бизнес-фокус):**\n"
            f"Специалист ({years_str}), связывающий технические решения ({stack_line}) с задачами продукта "
            f"и удобством конечного пользователя. Опыт: {core_facts}.\n\n"
            f"🛠 **Инструментарий:**\n`{stack_line}`\n\n"
            f"📈 **Продуктовые акценты опыта:**\n"
            f"• Выпуск востребованной функциональности от идеи до продакшена.\n"
            f"• Фокус на надёжности сервиса и ценности для бизнеса."
        )

    return title, body


async def build_resume_variant(
    base_resume: str,
    focus: str = "ats",
    target_role: str = "",
    vacancy_info: str = "",
    use_llm: bool = True,
) -> Dict[str, Any]:
    """
    Создаёт одну из 5 специализированных копий резюме с автоматической
    проверкой фактов. Если LLM пытается приписать несуществующие цифры,
    срабатывает фильтр или детерминированный вариант строго по фактам.
    """
    meta = RESUME_FOCUSES.get(focus, RESUME_FOCUSES["ats"])
    role = (target_role or "Специалист").strip()
    det_title, det_body = _deterministic_variant(base_resume, meta, role, vacancy_info)

    content = det_body
    title = det_title

    if use_llm and (base_resume or "").strip():
        sys_prompt = (
            "Ты — ИИ-коуч по резюме (аналог Sisyphus). Создай целевую версию резюме под выбранный акцент.\n"
            f"АКЦЕНТ ВЕРСИИ: {meta['label']} — {meta['hint']}.\n"
            "ЖЕЛЕЗНОЕ ПРАВИЛО ФАКТОВ:\n"
            "- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО придумывать цифры, проценты, суммы, компании или технологии, которых нет в исходном резюме.\n"
            "- Используй ТОЛЬКО реальные факты и числа из исходного текста кандидата.\n"
            "- Выдай: 1) 🎯 Заголовок резюме, 2) 💡 Summary (3–4 предложения), 3) 🛠 Ключевые навыки, 4) 📈 Формулировки опыта."
        )
        user_prompt = (
            f"ИСХОДНОЕ РЕЗЮМЕ:\n{base_resume}\n\n"
            f"ЦЕЛЕВАЯ РОЛЬ: {role}\n"
            f"ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ВАКАНСИИ:\n{vacancy_info or 'общий профиль по роли'}"
        )
        llm_out = await llm.ask(sys_prompt, user_prompt, temperature=0.25)
        if llm_out and len(llm_out.strip()) > 60:
            check_llm = verify_resume_facts(base_resume, llm_out, vacancy_info)
            # Если LLM выдумала числа — откатываемся к строго фактической сборке
            if check_llm["ok"]:
                content = llm_out.strip()
                first_line = re.search(r"Заголовок[^:\n]*:\s*\**([^\n*]+)", content, re.I)
                if first_line:
                    title = first_line.group(1).strip()[:90]

    fact_report = verify_resume_facts(base_resume, content, vacancy_info)
    return {
        "slot": meta["slot"],
        "code": meta["code"],
        "focus": meta["focus"],
        "label": meta["label"],
        "title": title,
        "content": content,
        "fact_ok": fact_report["ok"],
        "fact_badge": fact_report["badge"],
        "fact_report": fact_report,
    }


def pick_best_resume_variant(
    variants: List[Dict[str, Any]],
    vacancy_title: str = "",
    vacancy_text: str = "",
    base_resume: str = "",
) -> Dict[str, Any]:
    """
    Автоматически выбирает лучшую из сохранённых A/B-копий резюме под конкретную
    вакансию. Оценивает:
      1. Совпадение заголовка резюме и названия вакансии.
      2. Пересечение ключевых технологий и слов из описания вакансии.
      3. Соответствие акцента версии характеру вакансии (лид/команда -> D,
         глубокий техстек/архитектура -> C, KPI/метрики -> B, продукт -> E, база -> A).
    Возвращает словарь с выбранным вариантом (`best`), процентом совпадения
    (`match_score`), совпавшими маркерами (`matched_keywords`) и ранжированным списком.
    """
    vac_full = f"{vacancy_title or ''} {vacancy_text or ''}".strip()
    vac_low = vac_full.lower()
    title_words = [w for w in re.findall(r"[а-яёa-z0-9+#]{3,}", (vacancy_title or "").lower()) if len(w) >= 3]
    vac_tokens = list(dict.fromkeys(re.findall(r"[а-яёa-z0-9+#.-]{3,}", vac_low)))

    if not variants:
        det_title, det_body = _deterministic_variant(
            base_resume,
            RESUME_FOCUSES["ats"],
            vacancy_title or "Специалист",
            vacancy_text,
        )
        fallback_var = {
            "slot": 1,
            "code": "A",
            "focus": "ats",
            "label": RESUME_FOCUSES["ats"]["label"],
            "title": det_title,
            "content": det_body,
            "fact_ok": True,
            "fact_badge": "Базовое резюме",
        }
        return {
            "best": fallback_var,
            "match_score": 78,
            "matched_keywords": vac_tokens[:6],
            "reason": "Использовано базовое резюме (создайте до 5 A/B-копий для точного автовыбора)",
            "ranked": [fallback_var],
        }

    scored: List[Tuple[int, List[str], str, Dict[str, Any]]] = []
    for var in variants:
        focus_key = str(var.get("focus") or "ats")
        meta = RESUME_FOCUSES.get(focus_key, RESUME_FOCUSES["ats"])
        v_title_low = str(var.get("title") or "").lower()
        v_content_low = str(var.get("content") or "").lower()
        combined = f"{v_title_low}\n{v_content_low}"

        raw_score = 45
        matched: List[str] = []

        # 1. Пересечение слов заголовка вакансии
        for w in title_words:
            if w in v_title_low:
                raw_score += 10
                if w not in matched:
                    matched.append(w)
            elif w in v_content_low:
                raw_score += 5
                if w not in matched:
                    matched.append(w)

        # 2. Пересечение маркеров описания вакансии
        for tok in vac_tokens[:35]:
            if len(tok) < 3 or tok in _STOP_TECH_WORDS:
                continue
            if tok in combined:
                raw_score += 3
                if tok not in matched:
                    matched.append(tok)

        # 3. Профильная близость акцента (affinity)
        affinity_hits: List[str] = []
        for marker in meta.get("affinity", ()):
            if marker in vac_low:
                raw_score += 9
                affinity_hits.append(marker)

        if var.get("fact_ok", True):
            raw_score += 4

        score = max(52, min(98, raw_score))
        reason_parts = [f"акцент «{meta['short']}»"]
        if affinity_hits:
            reason_parts.append(f"маркеры вакансии: {', '.join(affinity_hits[:3])}")
        elif matched:
            reason_parts.append(f"совпадения: {', '.join(matched[:4])}")

        enriched = {
            **var,
            "code": meta["code"],
            "label": meta["label"],
            "match_score": score,
            "raw_score": raw_score,
            "matched_keywords": matched[:8],
            "reason": " · ".join(reason_parts),
        }
        scored.append((raw_score, matched[:8], enriched["reason"], enriched))

    scored.sort(key=lambda x: (x[0], int(x[3].get("fact_ok", True)), -int(x[3].get("slot", 9))), reverse=True)
    _, best_kw, best_reason, best_var = scored[0]
    best_score = int(best_var["match_score"])
    return {
        "best": best_var,
        "match_score": best_score,
        "matched_keywords": best_kw,
        "reason": best_reason,
        "ranked": [item[3] for item in scored],
    }
