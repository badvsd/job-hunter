# AGENT_SKILLS_MCP.md — План MCP-серверов и agent-скиллов (стиль NVIDIA AgentIQ / NeMo)

Статус: задача 3 — план (не реализация). Дата: ноябрь 2026.

Цель: превратить монолит «Джонатана» в набор **компонуемых функций** (AgentIQ-принцип) и
**портируемых скиллов** (NVIDIA Verified Skills), чтобы тот же код могли вызывать и Telegram-боты,
и Mini App, и внешние агенты (Claude Code, Codex, Cursor), и будущие MCP-клиенты.

---

## 0. Что такое «стиль NVIDIA AgentIQ / NeMo» (в двух абзацах)

**AgentIQ (NeMo Agent Toolkit)** — принцип: «каждый агент, инструмент и workflow — это вызов
функции». Всё компонуется из функций, совместимо с MCP, и обязательно покрыто профилированием
(OpenTelemetry), наблюдением и оценкой (RAGAS / trajectory / SWE-bench). Модуль памяти отдельно.

**NVIDIA Verified Skills** — портируемые инструкции (папка с `SKILL.md`) + карточка скилла
(владелец, зависимости, ограничения, статус верификации). Конвейер верификации:
`scan (SkillSpector) → evaluate (SkillEvaluator, 3 уровня) → skill card → sign → catalog → sync`.
Оценка пишется в `BENCHMARK.md` по 5 измерениям: security, correctness, discoverability,
effectiveness, efficiency (baseline → с скиллом).

**Вывод для нас:** не надо тащить сам фреймворк AgentIQ. Нужно перенять три вещи:
1) всё — функция с чёткой схемой входа/выхода (у нас уже так);
2) MCP-обёртка над этими функциями;
3) скиллы как папки с `SKILL.md`, с карточкой и (позже) оценкой.

---

## 1. Текущее состояние = готовый фундамент

`webapp/server.py` (FastAPI) уже отдаёт те же функции, что и боты. То есть **HTTP-«MCP» у нас уже
есть**, просто без стандарта. MCP-слой — это стандартизация того же, а не новая логика.

| Существующая функция (engine.py / social_search.py) | Уже отдаётся по эндпоинту |
|---|---|
| `search_hh` / `search_trudvsem` / `search_telegram_channels` / `search_all_platforms` | `POST /api/search` |
| `deep_check_employer` (антифрод, Trust Score) | `POST /api/scam-check` |
| `adapt_resume_for_vacancy` / `build_resume_variant` / `pick_best_resume_variant` | `/api/adapt-resume`, `/api/resumes/*` |
| `generate_cover_letter` | `POST /api/cover-letter` |
| `search_social_networks` (7 живых парсеров + 10 deep-link шлюзов) | `POST /api/social/search` |
| воркер `autoapply.py` (очередь откликов) | `/api/apply/*` |
| `verify_resume_facts` / `extract_text_from_pdf_bytes` | внутри `/api/resumes/generate` |

Итог: MCP-инструменты — это 1:1 обёртки над существующими функциями. Усилия — минимальные,
ценность — агенты (Claude/Cursor/Codex/внешние) получают доступ к поиску и антифроду «Джонатана».

---

## 2. План MCP-серверов

Разбиваем на 5 MCP-серверов по доменам (не один монолит — чтобы каждый можно было включать
отдельно, как в AgentIQ «tool → function»).

### 2.1 `job-search-mcp` — поиск
Инструменты (1:1 к функциям):
- `search_hh(query, city, min_salary, limit)`
- `search_trudvsem(query, limit)`
- `search_telegram_channels(query, city, limit)`
- `search_reddit(query, limit)` · `search_bluesky(query, limit)` · `search_mastodon(query, limit)`
- `search_hn_hiring(query, limit)` · `search_remoteok(query, limit)` · `search_discord_servers(query, limit)`
- `search_all_platforms(query, city, min_salary)` — агрегат
- `search_social_networks(query, city, networks)` — живой + шлюзы
- `generate_crossplatform_search_hubs(query, city)` — deep-link шлюзы

Выход: список `Vacancy` (title, company, city, salary, url, source, trust_hint).
Вход-нюансы: Bluesky требует `BSKY_TOKEN`/`BSKY_HANDLE`+`BSKY_APP_PASSWORD` (сервер отдаёт
deep-link шлюз, если токена нет). Reddit троттлит 429 — сохранить повторную попытку.

### 2.2 `antifraud-mcp` — проверка работодателя (легальный OSINT)
- `deep_check_employer(query_or_inn_or_text)` → Trust Score 0–100 + маркеры скама + ссылки на
  ЕГРЮЛ / КАД / ФССП / отзовики.
- `quick_scam_filter(text)` → предварительный фильтр (регулярки, 15+ схем).
- **Новые (приоритет по RESEARCH.md):**
  - `ghost_job_score(vacancy)` — «живая ли вакансия» (дата публикации, дубли, история просмотров,
    признак «вечно висит»). УТП, которого нет ни у одного конкурента.
  - `employer_public_records(inn)` — агрегация публичных реестров: ФНС/ЕГРЮЛ (массовый адрес,
    массовый руководитель, «однодневка»), ФССП (исполнительные производства), КАД (арбитраж),
    госконтракты, Роструд (долги по зарплате).

Граница (зафиксировано в RESEARCH.md §6): только публичные реестры. Никаких слитых баз, никакого
доксинга «подчинённых».

### 2.3 `resume-mcp` — резюме и письма
- `adapt_resume_for_vacancy(base_resume, vacancy_info, mode)` — `ats` / `audit`
- `generate_cover_letter(base_resume, vacancy_info, tone)` — `natural` / `pro` / `short_tg`
- `build_resume_variant(base_resume, focus, facts)` — 5 A/B-копий
- `pick_best_resume_variant(variants, vacancy_info)` — автовыбор
- `verify_resume_facts(base_resume, variants)` — сверка фактов (антигаллюцинации)
- `extract_text_from_pdf_bytes(pdf_bytes)`

### 2.4 `apply-mcp` — автопилот откликов
Обёртки над `worker/autoapply.py`:
- `apply_enqueue(vacancy, letter)` · `apply_batch_enqueue(vacancies)`
- `apply_queue_state()` · `apply_run()` · `apply_stop()` · `apply_retry()` · `apply_cancel()`
- Ограничения встраиваем в описание инструмента (не в скрытую логику): `APPLY_DAILY_LIMIT`,
  случайные паузы, dry-run, стоп на капче — всё это становится «контрактом» инструмента.

### 2.5 `memory-mcp` — память агента (AgentIQ Memory Module)
- `get_profile(user_id)` · `save_profile(user_id, fields)` — профессия, город, зарплата, каналы,
  настройки автопилота.
- `get_quotas(user_id)` / `consume_quota(user_id, kind)` — для SaaS-режима (Вариант Б).
- Реализация — лёгкий KV (текущие базы ботов) + опционально векторный retriever для истории.

### Транспорт и деплой MCP
- Прототип: **FastMCP** (официальный Python MCP SDK) — `stdio` для локальных агентов + опционально
  HTTP/SSE для серверного деплоя.
- Совместимость с AgentIQ: инструменты MCP становятся AgentIQ-функциями автоматически
  (MCP Compatibility — заявлено в доках AgentIQ).
- Каждый сервер = отдельный `pyproject`-модуль в `mcp_servers/`, без дублирования логики: он
  **импортирует** `shared_engine.*`, не копирует.

---

## 3. План agent-скиллов (SKILL.md, стиль NVIDIA)

Живут в `.agents/skills/` (канон, как в NeMo Gym) + симлинки в `.claude/skills/`, `.codex/skills/`
для нативных путей обнаружения. Корневой `AGENTS.md` — точка входа с таблицей маршрутизации.

Формат каждой папки скилла:
```
.agents/skills/<skill>/
├── SKILL.md        # инструкция: когда включать, шаги, какие MCP-инструменты вызывает
├── skill-card.md   # владелец, зависимости, ограничения, статус верификации
└── BENCHMARK.md    # (позже) 5 измерений, baseline → с скиллом
```

### Скиллы (8), отсортированы по ценности из RESEARCH.md

| Скилл | Когда включается | Вызывает MCP-инструменты | Приоритет |
|---|---|---|---|
| `ghost-job-score` | «живая ли вакансия», «не фейк ли» | `antifraud.ghost_job_score`, `antifraud.deep_check_employer` | P0 (УТП) |
| `employer-security-check` | «пробить компанию», «не мошенник ли» | `antifraud.quick_scam_filter`, `antifraud.employer_public_records` | P0 (ядро) |
| `resume-ats-optimization` | «резюме не проходит», «адаптируй под вакансию» | `resume.adapt_resume_for_vacancy(ats)`, `resume.verify_resume_facts` | P1 |
| `cover-letter-craft` | «напиши письмо» | `resume.generate_cover_letter` (3 тона) | P1 |
| `fast-money-track` | «подработка/смена/вахта сейчас» | `job-search.search_trudvsem`, `search_telegram_channels`, `search_all_platforms` (фильтр по графику) | P1 (сегмент «нет денег») |
| `rejection-feedback` | «почему отказ», «что исправить» | `resume.adapt_resume_for_vacancy(audit)` + LLM-разбор | P1 |
| `autoapply-guardrails` | «отправь отклик за меня» | `apply.*` (с контрактом лимитов/антибана) | P1 |
| `salary-benchmark` | «сколько просить» | `job-search.*` + агрегация зарплатных вилок | P2 |

### Пример структуры SKILL.md (для `employer-security-check`)

```markdown
---
name: employer-security-check
description: Проверка работодателя по публичным реестрам перед откликом.
  Использовать, когда соискатель просит «пробить компанию», «проверить ИНН»,
  «не мошенник ли». Только легальные публичные источники.
---

# Проверка работодателя (легальный OSINT)

## Шаги
1. `antifraud.quick_scam_filter` — быстрый отсев по тексту вакансии (15+ схем).
2. Если есть ИНН — `antifraud.employer_public_records` (ЕГРЮЛ, ФССП, КАД, госконтракты, Роструд).
3. `antifraud.deep_check_employer` — Trust Score 0–100 + ссылки на реестры.
4. Отдать пользователю: оценка, маркеры, ссылки. Никогда не отдавать чужие персональные данные.

## Запрещено
- Слитые базы, данные «подчинённых», доксинг. Только публичные реестры.
```

---

## 4. Карточка скилла и конвейер верификации (NVIDIA-стиль)

`skill-card.md` каждой папки заполняется полями NVIDIA Verified Skills:
- **owner** — кто ведёт скилл (проект «Джонатан»);
- **dependencies** — какие MCP-инструменты нужны (напр. `antifraud-mcp`);
- **limitations** — что скилл не умеет (напр. «не проверяет юридическую чистоту физлиц»);
- **verification** — статус: `draft` → `scanned` → `evaluated` → `signed`.

Оценка (SkillEvaluator, когда понадобится):
- **Tier 1** — валидация схемы, лицензии, PII, сканер SkillSpector;
- **Tier 2** — семантическая дедупликация против каталога;
- **Tier 3** — живая оценка: агент делает задачу с/без скилла, разница пишется в `BENCHMARK.md`
  (5 измерений). PASS = все настроенные измерения прошли хотя бы у одного агента.

Для старта достаточно Tier 1 + ручной прогон `test_smoke.py` как аналог «task set». Полный
SkillEvaluator — опционально на этапе, когда скиллы станут публичным артефактом.

---

## 5. Дорожная карта (фазы)

| Фаза | Что | Трудоёмкость | Риск |
|---|---|---|---|
| 0 | Оставить FastAPI как есть (уже «MCP-шейп») | 0 | — |
| 1 | `mcp_servers/` + FastMCP-обёртки над `shared_engine.*` (5 серверов, импорт, не копия) | низкая | низкий |
| 2 | `.agents/skills/` + 8 SKILL.md + корневой `AGENTS.md` (маршрутизация) | низкая | низкий |
| 3 | ghost-job score + employer_public_records (новые инструменты антифрода) | средняя | средний (нужны источники реестров) |
| 4 | `skill-card.md` + Tier-1 валидация + прогон `test_smoke.py` как таск-сет | низкая | низкий |
| 5 | (опц.) SkillEvaluator Tier 2–3, подпись, каталог | высокая | — |

**Ближайший шаг без переписывания:** фаза 1 (обёртки) + фаза 2 (скиллы) — они не трогают боевой
код и дают агентам доступ к уже работающему поиску/антифроду.

---

## 6. Связь с выводами исследования

- P0-скиллы (`ghost-job-score`, `employer-security-check`) = два из трёх УТП «Джонатана»
  (RESEARCH.md, задача 2). Именно их нет ни у одного из 8 конкурентов.
- `fast-money-track` = трек подработок/смен/вахты (сегмент «нет денег», 50% аудитории).
- `rejection-feedback` + `salary-benchmark` = закрывают боли №2 и №5 из голоса соискателей.
- MCP-слой делает это доступным не только боту, но и внешним агентам → канал дистрибуции
  (Claude/Cursor/Codex-пользователи), которого нет у конкурентов-закрытых SaaS.
