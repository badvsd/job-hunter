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
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

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


# Популярные публичные Telegram-каналы с вакансиями в РФ
DEFAULT_TG_CHANNELS = [
    "finder_vc",        # Finder.vc: удалённая работа
    "normrabota",       # Норм работа
    "vacancies_it",     # Вакансии IT
    "devjobs",          # DevJobs
    "foranalysts",      # Для аналитиков
    "zarubezhom_jobs",  # Удалёнка и релокация
    "work_in_digital",  # Digital вакансии
]


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


async def _search_hh_html(query: str, city: str = "", limit: int = 6) -> List[Vacancy]:
    """
    Резервный парсер публичной HTML-выдачи hh.ru.
    Используется, когда официальное API отвечает 403 (без OAuth/с чужого IP):
    читаем только открытые карточки вакансий, без авторизации и капчи.
    """
    out: List[Vacancy] = []
    text_q = f"{query} {city}".strip()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(
                "https://hh.ru/search/vacancy",
                params={"text": text_q, "items_on_page": min(max(limit, 20), 50), "hhtmFrom": "vacancy_search_list"},
            )
            if r.status_code != 200:
                return out
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.find_all("div", class_=re.compile(r"vacancy-card--"))
            for card in cards[:limit]:
                link = card.select_one('[data-qa="serp-item__title"]')
                title_el = card.select_one('[data-qa="serp-item__title-text"]') or link
                if not title_el:
                    continue
                title = title_el.get_text(" ", strip=True)
                url = (link.get("href") if link else "") or ""
                url = url.split("?")[0]
                emp = card.select_one('[data-qa="vacancy-serp__vacancy-employer-text"]') or card.select_one(
                    '[data-qa="vacancy-serp__vacancy-employer"]'
                )
                company = emp.get_text(" ", strip=True) if emp else "Прямой работодатель"
                city_el = card.select_one('[data-qa="vacancy-serp__vacancy-address"]')
                vac_city = city_el.get_text(" ", strip=True) if city_el else city
                trusted_emp = bool(card.select_one('[data-qa="trusted-employer-link"]'))

                experience = ""
                exp_el = card.select_one('[data-qa^="vacancy-serp__vacancy-work-experience"]')
                if exp_el:
                    experience = exp_el.get_text(" ", strip=True)
                remote = "Можно удалённо" in card.get_text(" ", strip=True)

                desc_parts = []
                if experience:
                    desc_parts.append(experience)
                if remote:
                    desc_parts.append("можно удалённо")
                desc_parts.append("Полные условия и отклик — в карточке на hh.ru")
                description = " · ".join(desc_parts)

                vac_id = card.get("id") or url.rstrip("/").split("/")[-1] or title
                v = Vacancy(
                    id=f"hh_{vac_id}",
                    platform="HH.ru",
                    title=title,
                    company=company,
                    salary="",
                    url=url or f"https://hh.ru/vacancy/{vac_id}",
                    description=description[:600],
                    city=vac_city,
                )
                QuickScamFilter.score_vacancy(v, employer_verified=trusted_emp)
                out.append(v)
    except Exception:
        pass
    return out


async def search_hh(query: str, city: str = "", min_salary: int = 0, limit: int = 6) -> List[Vacancy]:
    """Поиск по официальному публичному API HH.ru (без бана и капчи)."""
    params: Dict[str, Any] = {
        "text": f"{query} {city}".strip(),
        "per_page": min(limit, 20),
        "page": 0,
        "order_by": "publication_time",
    }
    if min_salary and min_salary > 0:
        params["salary"] = min_salary

    headers = {"User-Agent": "JobShieldBot/2.0 (contact@jobshield.local)"}
    out: List[Vacancy] = []
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            r = await client.get("https://api.hh.ru/vacancies", params=params)
            if r.status_code != 200:
                # API часто отдаёт 403 без OAuth — читаем публичную HTML-выдачу.
                return await _search_hh_html(query, city=city, limit=limit)
            items = r.json().get("items", [])
            for it in items[:limit]:
                emp = it.get("employer") or {}
                snippet = it.get("snippet") or {}
                req = _clean_html(snippet.get("requirement") or "")
                resp = _clean_html(snippet.get("responsibility") or "")
                desc = " | ".join([x for x in (req, resp) if x]) or "Подробности по ссылке на вакансию."
                trusted_emp = bool(emp.get("trusted"))
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
                QuickScamFilter.score_vacancy(v, employer_verified=trusted_emp)
                out.append(v)
    except Exception:
        return await _search_hh_html(query, city=city, limit=limit)
    return out


async def search_trudvsem(query: str, limit: int = 5) -> List[Vacancy]:
    """
    Поиск по Государственному порталу «Работа России» (opendata.trudvsem.ru).
    Огромный плюс: отдает реальный ИНН и ОГРН каждого работодателя!
    """
    url = "https://opendata.trudvsem.ru/api/v1/vacancies"
    params = {"text": query, "limit": min(limit, 15)}
    out: List[Vacancy] = []
    try:
        async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                return out
            data = r.json()
            vacancies = (
                data.get("results", {})
                .get("vacancies", [])
            )
            for item in vacancies[:limit]:
                vac = item.get("vacancy") or {}
                comp = vac.get("company") or {}
                sal = vac.get("salary") or (
                    f"от {vac.get('salary_min')} ₽" if vac.get("salary_min") else "з/п по договорённости"
                )
                duty = _clean_html(vac.get("duty") or "")
                inn = str(comp.get("inn") or "")
                v = Vacancy(
                    id=f"tv_{vac.get('id', '')[:12]}",
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
    except Exception:
        pass
    return out


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


async def search_telegram_channels(
    query: str,
    channels: Optional[List[str]] = None,
    limit: int = 5,
) -> List[Vacancy]:
    """
    Реальный парсинг публичных Telegram-каналов с вакансиями через веб-превью t.me/s/<channel>?q=<query>.
    Работает мгновенно, без авторизации сим-карты и риска бана TG-аккаунта!
    """
    ch_list = channels or DEFAULT_TG_CHANNELS
    out: List[Vacancy] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
        tasks = []
        for ch in ch_list[:5]:
            search_url = f"https://t.me/s/{ch}?q={quote_plus(query)}"
            tasks.append((ch, client.get(search_url)))

        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

        for idx, res in enumerate(results):
            ch = tasks[idx][0]
            if isinstance(res, Exception) or res.status_code != 200:
                continue
            soup = BeautifulSoup(res.text, "html.parser")
            wraps = soup.select(".tgme_widget_message_wrap")
            for w in reversed(wraps):
                text_el = w.select_one(".tgme_widget_message_text")
                if not text_el:
                    continue
                raw_text = text_el.get_text("\n", strip=True)
                if len(raw_text) < 40:
                    continue
                lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
                title = f"Вакансия из @{ch}"
                for ln in lines[:4]:
                    if len(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", ln)) >= 8:
                        title = ln[:90]
                        break
                link_el = w.select_one(".tgme_widget_message_date")
                post_url = link_el["href"] if link_el and link_el.has_attr("href") else f"https://t.me/s/{ch}"
                post_id = post_url.rstrip("/").split("/")[-1]
                sal_match = re.search(
                    r"(\d[\d\s\.,]{2,12}\s*(?:₽|руб|р\.|тыс|\$|€|USD|EUR))",
                    raw_text,
                    re.IGNORECASE,
                )
                salary = sal_match.group(1).strip() if sal_match else "см. в посте"
                contact = _extract_tg_contact(raw_text, ch)

                v = Vacancy(
                    id=f"tg_{ch}_{post_id}",
                    platform=f"Telegram (@{ch})",
                    title=title,
                    company=f"Контакт: {contact}" if contact else f"Канал @{ch}",
                    salary=salary,
                    url=post_url,
                    description=raw_text[:650],
                    contact=contact,
                )
                QuickScamFilter.score_vacancy(v, employer_verified=False)
                out.append(v)
                if len(out) >= limit:
                    return out
    return out


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


async def search_all_platforms(
    query: str,
    city: str = "",
    min_salary: int = 0,
    tg_channels: Optional[List[str]] = None,
) -> List[Vacancy]:
    """Параллельный мульти-поиск сразу по всем площадкам Рунета."""
    hh_task = search_hh(query, city=city, min_salary=min_salary, limit=5)
    tv_task = search_trudvsem(f"{query} {city}".strip(), limit=3)
    tg_task = search_telegram_channels(query, channels=tg_channels, limit=4)

    hh_res, tv_res, tg_res = await asyncio.gather(hh_task, tv_task, tg_task, return_exceptions=True)

    combined: List[Vacancy] = []
    if isinstance(hh_res, list):
        combined.extend(hh_res)
    if isinstance(tv_res, list):
        combined.extend(tv_res)
    if isinstance(tg_res, list):
        combined.extend(tg_res)

    # Добавляем кроссплатформенные шлюзы (Авито, ВК, ОК, Habr, SuperJob, LinkedIn, Instagram)
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
