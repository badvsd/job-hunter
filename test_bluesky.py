# -*- coding: utf-8 -*-
r"""
Диагностика живого поиска Bluesky.

Запуск (после создания App Password в настройках Bluesky):
    # Windows (PowerShell):
    $env:BSKY_HANDLE="ваш.bsky.social"
    $env:BSKY_APP_PASSWORD="xxxx-xxxx-xxxx"
    python -X utf8 test_bluesky.py

    # Mac / Linux:
    export BSKY_HANDLE="ваш.bsky.social"
    export BSKY_APP_PASSWORD="xxxx-xxxx-xxxx"
    python -X utf8 test_bluesky.py

Скрипт по шагам показывает: что видит из окружения, проходит ли вход,
обновляется ли сессия и возвращается ли живой поиск постов.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import httpx

from shared_engine.social_search import (
    SOCIAL_HEADERS,
    _BSKY_SESSION,
    _bsky_access_token,
    search_bluesky,
)


def _mask(v: str) -> str:
    if not v:
        return "—"
    if len(v) <= 8:
        return "***"
    return v[:6] + "…" + v[-4:]


async def main() -> None:
    handle = os.environ.get("BSKY_HANDLE") or os.environ.get("BLUESKY_HANDLE") or ""
    password = os.environ.get("BSKY_APP_PASSWORD") or os.environ.get("BLUESKY_APP_PASSWORD") or ""
    token = os.environ.get("BSKY_TOKEN") or ""

    print("=== 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===")
    print(f"BSKY_HANDLE        : {handle or '—'}")
    print(f"BSKY_APP_PASSWORD  : {_mask(password)}")
    print(f"BSKY_TOKEN         : {_mask(token)}")
    if not (token or (handle and password)):
        print("\n❌ Токенов нет. Задайте BSKY_HANDLE + BSKY_APP_PASSWORD и перезапустите.")
        print("   App Password: Bluesky → Settings → Privacy and security → App passwords")
        return

    print("\n=== 2. ВХОД (createSession) ===")
    async with httpx.AsyncClient(timeout=12.0, headers=SOCIAL_HEADERS, follow_redirects=True) as client:
        access = await _bsky_access_token(client)
        if not access:
            print("❌ Вход не удался: проверьте handle и App Password (не обычный пароль аккаунта!).")
            return
        print(f"✅ Вход выполнен, accessJwt: {_mask(access)}")
        print(f"   Кэш сессии: exp через {max(0, int(_BSKY_SESSION['exp'] - __import__('time').time()))} сек")

        print("\n=== 3. ПОВТОРНЫЙ ВЫЗОВ (должен взять токен из кэша) ===")
        again = await _bsky_access_token(client)
        print("✅ Из кэша:" if _BSKY_SESSION["access"] == again else "⚠️ Токен изменился:", _mask(again or ""))

    print("\n=== 4. ЖИВОЙ ПОИСК ПОСТОВ ===")
    posts = await search_bluesky("hiring python", limit=3)
    print(f"Найдено: {len(posts)}")
    for v in posts:
        print(f"  - {v.title[:70]} | {v.company} | {v.url[:70]}")
    if not posts:
        print("ℹ️ Вход прошёл, но релевантных постов о найме сейчас нет — попробуйте другой запрос.")

    print("\n=== 5. ИНТЕГРАЦИЯ С ОРКЕСТРАТОРОМ ===")
    from shared_engine import search_social_networks

    soc = await search_social_networks("python разработчик", limit_per_network=2)
    bsky_stat = next((s for s in soc["stats"] if s["network"] == "bluesky"), None)
    print(f"Статистика bluesky: {bsky_stat}")
    print(f"Итого по всем сетям: живых {soc['total_live']}, шлюзов {len(soc['hubs'])}")
    print("\n✅ Диагностика завершена." if posts else "\n⚠️ Парсер подключён, но постов не нашёл (см. шаг 4).")


if __name__ == "__main__":
    asyncio.run(main())
