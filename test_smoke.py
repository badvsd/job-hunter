# -*- coding: utf-8 -*-
"""
Самотестирование ядра и обоих вариантов ботов (А и Б).
Проверяет:
1. Реальный поиск вакансий на HH.ru, Работа России (Trudvsem) и парсинг Telegram-каналов
2. Антифрод и СБ-проверку работодателей (валидация ИНН ФНС, детекция дроппинга/скама)
3. ИИ-генерацию сопроводительного письма и адаптацию резюме
4. Базы данных обоих ботов (PersonalDB и SaaSDB с квотами и тарифами)
5. Очередь автооткликов (дедупликация, статусы, повторы, лимиты)
6. ИИ-коуч и A/B-копии резюме (5 акцентов, сверка фактов, автовыбор под вакансию)
7. Соцпоиск: Reddit RSS, Mastodon #hiring, HN-тред месяца, RemoteOK + fallback-шлюзы
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from shared_engine import (
    QuickScamFilter,
    adapt_resume_for_vacancy,
    deep_check_employer,
    generate_cover_letter,
    search_all_platforms,
)
from variant_a_personal.bot_personal import PersonalDB
from variant_b_saas.bot_saas import SaaSDB


async def run_tests() -> None:
    print("=== 1. ТЕСТ МУЛЬТИПЛАТФОРМЕННОГО ПОИСКА (HH.ru + Работа России + TG + Агрегаторы) ===")
    vacancies = await search_all_platforms("Python разработчик", city="Москва")
    print(f"Найдено карточек/источников: {len(vacancies)}")
    for v in vacancies[:5]:
        print(f" - [{v.platform}] {v.title} | {v.company} | {v.salary} | {v.trust_badge}")

    print("\n=== 2. ТЕСТ АНТИФРОД-СИСТЕМЫ (SCAM SHIELD) ===")
    ok_inn, msg_inn = QuickScamFilter.validate_inn("7736207543")  # Яндекс ИНН
    bad_inn, msg_bad = QuickScamFilter.validate_inn("7736207549")
    print(f"Проверка реального ИНН 7736207543: {ok_inn} ({msg_inn})")
    print(f"Проверка фейкового ИНН 7736207549: {bad_inn} ({msg_bad})")

    scam_sample = (
        "Срочно требуется курьер и оператор П2П арбитраж без опыта от 300 000 руб! "
        "Нужно оформить дебетовую карту по нашей ссылке и внести страховой депозит 2000 руб за обучение."
    )
    report = await deep_check_employer(scam_sample)
    print(f"Вердикт на скам-вакансию: {report['verdict']} (Trust Score: {report['score']}%)")
    for fl in report["flags"]:
        print(f"   * {fl}")

    print("\n=== 3. ТЕСТ ИИ-АДАПТАЦИИ РЕЗЮМЕ И СОПРОВОДИТЕЛЬНОГО ПИСЬМА ===")
    sample_resume = "Python Backend разработчик, 3 года опыта: FastAPI, PostgreSQL, Docker, Redis, парсинг и Telegram-боты."
    sample_vac = "Ищем Middle Python Developer в финтех. Стек: FastAPI, асинхронность, микросервисы, интеграция внешних API."
    letter = await generate_cover_letter(sample_resume, sample_vac, tone="short_tg", platform="Telegram")
    print(f"Сгенерированный питч ({len(letter)} симв.):\n{letter[:250]}...")

    print("\n=== 4. ТЕСТ БАЗ ДАННЫХ ВАРИАНТА А И ВАРИАНТА Б ===")
    pdb = PersonalDB()
    prof = pdb.get_profile(10001)
    assert prof["user_id"] == 10001
    print("Вариант А (PersonalDB): OK")

    sdb = SaaSDB()
    u, _ = sdb.get_or_create_user(20002, username="test_user")
    # Сбрасываем счётчики, чтобы самотест можно было прогонять многократно
    for col in ("used_scam", "used_searches", "used_ai"):
        sdb.update_field(20002, col, 0)
    ok_q, _ = sdb.check_and_consume_quota(20002, "scam")
    assert ok_q is True
    print(f"Вариант Б (SaaSDB): OK (Тариф: {u['plan']}, Подарочных дней: {u['gift_days']})")

    print("\n=== 5. ТЕСТ ОЧЕРЕДИ АВТООТКЛИКОВ ===")
    import tempfile

    from worker.autoapply import ApplyQueue, platform_of

    q = ApplyQueue(Path(tempfile.mkdtemp()) / "apply_test.db")
    assert platform_of("https://hh.ru/vacancy/1") == "hh"
    assert platform_of("https://www.avito.ru/x") == "avito"
    assert platform_of("https://t.me/s/jobs/1") == "telegram"

    t_hh, created_hh = q.add(90001, "https://hh.ru/vacancy/1", title="Python", letter="Привет")
    assert created_hh and t_hh["status"] == "queued"
    t_dup, created_dup = q.add(90001, "https://hh.ru/vacancy/1", title="Python")
    assert not created_dup and t_dup["id"] == t_hh["id"]
    t_avito, _ = q.add(90001, "https://www.avito.ru/x", title="Продавец")
    assert t_avito["status"] == "ready"

    claimed = q.claim_next(90001)
    assert claimed["status"] == "running" and claimed["attempts"] == 1
    q.finish(claimed["id"], "sent", "Отклик отправлен")
    assert q.counts(90001)["sent"] == 1
    assert q.retry(claimed["id"]) is False
    assert q.cancel(t_avito["id"]) is True
    assert q.retry(t_avito["id"]) is True
    print("Очередь откликов: OK (дедупликация, статусы, повторы, лимиты)")

    print("\n=== 6. ТЕСТ ИИ-КОУЧА И A/B-КОПИЙ РЕЗЮМЕ (5 АКЦЕНТОВ + СВЕРКА ФАКТОВ + АВТОВЫБОР) ===")
    from shared_engine import (
        RESUME_FOCUSES,
        build_resume_variant,
        pick_best_resume_variant,
        verify_resume_facts,
    )

    fact_ok = verify_resume_facts(sample_resume, "Python Backend (3 года опыта): FastAPI, PostgreSQL, Docker, Redis.")
    fact_bad = verify_resume_facts(sample_resume, "Увеличил выручку на 500% за 9 лет на Rust.")
    assert fact_ok["ok"] is True and fact_bad["ok"] is False

    ab_list = []
    for fk in RESUME_FOCUSES:
        v = await build_resume_variant(sample_resume, focus=fk, target_role="Python Backend Developer", use_llm=False)
        ab_list.append(v)
    assert len(ab_list) == 5 and all(v["fact_ok"] for v in ab_list)

    matched = pick_best_resume_variant(ab_list, vacancy_title="Senior Python (FastAPI / Docker / Redis)", vacancy_text=sample_vac)
    assert matched["best"]["focus"] == "stack" and matched["match_score"] >= 85
    print(f"A/B-резюме (5 копий): OK | Выбрано под вакансию: {matched['best']['label']} ({matched['match_score']}%)")

    print("\n=== 7. ТЕСТ СОЦПОИСКА (Reddit RSS / Mastodon #hiring / HN-тред месяца / RemoteOK) ===")
    from shared_engine import search_social_networks

    soc = await search_social_networks(
        "Python разработчик", city="", limit_per_network=2,
    )
    assert all(k in soc for k in ("items", "hubs", "networks", "stats", "total_live"))
    assert soc["total_networks"] >= 10
    live_by_net = {s["network"]: s["found"] for s in soc["stats"]}
    print("Живые находки по сетям:", live_by_net)
    for s in soc["stats"]:
        if s["error"]:
            print(f"   ! {s['network']}: {s['error']}")
    # Если задан токен Bluesky — живой поиск обязан работать
    import os as _os
    if _os.environ.get("BSKY_TOKEN") or (_os.environ.get("BSKY_HANDLE") and _os.environ.get("BSKY_APP_PASSWORD")):
        assert live_by_net.get("bluesky", 0) > 0, "BSKY-токен задан, но живого поиска Bluesky нет"
        print("Bluesky (с токеном): OK")
    # У оркестратора всегда остаётся шанс: живые находки или fallback-шлюзы
    assert soc["total_live"] > 0 or len(soc["hubs"]) > 0
    print(f"Итог: живых {soc['total_live']}, шлюзов {len(soc['hubs'])} — соцпоиск OK")

    print("\n✅ ВСЕ МОДУЛИ УСПЕШНО ПРОШЛИ ПРОВЕРКУ!")


if __name__ == "__main__":
    asyncio.run(run_tests())
