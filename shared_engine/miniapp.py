# -*- coding: utf-8 -*-
"""
Интеграция с Telegram Mini App:
 1. `validate_init_data()` — проверка подписи `initData` по алгоритму Telegram
    (HMAC-SHA256, ключ = HMAC("WebAppData", bot_token)).
 2. `webapp_url()` — сборка HTTPS-ссылки на Mini App для кнопок `web_app`.
 3. `configured_bots()` — какие токены ботов заданы в окружении и какому
    режиму («Личный» / «Сервис») соответствует каждый из них.

Telegram открывает Mini App только по HTTPS. Для локальной разработки
поднимите туннель (cloudflared / ngrok) и положите его адрес в `WEBAPP_URL`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

# Сутки: если initData старше, считаем подпись просроченной.
MAX_AUTH_AGE = 24 * 60 * 60

_ENV_BOTS: Tuple[Tuple[str, str], ...] = (
    ("PERSONAL_BOT_TOKEN", "personal"),
    ("SAAS_BOT_TOKEN", "saas"),
    ("MINIAPP_BOT_TOKEN", "personal"),
    ("BOT_TOKEN", "personal"),
)


def configured_bots() -> List[Tuple[str, str]]:
    """Список пар (режим, токен) без дублей — в порядке приоритета."""
    out: List[Tuple[str, str]] = []
    for env_name, mode in _ENV_BOTS:
        token = (os.environ.get(env_name) or "").strip()
        if token and all(token != known for _, known in out):
            out.append((mode, token))
    return out


def dev_fallback_allowed() -> bool:
    """Разрешать вход без подписи (локальная разработка / демо в браузере)."""
    flag = (os.environ.get("MINIAPP_DEV_FALLBACK") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Если боты ещё не подключены — это локальный запуск, подпись проверить нечем.
    return not configured_bots()


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def _check_hash(init_data: str, bot_token: str) -> Optional[Dict[str, str]]:
    """Проверяет HMAC-подпись и возвращает поля initData (без hash/signature)."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    except Exception:
        return None

    received_hash = pairs.pop("hash", "")
    # Ed25519-подпись (Bot API 8.0+) не входит в строку для HMAC.
    pairs.pop("signature", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    expected = hmac.new(_secret_key(bot_token), check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        return None
    return pairs


def validate_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """
    Полная проверка initData: подпись + срок жизни + разбор пользователя.
    Возвращает словарь с данными пользователя или None, если подпись неверна.
    """
    raw = (init_data or "").strip()
    if not raw:
        return None

    for mode, token in configured_bots():
        fields = _check_hash(raw, token)
        if not fields:
            continue

        try:
            auth_date = int(fields.get("auth_date") or 0)
        except (TypeError, ValueError):
            auth_date = 0
        if auth_date and (time.time() - auth_date) > MAX_AUTH_AGE:
            return None

        try:
            user = json.loads(fields.get("user") or "{}")
        except Exception:
            user = {}
        if not isinstance(user, dict) or not user.get("id"):
            return None

        user_id = int(user["id"])
        first = str(user.get("first_name") or "").strip()
        last = str(user.get("last_name") or "").strip()
        return {
            "verified": True,
            "mode": mode,
            "user_id": user_id,
            "username": str(user.get("username") or ""),
            "first_name": first,
            "last_name": last,
            "full_name": " ".join(p for p in (first, last) if p).strip(),
            "language_code": str(user.get("language_code") or ""),
            "is_premium": bool(user.get("is_premium")),
            "auth_date": auth_date,
            "start_param": str(fields.get("start_param") or ""),
        }
    return None


def webapp_url(mode: str = "personal", anchor: str = "") -> str:
    """
    HTTPS-адрес Mini App из `WEBAPP_URL`. Пустая строка, если адрес не задан
    или не является HTTPS — Telegram не примет такую кнопку.
    """
    base = (os.environ.get("WEBAPP_URL") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return ""
    url = f"{base}/?mode=saas" if mode == "saas" else f"{base}/"
    if anchor:
        url += f"#{anchor.lstrip('#')}"
    return url


def _session_secret() -> bytes:
    """Секрет для подписи сессионных токенов Mini App."""
    explicit = (os.environ.get("MINIAPP_SESSION_SECRET") or "").strip()
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).digest()
    bots = configured_bots()
    if bots:
        return hmac.new(b"WebAppSession", bots[0][1].encode("utf-8"), hashlib.sha256).digest()
    return b"jonathan-local-dev-session"


def make_session(user_id: int, auth_date: int) -> str:
    """
    Короткий сессионный токен: `<auth_date>.<hmac>`.
    Подписывает пару (user_id, auth_date), чтобы клиент не мог подставить
    чужой user_id в запросы после проверки initData.
    """
    mac = hmac.new(
        _session_secret(),
        f"{int(user_id)}:{int(auth_date)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{int(auth_date)}.{mac}"


def verify_session(user_id: Any, token: str) -> bool:
    """Проверка сессионного токена (с тем же сроком жизни, что и initData)."""
    try:
        auth_date_raw, mac = str(token or "").split(".", 1)
        auth_date = int(auth_date_raw)
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if auth_date <= 0 or (time.time() - auth_date) > MAX_AUTH_AGE:
        return False
    expected = hmac.new(
        _session_secret(),
        f"{uid}:{auth_date}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, mac)


def auth_required() -> bool:
    """Нужно ли требовать подписанную сессию на API (боевой режим)."""
    return bool(configured_bots()) and not dev_fallback_allowed()
