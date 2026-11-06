# -*- coding: utf-8 -*-
"""
Оплата тарифов через Stripe Checkout (для Варианта Б — коммерческий сервис).

Как это работает (по шагам):
 1. Пользователь жмёт «Подключить» на тарифе → сервер создаёт «сессию оплаты»
    в Stripe и возвращает ссылку на защищённую страницу Stripe.
 2. Stripe принимает деньги и присылает нашему серверу webhook
    (запрос-уведомление) «оплата прошла».
 3. Сервер проверяет подпись webhook и включает тариф пользователю.

Важно:
 - Модуль не обязателен. Если Stripe не настроен (нет STRIPE_SECRET_KEY) —
   приложение работает в «демо-режиме»: тарифы активируются напрямую, без денег.
 - Цены задаются одним из двух способов (см. README, раздел «Оплата Stripe»):
   1) готовыми Stripe Price ID через STRIPE_PRICE_FAST / _STRONG / _MAX (рекомендуется);
   2) автоматически из цены в рублях (price_rub трактуется как центы валюты STRIPE_CURRENCY).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

try:
    import stripe
except ImportError:  # Stripe не установлен — модуль просто молчит
    stripe = None


def is_configured() -> bool:
    """Настроен ли Stripe (есть ключ и установлена библиотека)."""
    return stripe is not None and bool((os.environ.get("STRIPE_SECRET_KEY") or "").strip())


def _currency() -> str:
    return (os.environ.get("STRIPE_CURRENCY") or "usd").strip().lower()


def _price_id(plan_key: str) -> str:
    return (os.environ.get(f"STRIPE_PRICE_{plan_key.upper()}") or "").strip()


def build_line_items(plan_key: str, plan_name: str, price_rub: int) -> List[Dict[str, Any]]:
    """
    Позиции для Checkout Session.
    Если задан STRIPE_PRICE_<PLAN> — берём готовый Price ID, иначе собираем цену из кода.
    """
    pid = _price_id(plan_key)
    if pid:
        return [{"price": pid, "quantity": 1}]
    # Фолбэк: цена в рублях трактуется как копейки/центы выбранной валюты.
    # 690 ₽ → 6.90 usd/eur. Для точной цены задайте STRIPE_PRICE_<PLAN>.
    return [
        {
            "quantity": 1,
            "price_data": {
                "currency": _currency(),
                "product_data": {"name": plan_name},
                "unit_amount": max(50, int(price_rub)),
            },
        }
    ]


def create_checkout_session(
    user_id: int,
    plan_key: str,
    plan_name: str,
    price_rub: int,
    success_url: str,
    cancel_url: str,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    Создаёт сессию оплаты Stripe. Возвращает (сессия, ошибка).
    Сессия содержит поле .url — ссылку, куда вести пользователя.
    """
    if not is_configured():
        return None, "stripe_not_configured"
    try:
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"].strip()
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=build_line_items(plan_key, plan_name, price_rub),
            metadata={"user_id": str(user_id), "plan": plan_key},
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
        )
        return session, None
    except Exception as exc:  # noqa: BLE001 — не роняем сервер из-за проблем Stripe
        return None, f"{type(exc).__name__}: {exc}"


def verify_webhook(payload: bytes, sig_header: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Проверяет подпись webhook-запроса Stripe.
    Возвращает (событие, ошибка). Ошибка — значит подпись не сошлась (чужой запрос).
    """
    if stripe is None:
        return None, "stripe_not_installed"
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        return None, "no_webhook_secret"
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
        return event, None
    except Exception as exc:  # noqa: BLE001
        return None, f"signature_failed: {type(exc).__name__}"
