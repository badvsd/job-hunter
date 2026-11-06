# -*- coding: utf-8 -*-
"""
Веб-сервер и API для приложения «Джонатан · Свободный полёт к работе мечты»
(Telegram Mini App + Веб-сайт в стиле Scandi / Cozy Home).

Поддерживает переключение и раздельную работу обоих режимов:
 - Вариант А (Personal Edition): `/app?mode=personal` (или по умолчанию) — безлимитный уютный комбайн для себя.
 - Вариант Б (SaaS Edition): `/app?mode=saas` — коммерческая версия с тарифами, подарочными днями и рефералкой (+750 ₽).

Эндпоинты API:
 - GET  /                     -> Главный веб-интерфейс / Telegram Mini App
 - GET  /api/state            -> Состояние профиля, режима (A/B), тарифа и живого полёта Джонатана
 - POST /api/search           -> Мульти-поиск по всем площадкам Рунета (HH, Работа России, TG, Авито, ВК, ОК, LinkedIn, Insta)
 - POST /api/scam-check       -> Глубокая СБ-проверка работодателя (ИНН ФНС, суды, маркеры скама, ИИ-аудит)
 - POST /api/adapt-resume     -> ИИ-адаптация резюме под вакансию или аудит слабых мест
 - POST /api/cover-letter     -> Генерация сопроводительного письма / короткого питча для TG/ВК/Авито
 - POST /api/settings         -> Сохранение настроек поиска, резюме и переключение режима полёта
 - POST /api/saas/buy         -> Покупка тарифа / активация подарочного дня (для Варианта Б)

Автопилот откликов (Playwright):
 - GET  /api/apply/queue      -> очередь задач, счётчики, состояние воркера и лимиты
 - POST /api/apply/enqueue    -> поставить вакансию в очередь (письмо сгенерируется, если не передано)
 - POST /api/apply/run        -> запустить воркер в фоне
 - POST /api/apply/stop       -> остановиться после текущего отклика
 - POST /api/apply/retry      -> вернуть задачу в очередь
 - POST /api/apply/cancel     -> отменить задачу
 - POST /api/apply/clear      -> убрать завершённые задачи
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_engine import (
    DEFAULT_TG_CHANNELS,
    RESUME_FOCUSES,
    SOCIAL_NETWORKS,
    adapt_resume_for_vacancy,
    build_resume_variant,
    deep_check_employer,
    generate_cover_letter,
    generate_crossplatform_search_hubs,
    pick_best_resume_variant,
    search_all_platforms,
    search_hh,
    search_social_networks,
    search_telegram_channels,
    search_trudvsem,
    verify_resume_facts,
)
from variant_a_personal.bot_personal import PersonalDB
from variant_b_saas.bot_saas import PLANS, SaaSDB
from shared_engine import stripe_pay
from shared_engine.miniapp import (
    auth_required,
    configured_bots,
    dev_fallback_allowed,
    make_session,
    validate_init_data,
    verify_session,
    webapp_url,
)
from worker.autoapply import (
    AUTO_PLATFORMS,
    SHOT_DIR,
    STATUS_LABELS,
    platform_of,
    queue as apply_queue,
    settings as apply_settings,
    worker as apply_worker,
)

app = FastAPI(title="Jonathan Livingston — Cozy Scandi Job Hunter")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
personal_db = PersonalDB()
saas_db = SaaSDB()

DEFAULT_DEMO_RESUME = (
    "Опытный специалист. Уверенный пользователь современных цифровых инструментов, "
    "аналитика, автоматизация процессов, ведение проектов от идеи до результата, "
    "работа в кросс-функциональной команде, ориентация на бизнес-метрики и качество."
)


class SearchReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"  # 'personal' (Вариант А) | 'saas' (Вариант Б)
    query: str = ""
    city: str = ""
    min_salary: int = 0
    source: str = "all"     # 'all' | 'hh' | 'tg' | 'tv' | 'hubs'


class ScamCheckReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    target: str


class AdaptResumeReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    resume_text: str = ""
    vacancy_text: str = ""
    style: str = "ats"


class CoverLetterReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    resume_text: str = ""
    vacancy_text: str = ""
    tone: str = "natural"
    platform: str = "HH.ru"


class SettingsReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    search_query: Optional[str] = None
    city: Optional[str] = None
    min_salary: Optional[int] = None
    tone: Optional[str] = None
    resume_text: Optional[str] = None
    tg_channels: Optional[str] = None
    autopilot: Optional[int] = None


class BuyPlanReq(BaseModel):
    user_id: int = 10001
    action: str  # 'fast' | 'strong' | 'max' | 'gift'


class StripeCheckoutReq(BaseModel):
    user_id: int = 10001
    mode: str = "saas"
    plan: str  # 'fast' | 'strong' | 'max'


class TelegramAuthReq(BaseModel):
    init_data: str = ""
    fallback_user_id: int = 10001
    mode: str = "personal"


class ApplyEnqueueReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    url: str
    title: str = ""
    company: str = ""
    platform: str = ""
    letter: str = ""


class ApplyRunReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    limit: int = 3
    dry_run: Optional[bool] = None
    answer_questions: Optional[bool] = None


class ApplyTaskReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    task_id: int


class ApplyUpdateLetterReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    task_id: int
    letter: str = ""


class ApplyBatchReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    items: List[Dict[str, Any]]
    min_trust: int = 75
    limit: int = 6


class ApplyUserReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"


class ResumeGenerateReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    focus: str = "all"  # 'all' | 'ats' | 'numbers' | 'stack' | 'team' | 'product'
    target_role: str = ""
    vacancy_text: str = ""
    resume_text: str = ""
    use_llm: bool = True


class ResumeMatchReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    vacancy_title: str = ""
    vacancy_text: str = ""


class ResumeDeleteReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    slot: int = 1


class SocialSearchReq(BaseModel):
    user_id: int = 10001
    mode: str = "personal"
    query: str = ""
    city: str = ""
    networks: List[str] = []
    limit_per_network: int = 3


def ensure_user(request: Request, user_id: int) -> None:
    """
    В боевом режиме (токены ботов заданы, dev-fallback выключен) API доступно
    только с подписанной сессией Mini App — нельзя читать чужие профили,
    подставив другой user_id.
    """
    if not auth_required():
        return
    if not verify_session(user_id, request.headers.get("x-session") or ""):
        raise HTTPException(status_code=401, detail="Нужен вход через Telegram")


def _apply_summary(user_id: int) -> Dict[str, Any]:
    """Короткая сводка для главного экрана: сколько в очереди и летит ли воркер."""
    counts = apply_queue.counts(user_id)
    cfg = apply_settings()
    return {
        "counts": counts,
        "running": bool(apply_worker.state.get("running")),
        "dry_run": cfg["dry_run"],
        "queued": counts.get("queued", 0),
        "sent": counts.get("sent", 0),
        "ready": counts.get("ready", 0),
    }


def _apply_payload(user_id: int) -> Dict[str, Any]:
    """Полное состояние автопилота: задачи, счётчики, воркер, лимиты."""
    cfg = apply_settings()
    return {
        "tasks": apply_queue.list(user_id),
        "counts": apply_queue.counts(user_id),
        "worker": apply_worker.snapshot(),
        "labels": STATUS_LABELS,
        "settings": {
            "dry_run": cfg["dry_run"],
            "daily_limit": cfg["daily_limit"],
            "min_delay": cfg["min_delay"],
            "max_delay": cfg["max_delay"],
            "headless": cfg["headless"],
            "answer_questions": cfg.get("answer_questions", True),
            "max_attempts": cfg.get("max_attempts", 3),
            "auto_platforms": sorted(AUTO_PLATFORMS),
            "used_today": apply_queue.used_today(user_id),
        },
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
async def index_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def api_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "jonathan-miniapp",
        "bots_configured": [mode for mode, _ in configured_bots()],
        "auth_enforced": bool(configured_bots()) and not dev_fallback_allowed(),
        "webapp_url": webapp_url("personal"),
    }


@app.post("/api/auth/telegram")
async def api_auth_telegram(req: TelegramAuthReq) -> Dict[str, Any]:
    """
    Вход через Telegram Mini App.
    Подпись initData проверяется HMAC-SHA256 с токеном бота; при валидной
    подписи пользователь автоматически заводится в обеих базах.
    В локальном режиме (токены ботов не заданы) допускается гостевой вход.
    """
    info = validate_init_data(req.init_data)

    if not info:
        if not dev_fallback_allowed():
            raise HTTPException(
                status_code=401,
                detail="Подпись Telegram не совпала. Откройте приложение из бота.",
            )
        info = {
            "verified": False,
            "mode": req.mode if req.mode in ("personal", "saas") else "personal",
            "user_id": int(req.fallback_user_id or 10001),
            "username": "",
            "first_name": "",
            "last_name": "",
            "full_name": "",
            "language_code": "",
            "is_premium": False,
            "auth_date": int(time.time()),
            "start_param": "",
        }

    uid = int(info["user_id"])
    start_param = (info.get("start_param") or "").lower()
    if start_param.startswith("saas") or "mode=saas" in start_param:
        info["mode"] = "saas"
    mode = info["mode"] if info["mode"] in ("personal", "saas") else "personal"

    personal_db.get_profile(uid)
    if info.get("username"):
        personal_db.update_field(uid, "tg_username", info["username"])
    if info.get("full_name"):
        personal_db.update_field(uid, "tg_name", info["full_name"])

    saas_db.get_or_create_user(
        uid,
        username=info.get("username") or "",
        full_name=info.get("full_name") or "",
    )
    if info.get("username"):
        saas_db.update_field(uid, "username", info["username"])
    if info.get("full_name"):
        saas_db.update_field(uid, "full_name", info["full_name"])

    auth_date = int(info.get("auth_date") or time.time())
    return {
        "ok": True,
        "auth": info,
        "session": make_session(uid, auth_date),
        "state": await _state_payload(user_id=uid, mode=mode),
    }


@app.get("/api/state")
async def api_state(
    request: Request,
    user_id: int = Query(10001),
    mode: str = Query("personal"),
) -> Dict[str, Any]:
    ensure_user(request, user_id)
    return await _state_payload(user_id=user_id, mode=mode)


def _resumes_payload(user_id: int) -> Dict[str, Any]:
    """До 5 A/B-копий резюме + справочник акцентов."""
    variants = personal_db.get_resume_variants(user_id)
    for v in variants:
        meta = RESUME_FOCUSES.get(str(v.get("focus") or "ats"), RESUME_FOCUSES["ats"])
        v["code"] = meta["code"]
        v["label"] = meta["label"]
        v["short"] = meta["short"]
        v["hint"] = meta["hint"]
    return {
        "variants": variants,
        "count": len(variants),
        "max_slots": 5,
        "focuses": list(RESUME_FOCUSES.values()),
    }


async def _state_payload(user_id: int = 10001, mode: str = "personal") -> Dict[str, Any]:
    prof = personal_db.get_profile(user_id)
    if not prof.get("resume_text"):
        prof["resume_text"] = DEFAULT_DEMO_RESUME
        personal_db.update_field(user_id, "resume_text", DEFAULT_DEMO_RESUME)

    saas_user, _ = saas_db.get_or_create_user(user_id, username="jonathan_flier")
    if not saas_user.get("resume_text"):
        saas_db.update_field(user_id, "resume_text", prof["resume_text"])
        saas_user["resume_text"] = prof["resume_text"]

    plan_key = saas_user.get("plan", "trial")
    plan_info = PLANS.get(plan_key, PLANS["trial"])
    saved = personal_db.get_saved(user_id, limit=20)

    return {
        "ok": True,
        "mode": mode,
        "personal": prof,
        "saas": {
            "user": saas_user,
            "plan_info": plan_info,
            "plans": PLANS,
            "admin_stats": saas_db.get_admin_stats(),
        },
        "saved": saved,
        "telegram": {
            "auth_enforced": bool(configured_bots()) and not dev_fallback_allowed(),
            "bots": [m for m, _ in configured_bots()],
            "bot_username": (os.environ.get("BOT_USERNAME") or "").strip().lstrip("@"),
            "webapp_url": webapp_url(mode),
        },
        "apply": _apply_summary(user_id),
        "resumes": _resumes_payload(user_id),
    }


@app.post("/api/search")
async def api_search(req: SearchReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    if req.mode == "saas":
        ok, msg = saas_db.check_and_consume_quota(req.user_id, "search")
        if not ok:
            return {"ok": False, "quota_exceeded": True, "error": msg}

    prof = personal_db.get_profile(req.user_id)
    q = (req.query or prof.get("search_query") or "Python разработчик").strip()
    city = (req.city or prof.get("city") or "Москва").strip()
    sal = req.min_salary if req.min_salary is not None else int(prof.get("min_salary") or 0)
    channels = [c.strip().lstrip("@") for c in (prof.get("tg_channels") or "").split(",") if c.strip()]

    if req.query:
        personal_db.update_field(req.user_id, "search_query", q)
        saas_db.update_field(req.user_id, "search_query", q)

    if req.source == "hh":
        items = await search_hh(q, city=city, min_salary=sal, limit=12)
    elif req.source == "tg":
        items = await search_telegram_channels(q, channels=channels, limit=12)
    elif req.source == "tv":
        items = await search_trudvsem(f"{q} {city}".strip(), limit=12)
    elif req.source == "hubs":
        items = generate_crossplatform_search_hubs(q, city=city)
    else:
        items = await search_all_platforms(q, city=city, min_salary=sal, tg_channels=channels)

    return {
        "ok": True,
        "query": q,
        "city": city,
        "count": len(items),
        "items": [v.__dict__ for v in items],
    }


@app.post("/api/scam-check")
async def api_scam_check(req: ScamCheckReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    if not req.target or len(req.target.strip()) < 2:
        raise HTTPException(status_code=400, detail="Введите ИНН, название компании или текст вакансии")

    if req.mode == "saas":
        ok, msg = saas_db.check_and_consume_quota(req.user_id, "scam")
        if not ok:
            return {"ok": False, "quota_exceeded": True, "error": msg}

    report = await deep_check_employer(req.target)
    return {"ok": True, "report": report}


@app.post("/api/adapt-resume")
async def api_adapt_resume(req: AdaptResumeReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    if req.mode == "saas":
        ok, msg = saas_db.check_and_consume_quota(req.user_id, "ai")
        if not ok:
            return {"ok": False, "quota_exceeded": True, "error": msg}

    prof = personal_db.get_profile(req.user_id)
    resume = (req.resume_text or prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    if req.resume_text and len(req.resume_text.strip()) > 25:
        personal_db.update_field(req.user_id, "resume_text", resume)
        saas_db.update_field(req.user_id, "resume_text", resume)

    vac = (req.vacancy_text or f"Целевая позиция: {prof.get('search_query', 'Специалист')}").strip()
    result = await adapt_resume_for_vacancy(resume, vac, mode=req.style)
    fact_check = verify_resume_facts(resume, result, vac)
    return {"ok": True, "result": result, "fact_check": fact_check}


@app.get("/api/resumes")
async def api_resumes_list(
    request: Request,
    user_id: int = Query(10001),
) -> Dict[str, Any]:
    ensure_user(request, user_id)
    return {"ok": True, **_resumes_payload(user_id)}


@app.post("/api/resumes/generate")
async def api_resumes_generate(req: ResumeGenerateReq, request: Request) -> Dict[str, Any]:
    """
    Создаёт одну целевую копию резюме по выбранному акценту либо сразу все 5
    A/B-копий (ATS, Цифры, Стек, Команда, Продукт) с проверкой фактов.
    """
    ensure_user(request, req.user_id)
    if req.mode == "saas":
        ok, msg = saas_db.check_and_consume_quota(req.user_id, "ai")
        if not ok:
            return {"ok": False, "quota_exceeded": True, "error": msg}

    prof = personal_db.get_profile(req.user_id)
    base_resume = (req.resume_text or prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    if req.resume_text and len(req.resume_text.strip()) > 25:
        personal_db.update_field(req.user_id, "resume_text", base_resume)
        saas_db.update_field(req.user_id, "resume_text", base_resume)

    role = (req.target_role or prof.get("search_query") or "Python разработчик").strip()
    focus_keys = (
        list(RESUME_FOCUSES.keys())
        if req.focus == "all" or req.focus not in RESUME_FOCUSES
        else [req.focus]
    )

    generated: List[Dict[str, Any]] = []
    for f_key in focus_keys:
        built = await build_resume_variant(
            base_resume=base_resume,
            focus=f_key,
            target_role=role,
            vacancy_info=req.vacancy_text,
            use_llm=bool(req.use_llm),
        )
        saved_row = personal_db.upsert_resume_variant(
            user_id=req.user_id,
            slot=built["slot"],
            focus=built["focus"],
            title=built["title"],
            content=built["content"],
            fact_ok=built["fact_ok"],
            fact_badge=built["fact_badge"],
        )
        generated.append({**built, **saved_row})

    payload = _resumes_payload(req.user_id)
    return {
        "ok": True,
        "generated": generated,
        "message": (
            f"Создано 5 A/B-копий резюме под роль «{role}» (факты проверены)"
            if len(generated) > 1
            else f"Копия «{generated[0]['label']}» сохранена ({generated[0]['fact_badge']})"
        ),
        **payload,
    }


@app.post("/api/resumes/match")
async def api_resumes_match(req: ResumeMatchReq, request: Request) -> Dict[str, Any]:
    """Автоматический выбор лучшей A/B-копии резюме под конкретную вакансию."""
    ensure_user(request, req.user_id)
    prof = personal_db.get_profile(req.user_id)
    base_resume = (prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    variants = personal_db.get_resume_variants(req.user_id)
    match = pick_best_resume_variant(
        variants=variants,
        vacancy_title=req.vacancy_title or prof.get("search_query") or "",
        vacancy_text=req.vacancy_text or "",
        base_resume=base_resume,
    )
    return {"ok": True, **match, **_resumes_payload(req.user_id)}


@app.post("/api/resumes/delete")
async def api_resumes_delete(req: ResumeDeleteReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    ok = personal_db.delete_resume_variant(req.user_id, req.slot)
    return {
        "ok": ok,
        "message": f"Копия #{req.slot} удалена" if ok else "Копия не найдена",
        **_resumes_payload(req.user_id),
    }


@app.post("/api/social/search")
async def api_social_search(req: SocialSearchReq, request: Request) -> Dict[str, Any]:
    """
    Поиск по соцсетям и комьюнити-платформам: живые парсеры (Telegram, Reddit,
    Discord, Bluesky, Mastodon, Hacker News, RemoteOK) + deep-link шлюзы
    в закрытые сети (LinkedIn, Facebook, Instagram, X, ВК, ОК, WWR, Dribbble…).
    """
    ensure_user(request, req.user_id)
    prof = personal_db.get_profile(req.user_id)
    query = (req.query or prof.get("search_query") or "специалист").strip()
    city = (req.city or prof.get("city") or "").strip()
    result = await search_social_networks(
        query=query,
        city=city,
        networks=req.networks or None,
        limit_per_network=max(1, min(int(req.limit_per_network or 3), 5)),
    )
    return result


@app.get("/api/social/networks")
async def api_social_networks() -> Dict[str, Any]:
    """Каталог поддерживаемых соцсетей и комьюнити-платформ."""
    return {
        "ok": True,
        "networks": list(SOCIAL_NETWORKS.values()),
        "live": [k for k, n in SOCIAL_NETWORKS.items() if n["kind"] == "live"],
        "hubs": [k for k, n in SOCIAL_NETWORKS.items() if n["kind"] == "link"],
    }


@app.post("/api/cover-letter")
async def api_cover_letter(req: CoverLetterReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    if req.mode == "saas":
        ok, msg = saas_db.check_and_consume_quota(req.user_id, "ai")
        if not ok:
            return {"ok": False, "quota_exceeded": True, "error": msg}

    prof = personal_db.get_profile(req.user_id)
    base_resume = (req.resume_text or prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    vac = (req.vacancy_text or f"Вакансия по направлению {prof.get('search_query')}").strip()

    variants = personal_db.get_resume_variants(req.user_id)
    match = pick_best_resume_variant(
        variants=variants,
        vacancy_title=vac.splitlines()[0][:100] if vac else "",
        vacancy_text=vac,
        base_resume=base_resume,
    )
    best_var = match["best"]
    if variants and best_var.get("slot"):
        personal_db.bump_variant_used(req.user_id, int(best_var["slot"]))

    effective_resume = f"{base_resume}\n\nВыбранный акцент резюме ({best_var['label']} — {best_var['title']}):\n{best_var['content']}"
    letter = await generate_cover_letter(effective_resume, vac, tone=req.tone, platform=req.platform)
    return {
        "ok": True,
        "letter": letter,
        "selected_resume": {
            "slot": best_var.get("slot", 1),
            "code": best_var.get("code", "A"),
            "label": best_var.get("label", "A · ATS и ключи"),
            "title": best_var.get("title", ""),
            "match_score": match["match_score"],
            "reason": match["reason"],
        },
    }


@app.post("/api/settings")
async def api_save_settings(req: SettingsReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    fields = ["search_query", "city", "min_salary", "tone", "resume_text", "tg_channels", "autopilot"]
    for f in fields:
        val = getattr(req, f)
        if val is not None:
            personal_db.update_field(req.user_id, f, val)
            if f in ("search_query", "city", "min_salary", "resume_text"):
                saas_db.update_field(req.user_id, f, val)
    return await _state_payload(user_id=req.user_id, mode=req.mode)


@app.post("/api/saas/buy")
async def api_saas_buy(req: BuyPlanReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    if req.action == "gift":
        ok, msg = saas_db.activate_gift_day(req.user_id)
        st = await _state_payload(user_id=req.user_id, mode="saas")
        return {"ok": ok, "message": msg, "state": st}

    if req.action not in PLANS:
        raise HTTPException(status_code=400, detail="Неизвестный тариф")

    plan = PLANS[req.action]
    u, _ = saas_db.get_or_create_user(req.user_id)
    discount = min(u["balance_rub"], plan["price_rub"] // 2)
    final_price = plan["price_rub"] - discount
    if discount > 0:
        saas_db.update_field(req.user_id, "balance_rub", u["balance_rub"] - discount)
    saas_db.activate_plan(req.user_id, req.action, amount_rub=final_price)
    st = await _state_payload(user_id=req.user_id, mode="saas")
    plain_name = re.sub(r"^[^\w]+", "", plan["name"]).strip()
    return {
        "ok": True,
        "message": f"Тариф «{plain_name}» подключён. Бонусами списано: {discount} ₽.",
        "state": st,
    }


@app.post("/api/saas/checkout")
async def api_saas_checkout(req: StripeCheckoutReq, request: Request) -> Dict[str, Any]:
    """
    Создаёт Stripe Checkout Session и возвращает ссылку на оплату.
    Если Stripe не настроен — активирует тариф напрямую (демо-режим, для локальной разработки).
    """
    ensure_user(request, req.user_id)
    if req.plan not in PLANS or req.plan == "trial":
        raise HTTPException(status_code=400, detail="Неизвестный тариф")

    plan = PLANS[req.plan]

    if not stripe_pay.is_configured():
        u, _ = saas_db.get_or_create_user(req.user_id)
        discount = min(int(u.get("balance_rub") or 0), plan["price_rub"] // 2)
        final_price = plan["price_rub"] - discount
        if discount > 0:
            saas_db.update_field(req.user_id, "balance_rub", int(u["balance_rub"]) - discount)
        saas_db.activate_plan(req.user_id, req.plan, amount_rub=final_price)
        st = await _state_payload(user_id=req.user_id, mode="saas")
        return {
            "ok": True,
            "demo": True,
            "message": f"Тариф «{plan['name']}» активирован (демо-режим: Stripe не настроен).",
            "state": st,
        }

    base = str(request.base_url).rstrip("/")
    success_url = (os.environ.get("STRIPE_SUCCESS_URL") or f"{base}/app?mode=saas&pay=success").strip()
    cancel_url = (os.environ.get("STRIPE_CANCEL_URL") or f"{base}/app?mode=saas&pay=cancel").strip()

    session, err = stripe_pay.create_checkout_session(
        user_id=req.user_id,
        plan_key=req.plan,
        plan_name=plan["name"],
        price_rub=plan["price_rub"],
        success_url=success_url,
        cancel_url=cancel_url,
    )
    if err:
        raise HTTPException(status_code=400, detail=f"Не удалось создать оплату: {err}")
    return {"ok": True, "url": session.url}


@app.post("/api/saas/webhook")
async def api_saas_webhook(request: Request) -> Dict[str, Any]:
    """
    Приём уведомлений Stripe об оплате (webhook). Подпись проверяется
    по STRIPE_WEBHOOK_SECRET — чужие запросы отбрасываются.
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature") or ""
    event, err = stripe_pay.verify_webhook(payload, sig)
    if err:
        raise HTTPException(status_code=400, detail=err)

    if event and event.get("type") == "checkout.session.completed":
        session = event.get("data", {}).get("object", {}) or {}
        meta = session.get("metadata") or {}
        uid = int(meta.get("user_id") or 0)
        plan_key = str(meta.get("plan") or "")
        if uid and plan_key in PLANS and plan_key != "trial":
            # Сумму в рублях берём из прайса — учёт выручки ведётся в рублях.
            saas_db.activate_plan(uid, plan_key, amount_rub=PLANS[plan_key]["price_rub"])

    return {"received": True}


# ─────────────────────────────────────────────────────────────────────────────
# АВТОПИЛОТ ОТКЛИКОВ
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/apply/queue")
async def api_apply_queue(
    request: Request,
    user_id: int = Query(10001),
    mode: str = Query("personal"),
) -> Dict[str, Any]:
    ensure_user(request, user_id)
    return {"ok": True, **_apply_payload(user_id)}


@app.post("/api/apply/enqueue")
async def api_apply_enqueue(req: ApplyEnqueueReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    url = (req.url or "").strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Нужна ссылка на вакансию")

    prof = personal_db.get_profile(req.user_id)
    base_resume = (prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    variants = personal_db.get_resume_variants(req.user_id)
    vac_text = f"{req.title or 'Вакансия'} в {req.company or 'компании'}. {req.platform or ''}".strip()
    match = pick_best_resume_variant(
        variants=variants,
        vacancy_title=req.title or prof.get("search_query") or "",
        vacancy_text=vac_text,
        base_resume=base_resume,
    )
    best_var = match["best"]
    variant_tag = f"{best_var.get('code', 'A')} · {best_var.get('title', '')}".strip(" ·")

    letter = (req.letter or "").strip()
    if not letter:
        if req.mode == "saas":
            ok, msg = saas_db.check_and_consume_quota(req.user_id, "ai")
            if not ok:
                return {"ok": False, "quota_exceeded": True, "error": msg}
        effective_resume = f"{base_resume}\n\nВыбранный акцент резюме ({best_var['label']}):\n{best_var['content']}"
        letter = await generate_cover_letter(
            effective_resume,
            vac_text,
            tone=(prof.get("tone") or "natural"),
            platform=(req.platform or "HH.ru"),
        )

    task, created = apply_queue.add(
        user_id=req.user_id,
        url=url,
        title=req.title,
        company=req.company,
        platform=req.platform or platform_of(url),
        letter=letter,
        resume_variant=variant_tag,
    )
    if not task:
        raise HTTPException(status_code=400, detail="Не удалось создать задачу")

    if created and variants and best_var.get("slot"):
        personal_db.bump_variant_used(req.user_id, int(best_var["slot"]))

    if not created:
        message = "Эта вакансия уже в очереди"
    elif task["status"] == "queued":
        message = f"В очереди · резюме {best_var.get('code', 'A')} ({match['match_score']}%)"
    else:
        message = f"Письмо готово (резюме {best_var.get('code', 'A')}) — площадка требует ручного отклика"

    return {
        "ok": True,
        "created": created,
        "task": task,
        "selected_resume": {
            "slot": best_var.get("slot", 1),
            "code": best_var.get("code", "A"),
            "label": best_var.get("label", ""),
            "title": best_var.get("title", ""),
            "match_score": match["match_score"],
            "reason": match["reason"],
        },
        "message": message,
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/run")
async def api_apply_run(req: ApplyRunReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    prof = personal_db.get_profile(req.user_id)
    extra_cfg: Dict[str, Any] = {
        "resume_text": (prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip(),
        "min_salary": int(prof.get("min_salary") or 0),
    }
    if req.answer_questions is not None:
        extra_cfg["answer_questions"] = bool(req.answer_questions)
    started = apply_worker.start(
        req.user_id,
        limit=req.limit,
        dry_run=req.dry_run,
        extra_cfg=extra_cfg,
    )
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error"), **_apply_payload(req.user_id)}
    await asyncio.sleep(0.2)  # дать воркеру выставить флаг running
    return {
        "ok": True,
        "message": started["message"],
        "dry_run": started["dry_run"],
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/update-letter")
async def api_apply_update_letter(req: ApplyUpdateLetterReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    task = apply_queue.get(req.task_id)
    if not task or int(task["user_id"]) != int(req.user_id):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    ok = apply_queue.update_letter(req.task_id, req.letter)
    return {
        "ok": ok,
        "message": "Письмо сохранено" if ok else "Нельзя изменить письмо у уже отправленной задачи",
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/batch-enqueue")
async def api_apply_batch_enqueue(req: ApplyBatchReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    prof = personal_db.get_profile(req.user_id)
    resume = (prof.get("resume_text") or DEFAULT_DEMO_RESUME).strip()
    variants = personal_db.get_resume_variants(req.user_id)
    tone = prof.get("tone") or "natural"

    added = 0
    skipped = 0
    for item in (req.items or [])[: max(1, min(int(req.limit or 6), 12))]:
        url = str(item.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        trust = int(item.get("trust_score") or 0)
        if trust < int(req.min_trust or 0):
            skipped += 1
            continue
        title = str(item.get("title") or "").strip()
        company = str(item.get("company") or "").strip()
        platform = str(item.get("platform") or platform_of(url)).strip()
        vac_text = f"{title or 'Вакансия'} в {company or 'компании'}. {item.get('description') or ''}".strip()
        match = pick_best_resume_variant(
            variants=variants,
            vacancy_title=title,
            vacancy_text=vac_text,
            base_resume=resume,
        )
        best_var = match["best"]
        variant_tag = f"{best_var.get('code', 'A')} · {best_var.get('title', '')}".strip(" ·")
        effective_resume = f"{resume}\n\nВыбранный акцент резюме ({best_var['label']}):\n{best_var['content']}"
        letter = await generate_cover_letter(effective_resume, vac_text, tone=tone, platform=platform or "HH.ru")
        _, created = apply_queue.add(
            user_id=req.user_id,
            url=url,
            title=title,
            company=company,
            platform=platform_of(url),
            letter=letter,
            resume_variant=variant_tag,
        )
        if created:
            added += 1
            if variants and best_var.get("slot"):
                personal_db.bump_variant_used(req.user_id, int(best_var["slot"]))
        else:
            skipped += 1

    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "message": f"В очередь добавлено: {added}" + (f" (пропущено: {skipped})" if skipped else ""),
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/stop")
async def api_apply_stop(req: ApplyUserReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    stopped = await apply_worker.stop()
    return {
        "ok": bool(stopped.get("ok")),
        "message": stopped.get("message") or stopped.get("error"),
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/retry")
async def api_apply_retry(req: ApplyTaskReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    task = apply_queue.get(req.task_id)
    if not task or int(task["user_id"]) != int(req.user_id):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    ok = apply_queue.retry(req.task_id)
    return {
        "ok": ok,
        "message": "Задача снова в очереди" if ok else "Эту задачу нельзя повторить",
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/cancel")
async def api_apply_cancel(req: ApplyTaskReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    task = apply_queue.get(req.task_id)
    if not task or int(task["user_id"]) != int(req.user_id):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    ok = apply_queue.cancel(req.task_id)
    return {
        "ok": ok,
        "message": "Задача отменена" if ok else "Задачу уже нельзя отменить",
        **_apply_payload(req.user_id),
    }


@app.post("/api/apply/clear")
async def api_apply_clear(req: ApplyUserReq, request: Request) -> Dict[str, Any]:
    ensure_user(request, req.user_id)
    removed = apply_queue.clear_finished(req.user_id)
    return {
        "ok": True,
        "message": f"Убрано задач: {removed}",
        **_apply_payload(req.user_id),
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
SHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(SHOT_DIR)), name="screenshots")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8085))
    # 0.0.0.0 — принимать подключения со всех интерфейсов. Обязательно для
    # хостинга (Railway и др.): там приложение живёт в контейнере и порт наружу
    # пробрасывается отдельно. Для локальной разработки можно задать HOST=127.0.0.1.
    host = os.environ.get("HOST", "0.0.0.0")
    bots = configured_bots()
    mini = webapp_url("personal")
    print(f"🕊 Веб-интерфейс и Mini App «Чайка Джонатан Ливингстон» запущен: http://{host}:{port}")
    if bots:
        print(f"   Telegram-боты: {', '.join(m for m, _ in bots)} | проверка initData включена")
    else:
        print("   Telegram-боты не заданы → гостевой вход (MINIAPP_DEV_FALLBACK)")
    print(f"   WebApp URL: {mini or 'не задан (нужен HTTPS-туннель, см. README)'}")
    uvicorn.run(app, host=host, port=port, log_level="info")
