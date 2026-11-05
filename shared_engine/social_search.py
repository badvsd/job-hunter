# -*- coding: utf-8 -*-
"""
Социальный поиск вакансий (Social Job Search) — сервис поиска найма в соцсетях
и комьюнити-платформах для JobShield / Multi-Platform Job Hunter.

Покрытие (17 сетей):
  ЖИВЫЕ ПАРСЕРЫ (реальные публичные API/ленты, без авторизации и без обхода защит):
   - Telegram        — t.me/s/<channel>?q= (веб-превью каналов)
   - Reddit          — публичный RSS-поток reddit.com/search.rss (JSON-поиск отдаёт 403 на датацентровых IP)
   - Bluesky         — XRPC app.bsky.feed.searchPosts: без токена шлюз, с BSKY_TOKEN / BSKY_HANDLE+BSKY_APP_PASSWORD — живой поиск
   - Mastodon        — публичные тайм-лайны хэштегов #hiring / #jobs (абстрактный поиск без токена пуст)
   - Hacker News     — Algolia API + тред «Ask HN: Who is hiring?» текущего месяца (точный запрос по дате)
   - RemoteOK        — публичный JSON remoteok.com/api
   - Discord         — каталог серверов disforge.com: категория jobs + тематические по запросу (Disboard закрыт Cloudflare)

  УМНЫЕ DEEP-LINK ШЛЮЗЫ (закрытые антифрод-сети, X-Ray и прямые фильтры):
   - LinkedIn        — jobs search по ключевым словам
   - Facebook        — вакансии + X-Ray по группам
   - Instagram*      — X-Ray по хэштегам найма (Meta признана экстремистской в РФ)
   - X (Twitter)     — живой поиск «hiring» по свежим постам
   - ВКонтакте / ОК  — посты рекрутеров и паблики с работой
   - WeWorkRemotely  — удалёнка global
   - TGStat          — каталог Telegram-каналов с работой
   - Dribbble / Behance — дизайн-вакансии и заказы

Принципы: только публичные источники, никакого обхода авторизации,
каждая живая находка прогоняется через антифрод QuickScamFilter.
"""

from __future__ import annotations

import asyncio
import calendar
import html as html_lib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from .engine import DEFAULT_TG_CHANNELS, QuickScamFilter, Vacancy, search_telegram_channels

SOCIAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────────────
# КАТАЛОГ СОЦСЕТЕЙ И КОМЬЮНИТИ-ПЛАТФОРМ
# ─────────────────────────────────────────────────────────────────────────────

SOCIAL_NETWORKS: Dict[str, Dict[str, Any]] = {
    "telegram": {
        "key": "telegram", "name": "Telegram-каналы", "emoji": "✈️", "kind": "live",
        "region": "ru", "trust": 75, "badge": "🟢 живой парсинг t.me/s",
        "how": "Парсинг публичных веб-лент каналов с вакансиями — без авторизации и риска бана.",
    },
    "reddit": {
        "key": "reddit", "name": "Reddit", "emoji": "👽", "kind": "live",
        "lang": "global", "trust": 70, "badge": "🟢 публичный RSS-поток",
        "how": "Свежие посты о найме из r/forhire, r/jobbit и профильных сабреддитов (Atom-лента).",
    },
    "discord": {
        "key": "discord", "name": "Discord-серверы", "emoji": "🎮", "kind": "live",
        "lang": "global", "trust": 62, "badge": "🟢 каталог Disforge",
        "how": "Комьюнити-серверы с каналами #vacancies и #jobs — вакансии появляются там раньше джоб-бордов.",
    },
    "bluesky": {
        "key": "bluesky", "name": "Bluesky", "emoji": "🦋", "kind": "live",
        "lang": "global", "trust": 70, "badge": "🟢 XRPC + токен BSKY",
        "how": "Живой поиск постов «hiring» (AT Protocol). Работает с BSKY_TOKEN или BSKY_HANDLE + BSKY_APP_PASSWORD; без токена — шлюз.",
    },
    "mastodon": {
        "key": "mastodon", "name": "Mastodon", "emoji": "🐘", "kind": "live",
        "lang": "global", "trust": 70, "badge": "🟢 публичный API",
        "how": "Посты о найме из публичных тайм-лайнов #hiring / #jobs / #вакансии федиверса.",
    },
    "hn": {
        "key": "hn", "name": "Hacker News", "emoji": "🟠", "kind": "live",
        "lang": "global", "trust": 88, "badge": "🟢 тред «Who is hiring?»",
        "how": "Ежемесячный тред «Ask HN: Who is hiring?» — сотни прямых вакансий без рекрутеров.",
    },
    "remoteok": {
        "key": "remoteok", "name": "RemoteOK", "emoji": "🌍", "kind": "live",
        "lang": "global", "trust": 85, "badge": "🟢 публичный JSON API",
        "how": "Крупнейший агрегатор удалённых вакансий с реальными вилками и тегами стека.",
    },
    "linkedin": {
        "key": "linkedin", "name": "LinkedIn", "emoji": "💼", "kind": "link",
        "lang": "global", "trust": 82, "badge": "🔗 deep-link + рекрутеры",
        "how": "Прямой фильтр вакансий по ключевым словам и географии.",
    },
    "facebook": {
        "key": "facebook", "name": "Facebook", "emoji": "📘", "kind": "link",
        "lang": "global", "trust": 60, "badge": "🔗 X-Ray по группам",
        "how": "Поиск вакансий и X-Ray по группам найма (Meta признана экстремистской в РФ).",
    },
    "instagram": {
        "key": "instagram", "name": "Instagram* / Threads", "emoji": "📸", "kind": "link",
        "lang": "ru+global", "trust": 60, "badge": "🔗 X-Ray хэштеги найма",
        "how": "X-Ray запрос по хэштегам #ищемвкоманду / #hiring — найм напрямую в Директ.",
    },
    "x": {
        "key": "x", "name": "X (Twitter)", "emoji": "𝕏", "kind": "link",
        "lang": "global", "trust": 62, "badge": "🔗 живой поиск постов",
        "how": "Лента свежих постов о найме: фильтр «hiring» по свежим твитам.",
    },
    "vk": {
        "key": "vk", "name": "ВКонтакте", "emoji": "🟦", "kind": "link",
        "lang": "ru", "trust": 65, "badge": "🔗 посты и паблики",
        "how": "Поиск по свежим постам рекрутеров и пабликам с вакансиями ВКонтакте.",
    },
    "ok": {
        "key": "ok", "name": "Одноклассники", "emoji": "🟠", "kind": "link",
        "lang": "ru", "trust": 65, "badge": "🔗 региональные группы",
        "how": "Группы прямых работодателей и региональные сообщества с работой в ОК.",
    },
    "weworkremotely": {
        "key": "weworkremotely", "name": "We Work Remotely", "emoji": "🌍", "kind": "link",
        "lang": "global", "trust": 85, "badge": "🔗 база удалёнки",
        "how": "Крупнейшая доска полностью удалённых вакансий (prog, support, design).",
    },
    "tgstat": {
        "key": "tgstat", "name": "TGStat (каталог TG)", "emoji": "🧭", "kind": "link",
        "lang": "ru", "trust": 70, "badge": "🔍 поиск каналов",
        "how": "Ищем ещё не охваченные каналы с вакансиями и расширяем подписки.",
    },
    "dribbble": {
        "key": "dribbble", "name": "Dribbble Jobs", "emoji": "🏀", "kind": "link",
        "lang": "global", "trust": 80, "badge": "🎨 дизайн",
        "how": "Дизайн-вакансии и проекты: UI/UX, брендинг, иллюстрация.",
    },
    "behance": {
        "key": "behance", "name": "Behance Jobs", "emoji": "🎨", "kind": "link",
        "lang": "global", "trust": 80, "badge": "🎨 креатив",
        "how": "Вакансии и заказы для креативщиков: дизайн, моушн, арт-дирекшн.",
    },
}

# Маркеры «пост — это найм», для фильтрации живых лент
_HIRE_RE = re.compile(
    r"(hiring|vacanc\w*|job opening|we(?:'re| are) hiring|join (?:our|the) team|"
    r"ваканси\w*|ищем|нанима\w*|в команду|требуетс\w*|открыт\w+ позици\w*|резюме|стажировк\w*)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _query_tokens(query: str) -> List[str]:
    return [w for w in re.findall(r"[a-zа-яё0-9+#]{4,}", (query or "").lower())][:6]


def _is_hiring_post(text: str, query: str) -> bool:
    """Пост релевантен найму: маркер найма + ≥1 токен запроса, либо ≥2 токена запроса."""
    low = (text or "").lower()
    tokens = _query_tokens(query)
    token_hits = sum(1 for t in tokens if t in low)
    return (bool(_HIRE_RE.search(low)) and token_hits >= 1) or token_hits >= 2


def _vac_to_dict(v: Vacancy, net: str) -> Dict[str, Any]:
    d = asdict(v)
    d["net"] = net
    return d


# ─────────────────────────────────────────────────────────────────────────────
# ЖИВЫЕ ПАРСЕРЫ (публичные API, без авторизации)
# ─────────────────────────────────────────────────────────────────────────────

async def search_reddit(query: str, limit: int = 4) -> List[Vacancy]:
    """Публичный Atom-поток Reddit (search.rss): JSON-поиск отдаёт 403 с датацентровых IP."""
    out: List[Vacancy] = []
    url = f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=new&limit=50"
    async with httpx.AsyncClient(timeout=12.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        res = await client.get(url)
        # Reddit троттлит частые запросы (429): одна бережная повторная попытка
        if res.status_code == 429 and out == []:
            retry_after = float(res.headers.get("Retry-After") or 0)
            await asyncio.sleep(min(max(retry_after, 1.2), 4.0))
            res = await client.get(url)
        if res.status_code != 200:
            return out
    try:
        root = ET.fromstring(res.content)
    except ET.ParseError:
        return out

    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        raw_title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        body = _strip_html(entry.findtext("a:content", default="", namespaces=ns) or "")
        link = ""
        lnk = entry.find("a:link", ns)
        if lnk is not None:
            link = str(lnk.get("href") or "")
        author = (entry.findtext("a:author/a:name", default="", namespaces=ns) or "unknown").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
        text = f"{raw_title} {body}"
        if not link or len(text) < 25 or not _is_hiring_post(text, query):
            continue
        sub_m = re.search(r"reddit\.com/r/([^/\s]+)/", link)
        subreddit = sub_m.group(1) if sub_m else "forhire"
        entry_id = (entry.findtext("a:id", default="", namespaces=ns) or "").replace("t3_", "") or str(abs(hash(link)))
        published = updated
        if updated:
            try:
                published = time.strftime("%d.%m.%Y", time.strptime(updated, "%Y-%m-%d"))
            except ValueError:
                pass
        v = Vacancy(
            id=f"reddit_{entry_id}",
            platform=f"Reddit · r/{subreddit}",
            title=_strip_html(raw_title)[:110] or "Пост о найме на Reddit",
            company=f"u/{author}",
            salary="см. в посте",
            url=link,
            description=body[:500],
            contact=f"u/{author}",
            published_at=published,
        )
        QuickScamFilter.score_vacancy(v, employer_verified=False)
        out.append(v)
        if len(out) >= limit:
            break
    return out


async def search_bluesky(query: str, limit: int = 3) -> List[Vacancy]:
    """
    Поиск постов Bluesky (app.bsky.feed.searchPosts). С конца 2024 XRPC-поиск
    требует авторизацию: задаётся готовый access-токен (BSKY_TOKEN) либо
    связка BSKY_HANDLE + BSKY_APP_PASSWORD (App Password, сессия кэшируется).
    Без токена парсер тихо отдаёт 0 — оркестратор подставит fallback-шлюз.
    """
    out: List[Vacancy] = []
    async with httpx.AsyncClient(timeout=15.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        access = await _bsky_access_token(client)
        if not access:
            return out
        # bsky.social доступнее с датацентровых/домашних IP и проксирует тот же AppView;
        # public.api.bsky.app — каноничный хост, оставлен как фолбэк.
        auth_headers = {"Authorization": f"Bearer {access}"}
        res = None
        for host in ("https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                     "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"):
            try:
                res = await client.get(host, params={"q": query, "limit": 20, "sort": "latest"}, headers=auth_headers)
                break
            except httpx.HTTPError:
                continue
        if res is None or res.status_code != 200:
            return out
        for p in res.json().get("posts") or []:
            rec = p.get("record") or {}
            text = str(rec.get("text") or "")
            if len(text) < 25 or not _is_hiring_post(text, query):
                continue
            author = (p.get("author") or {}).get("handle") or "bsky.user"
            rkey = str(p.get("uri") or "").rsplit("/", 1)[-1]
            v = Vacancy(
                id=f"bsky_{rkey or abs(hash(text))}",
                platform="Bluesky",
                title=_strip_html(text)[:110] or "Пост о найме в Bluesky",
                company=f"@{author}",
                salary="см. в посте",
                url=f"https://bsky.app/profile/{author}/post/{rkey}" if rkey else "https://bsky.app/search",
                description=_strip_html(text)[:450],
                contact=f"@{author}",
            )
            QuickScamFilter.score_vacancy(v, employer_verified=False)
            out.append(v)
            if len(out) >= limit:
                break
    return out


# Кэш сессии Bluesky: accessJwt живёт ~2 часа, refreshJwt обновляет его без пароля
_BSKY_SESSION: Dict[str, Any] = {"access": "", "refresh": "", "exp": 0.0}


async def _bsky_access_token(client: httpx.AsyncClient) -> Optional[str]:
    """
    Access-токен Bluesky для XRPC-поиска.
    Приоритет: BSKY_TOKEN (готовый accessJwt) → кэш → refreshJwt → createSession.
    """
    ready = os.environ.get("BSKY_TOKEN")
    if ready:
        return ready
    handle = os.environ.get("BSKY_HANDLE") or os.environ.get("BLUESKY_HANDLE")
    password = os.environ.get("BSKY_APP_PASSWORD") or os.environ.get("BLUESKY_APP_PASSWORD")
    if not handle or not password:
        return None
    now = time.time()
    if _BSKY_SESSION["access"] and _BSKY_SESSION["exp"] > now + 60:
        return str(_BSKY_SESSION["access"])
    # Пробуем обновить истёкший accessJwt через refreshJwt
    if _BSKY_SESSION["refresh"] and _BSKY_SESSION["exp"] > now:
        try:
            res = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {_BSKY_SESSION['refresh']}"},
            )
            if res.status_code == 200:
                j = res.json()
                _BSKY_SESSION.update(access=j.get("accessJwt"), refresh=j.get("refreshJwt"), exp=now + 3600)
                return str(_BSKY_SESSION["access"])
        except httpx.HTTPError:
            pass
    # Полный вход по app-password
    try:
        res = await client.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": password},
        )
        if res.status_code != 200:
            return None
        j = res.json()
        _BSKY_SESSION.update(access=j.get("accessJwt"), refresh=j.get("refreshJwt"), exp=now + 3600)
        return str(_BSKY_SESSION["access"])
    except httpx.HTTPError:
        return None


async def search_mastodon(query: str, limit: int = 3) -> List[Vacancy]:
    """
    Публичные тайм-лайны хэштегов федиверса: #hiring, #jobs (+ #вакансии для русских запросов).
    Абстрактный поиск /api/v2/search без токена возвращает пусто, а тайм-лайны тегов открыты.
    """
    out: List[Vacancy] = []
    tags = ["hiring", "jobs"]
    if any(re.search(r"[а-яё]", t) for t in _query_tokens(query)):
        tags.append("вакансии")
    seen: set = set()
    async with httpx.AsyncClient(timeout=15.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        for tag in tags:
            try:
                res = await client.get(
                    f"https://mastodon.social/api/v1/timelines/tag/{quote_plus(tag)}",
                    params={"limit": 20},
                )
            except httpx.HTTPError:
                continue
            if res.status_code != 200:
                continue
            for s in res.json() or []:
                sid = str(s.get("id") or "")
                if not sid or sid in seen:
                    continue
                text = _strip_html(str(s.get("content") or ""))
                if len(text) < 25 or not _is_hiring_post(text, query):
                    continue
                seen.add(sid)
                acc = s.get("account") or {}
                acct = str(acc.get("acct") or "user")
                v = Vacancy(
                    id=f"mastodon_{sid}",
                    platform="Mastodon (fediverse)",
                    title=text[:110] or "Пост о найме в Mastodon",
                    company=f"@{acct}",
                    salary="см. в посте",
                    url=str(s.get("url") or "https://mastodon.social"),
                    description=text[:450],
                    contact=f"@{acct}",
                    published_at=str(s.get("created_at") or "")[:10],
                )
                QuickScamFilter.score_vacancy(v, employer_verified=False)
                out.append(v)
                if len(out) >= limit:
                    return out
    return out


async def search_hn_hiring(query: str, limit: int = 3) -> List[Vacancy]:
    """Тред «Ask HN: Who is hiring?» текущего месяца через публичный Algolia API."""
    out: List[Vacancy] = []
    tokens = _query_tokens(query)
    async with httpx.AsyncClient(timeout=18.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        story_id = None
        # 1) Точное название треда текущего месяца — надёжный первичный запрос
        now = time.gmtime()
        current_title = f"Ask HN: Who is hiring? ({calendar.month_name[now.tm_mon]} {now.tm_year})"
        story_res = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": current_title, "tags": "story", "hitsPerPage": 5},
        )
        if story_res.status_code == 200:
            for hit in story_res.json().get("hits") or []:
                if str(hit.get("title") or "").strip().lower() == current_title.lower():
                    story_id = hit.get("objectID")
                    break
        # 2) Фолбэк: окно по дате, строгий паттерн месячного треда
        if not story_id:
            story_res = await client.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": "Who is hiring?", "tags": "story,ask_hn", "hitsPerPage": 40},
            )
            if story_res.status_code == 200:
                pat = re.compile(r"who is hiring\? \([a-z]+ \d{4}\)")
                for hit in story_res.json().get("hits") or []:
                    if pat.search(str(hit.get("title") or "").lower()):
                        story_id = hit.get("objectID")
                        break
        if not story_id:
            return out
        items_res = await client.get(f"https://hn.algolia.com/api/v1/items/{story_id}")
        if items_res.status_code != 200:
            return out
        for comment in (items_res.json().get("children") or []):
            text = _strip_html(str(comment.get("text") or ""))
            if len(text) < 60:
                continue
            tokens_hit = sum(1 for t in tokens if t in text.lower())
            if tokens_hit < 1:
                continue
            first_line = text[:110]
            v = Vacancy(
                id=f"hn_{comment.get('id')}",
                platform="Hacker News (Who is hiring)",
                title=first_line,
                company="Прямой работодатель (HN-тред)",
                salary="см. в комментарии",
                url=f"https://news.ycombinator.com/item?id={comment.get('id')}",
                description=text[:550],
                contact="автор комментария на HN",
                published_at=str(comment.get("created_at") or "")[:10],
            )
            QuickScamFilter.score_vacancy(v, employer_verified=False)
            out.append(v)
            if len(out) >= limit:
                break
    return out


async def search_remoteok(query: str, limit: int = 3) -> List[Vacancy]:
    """Публичный JSON RemoteOK — удалённые вакансии с реальными вилками."""
    out: List[Vacancy] = []
    tokens = _query_tokens(query)
    async with httpx.AsyncClient(timeout=15.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        res = await client.get("https://remoteok.com/api")
        if res.status_code != 200:
            return out
        data = res.json()
        if isinstance(data, dict):
            data = data.get("jobs") or []
        for d in data:
            if not isinstance(d, dict) or not d.get("position"):
                continue  # первый элемент — legal notice
            hay = " ".join([
                str(d.get("position") or ""), str(d.get("company") or ""),
                str(d.get("location") or ""), " ".join(d.get("tags") or []),
            ]).lower()
            if tokens and not any(t in hay for t in tokens):
                continue
            sal_min, sal_max = d.get("salary_min"), d.get("salary_max")
            salary = f"${sal_min}–{sal_max}" if sal_min and sal_max else "По вилке вакансии"
            v = Vacancy(
                id=f"remoteok_{d.get('slug') or d.get('id')}",
                platform=f"RemoteOK · {str(d.get('location') or 'Remote')}",
                title=str(d.get("position") or "")[:110],
                company=str(d.get("company") or "RemoteOK"),
                salary=salary,
                url=str(d.get("url") or "https://remoteok.com/remote-jobs"),
                description=_strip_html(str(d.get("description") or ""))[:500],
                contact="через сайт RemoteOK",
                published_at=str(d.get("date") or "")[:10],
            )
            QuickScamFilter.score_vacancy(v, employer_verified=False)
            out.append(v)
            if len(out) >= limit:
                break
    return out


async def search_discord_servers(query: str, limit: int = 4) -> List[Vacancy]:
    """
    Живой поиск Discord-серверов с каналами вакансий через открытый каталог
    disforge.com (без входа в аккаунт Discord). Disboard закрыт Cloudflare,
    встроенный поиск Disforge игнорирует параметр, поэтому используются
    профильные категории: /servers/jobs всегда + до 2 тематических по запросу.
    """
    out: List[Vacancy] = []
    seen: set = set()

    tokens = _query_tokens(query)
    topical: List[str] = []
    for cat, hints in _DISCORD_CATEGORY_HINTS.items():
        if any(any(h in t or t in h for h in hints) for t in tokens):
            topical.append(cat)
            if len(topical) >= 2:
                break
    # Тематические категории первыми (релевантнее), jobs — всегда в хвосте
    categories = topical[:2] + ["jobs"]

    async def fetch_category(client: httpx.AsyncClient, cat: str) -> List[Dict[str, str]]:
        try:
            res = await client.get(f"https://disforge.com/servers/{cat}")
        except httpx.HTTPError:
            return []
        if res.status_code != 200:
            return []
        cards: List[Dict[str, str]] = []
        for art in BeautifulSoup(res.text, "html.parser").select("article"):
            a = art.select_one("a[href*='/server/']")
            if not a:
                continue
            href = a.get("href") or ""
            if not href.startswith("/server/"):
                continue
            name_el = art.select_one("h3")
            desc_el = art.select_one("p")
            cards.append({
                "href": href,
                "name": name_el.get_text(strip=True) if name_el else "",
                "desc": _strip_html(desc_el.get_text(" ", strip=True)) if desc_el else "",
            })
        return cards

    async with httpx.AsyncClient(timeout=12.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        pages = await asyncio.gather(*(fetch_category(client, c) for c in categories))

    # Сначала карточки, релевантные запросу, затем добираем из категории jobs
    ranked: List[Dict[str, str]] = []
    for cards in pages:
        for card in cards:
            if card["href"] not in seen:
                seen.add(card["href"])
                ranked.append(card)
    if tokens and len(ranked) > limit:
        hits = [c for c in ranked if any(t in f"{c['name']} {c['desc']}".lower() for t in tokens)]
        rest = [c for c in ranked if c not in hits]
        ranked = hits + rest

    for card in ranked:
        name = card["name"]
        if not name:
            continue
        v = Vacancy(
            id=f"discord_{card['href'].rstrip('/').split('/')[-1]}",
            platform="Discord (Disforge)",
            title=f"Сервер «{name[:60]}» — каналы вакансий",
            company="Discord-комьюнити",
            salary="см. канал #jobs",
            url="https://disforge.com" + card["href"],
            description=card["desc"][:450] or "Комьюнити-сервер Discord с вакансиями и заказами.",
        )
        QuickScamFilter.score_vacancy(v, employer_verified=False)
        out.append(v)
        if len(out) >= limit:
            break
    return out


# Токены запроса → тематические категории Disforge (к jobs добавляются максимум 2)
_DISCORD_CATEGORY_HINTS: Dict[str, Tuple[str, ...]] = {
    "programming": ("python", "java", "php", "c++", "code", "код", "программ", "разработ", "backend", "frontend", "dev"),
    "technology": ("tech", "айти", "сисадм", "технолог", "engineer", "инженер"),
    "design": ("design", "дизайн", "figma", "ui", "ux", "illustr", "арт"),
    "marketing": ("маркет", "marketing", "seo", "smm", "реклам"),
    "business": ("бизнес", "business", "стартап", "startup", "предприн", "продав", "продаж"),
    "finance": ("финанс", "finance", "бухгалтер", "account", "аудит"),
    "education": ("учител", "преподав", "репетит", "education", "educat", "курс"),
}


# ─────────────────────────────────────────────────────────────────────────────
# DEEP-LINK ШЛЮЗЫ (закрытые сети, X-Ray dorks)
# ─────────────────────────────────────────────────────────────────────────────

def generate_social_hubs(query: str, city: str = "", networks: Optional[List[str]] = None) -> List[Vacancy]:
    """
    Умные поисковые шлюзы (deep links + X-Ray dorks) в закрытые соцсети,
    где живой парсинг невозможен без авторизации.
    """
    q_full = f"{query} {city}".strip()
    q_enc = quote_plus(q_full)
    selected = [n for n in (networks or list(SOCIAL_NETWORKS)) if n in SOCIAL_NETWORKS]
    q_job = quote_plus(f"вакансия {q_full}")
    insta_q = f'(site:instagram.com OR site:threads.net) ("вакансия" OR "ищем в команду") {q_full}'
    fb_q = f'(site:facebook.com) ("hiring" OR "вакансия") {q_full}'
    x_q = f'("we are hiring" OR "hiring" OR "вакансия") ({q_full}) -filter:replies'

    hubs: List[Vacancy] = []
    if "linkedin" in selected:
        hubs.append(Vacancy(
            id="soc_linkedin",
            platform="LinkedIn",
            title=f"Вакансии LinkedIn: «{query}»",
            company="Международные компании и рекрутеры",
            salary="Валюта / ₽",
            url=f"https://www.linkedin.com/jobs/search/?keywords={q_enc}",
            description="Прямой фильтр вакансий LinkedIn по ключевым словам. Ищите рекрутера по компании и пишите лично.",
            city=city, trust_score=82, trust_badge="🟢 Профессиональная сеть",
        ))
    if "facebook" in selected:
        hubs.append(Vacancy(
            id="soc_facebook",
            platform="Facebook*",
            title=f"Вакансии и группы найма Facebook: «{q_full}»",
            company="Группы «Jobs», «Hire me», экспат-комьюнити",
            salary="Договорная",
            url=f"https://www.facebook.com/search/top?q={quote_plus(query + ' hiring')}",
            description="X-Ray по группам найма. Обязательно проверяйте работодателя и требуйте договор.",
            city=city, trust_score=60, trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ))
    if "instagram" in selected:
        hubs.append(Vacancy(
            id="soc_instagram",
            platform="Instagram* / Threads (X-Ray)",
            title=f"Посты найма «{q_full}» в Instagram/Threads",
            company="Агентства, эксперты, e-commerce, блогеры",
            salary="Проектная / Оклад",
            url=f"https://www.google.com/search?q={quote_plus(insta_q)}",
            description="Умный X-Ray запрос по хэштегам найма. Пишите напрямую в Директ, требуйте договор.",
            city=city, trust_score=60, trust_badge="🟡 Обязательно требуйте договор",
        ))
    if "x" in selected:
        hubs.append(Vacancy(
            id="soc_x",
            platform="X (Twitter)",
            title=f"Живая лента «hiring» в X: «{q_full}»",
            company="Основатели и HR в X (Twitter)",
            salary="Валюта / ₽",
            url=f"https://x.com/search?q={quote_plus(x_q)}&f=live",
            description="Фильтр «Последние»: свежие посты о найме от фаундеров — отклик быстрее, чем на джоб-борде.",
            city=city, trust_score=62, trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ))
    if "vk" in selected:
        hubs.append(Vacancy(
            id="soc_vk",
            platform="ВКонтакте",
            title=f"Посты ВК: #вакансия «{q_full}»",
            company="Сообщества и стены работодателей ВК",
            salary="Договорная",
            url=f"https://vk.com/feed?q={quote_plus('#вакансия ' + q_full)}&section=search",
            description="Прямой выход в ЛС к нанимающему менеджеру. Проверяйте сообщество на возраст и активность.",
            city=city, trust_score=65, trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ))
    if "ok" in selected:
        hubs.append(Vacancy(
            id="soc_ok",
            platform="Одноклассники",
            title=f"Группы ОК: «{q_full}»",
            company="Региональные группы и компании ОК",
            salary="Договорная",
            url=f"https://ok.ru/search?st.mode=Groups&st.query={q_job}",
            description="Поиск вакансий в региональных сообществах и группах прямых работодателей в ОК.",
            city=city, trust_score=65, trust_badge="🟡 Соцсеть (проверяйте профиль)",
        ))
    if "weworkremotely" in selected:
        hubs.append(Vacancy(
            id="soc_wwr",
            platform="We Work Remotely",
            title=f"Удалённые вакансии «{query}» на WWR",
            company="Global remote-first компании",
            salary="В USD/EUR",
            url=f"https://weworkremotely.com/remote-jobs/search?term={q_enc}",
            description="Крупнейшая доска полностью удалённых вакансий: разработка, поддержка, дизайн, маркетинг.",
            city=city, trust_score=85, trust_badge="🟢 Проверенная площадка",
        ))
    if "tgstat" in selected:
        hubs.append(Vacancy(
            id="soc_tgstat",
            platform="TGStat",
            title=f"Каталог Telegram-каналов с работой: «{query}»",
            company="Поиск каналов по охвату и тематике",
            salary="—",
            url=f"https://tgstat.ru/search?q={quote_plus(query + ' вакансии')}",
            description="Расширьте список каналов в настройках: найдите нишевые каналы с вакансиями и подпишитесь.",
            city=city, trust_score=70, trust_badge="🟡 Рекомендуется проверка",
        ))
    if "dribbble" in selected:
        hubs.append(Vacancy(
            id="soc_dribbble",
            platform="Dribbble Jobs",
            title=f"Дизайн-вакансии «{query}» на Dribbble",
            company="Студии, стартапы, дизайн-команды",
            salary="Проект / Оклад",
            url=f"https://dribbble.com/jobs?keywords={q_enc}",
            description="Вакансии и заказы для дизайнеров: UI/UX, брендинг, иллюстрация, моушн.",
            city=city, trust_score=80, trust_badge="🟢 Креативная площадка",
        ))
    if "behance" in selected:
        hubs.append(Vacancy(
            id="soc_behance",
            platform="Behance Jobs",
            title=f"Креативные вакансии «{query}» на Behance",
            company="Агентства, бренды, продакшены",
            salary="Проект / Оклад",
            url=f"https://www.behance.net/joblist?search={q_enc}",
            description="Заказы и найм для креативщиков: от графдизайна до арт-дирекшна.",
            city=city, trust_score=80, trust_badge="🟢 Креативная площадка",
        ))
    return hubs


# ─────────────────────────────────────────────────────────────────────────────
# ОРКЕСТРАТОР СОЦИАЛЬНОГО ПОИСКА
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_PARSERS = {
    "telegram": lambda q, limit: search_telegram_channels(q, channels=None, limit=limit),
    "reddit": lambda q, limit: search_reddit(q, limit=limit),
    "discord": lambda q, limit: search_discord_servers(q, limit=limit),
    "bluesky": lambda q, limit: search_bluesky(q, limit=limit),
    "mastodon": lambda q, limit: search_mastodon(q, limit=limit),
    "hn": lambda q, limit: search_hn_hiring(q, limit=limit),
    "remoteok": lambda q, limit: search_remoteok(q, limit=limit),
}


async def search_social_networks(
    query: str,
    city: str = "",
    networks: Optional[List[str]] = None,
    limit_per_network: int = 3,
) -> Dict[str, Any]:
    """
    Параллельный поиск по соцсетям и комьюнити-платформам.

    Возвращает словарь:
      - items: живые находки (список Vacancy-диктов с полем net)
      - hubs:  deep-link шлюзы в закрытые сети
      - networks: карточки выбранных сетей для интерфейса
      - stats: счётчики и ошибки по каждой сети
    """
    query = (query or "").strip()
    selected = [k for k in (networks or list(SOCIAL_NETWORKS)) if k in SOCIAL_NETWORKS]

    tasks: Dict[str, Any] = {}
    for key in selected:
        parser = _LIVE_PARSERS.get(key)
        if parser:
            tasks[key] = parser(query, limit_per_network)

    keys = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True) if tasks else []

    items: List[Dict[str, Any]] = []
    stats: List[Dict[str, Any]] = []
    for key, res in zip(keys, results):
        entry = {"network": key, "found": 0, "error": None}
        if isinstance(res, Exception):
            entry["error"] = str(res)[:120]
        elif isinstance(res, list):
            entry["found"] = len(res)
            for v in res:
                items.append(_vac_to_dict(v, key))
        stats.append(entry)

    hubs = generate_social_hubs(
        query,
        city=city,
        networks=[k for k in selected if SOCIAL_NETWORKS[k]["kind"] == "link"],
    )

    # Fallback-шлюз для живых сетей, которые сейчас не ответили, — чтобы у пользователя
    # всегда оставалась прямая возможность зайти и поискать.
    rematch = {s["network"] for s in stats if (s["found"] or 0) == 0}
    for key, url in _FALLBACK_HUBS.items():
        if key in selected and key in rematch:
            name = SOCIAL_NETWORKS[key]["name"]
            hubs.append(Vacancy(
                id=f"soc_{key}",
                platform=SOCIAL_NETWORKS[key]["name"],
                title=f"Открыть поиск «{query}» в {name}",
                company=f"{name} — перейти и поискать вручную",
                salary="—",
                url=url.format(q=quote_plus(query)),
                description=f"Живой парсер сейчас не ответил (сеть блокирует запросы без входа), но вы можете открыть поиск прямо в {name} и откликнуться напрямую.",
                trust_score=SOCIAL_NETWORKS[key]["trust"],
                trust_badge="🟡 Прямой поиск во внешней сети",
            ))

    return {
        "ok": True,
        "query": query,
        "city": city,
        "items": items,
        "hubs": [_vac_to_dict(v, v.id.replace("soc_", "")) for v in hubs],
        "networks": [SOCIAL_NETWORKS[k] for k in selected],
        "stats": stats,
        "total_live": len(items),
        "total_networks": len(selected),
    }


_FALLBACK_HUBS: Dict[str, str] = {
    "reddit": "https://www.reddit.com/search/?q={q}&sort=new",
    "discord": "https://disforge.com/servers/jobs",
    "bluesky": "https://bsky.app/search?q={q}",
    "mastodon": "https://mastodon.social/tags/{q}",
    "hn": "https://hn.algolia.com/?q={q}&tags=comment,story_whoishiring",
    "remoteok": "https://remoteok.com/remote-jobs?search={q}",
}
