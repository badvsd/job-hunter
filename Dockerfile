# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile для «Джонатана Ливингстона»
#
# Собирает контейнер с Python + Chromium (Playwright). Chromium нужен, чтобы
# автопилот откликов работал не только на твоём компьютере, но и на хостинге.
#
# На Railway этот файл подхватывается автоматически: если в проекте есть
# Dockerfile, Railway соберёт по нему (см. railway.json, builder = DOCKERFILE).
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Не задавать вопросов при установке пакетов и не буферизовать вывод логов.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Сначала копируем только список зависимостей — так Docker закэширует
#    этот слой и не будет переустанавливать библиотеки при каждом изменении кода.
COPY requirements.txt .

# 2. Ставим библиотеки Python + Chromium со всеми системными зависимостями.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# 3. Копируем сам код приложения.
COPY . .

# 4. Настройки по умолчанию (можно переопределить переменными окружения):
#    - DATA_DIR=/data          — куда класть базы (сюда Railway монтирует Volume);
#    - APPLY_HEADLESS=1        — браузер без окна (на хостинге нет экрана);
#    - APPLY_BROWSER_CHANNEL=  — пусто = использовать встроенный Chromium Playwright.
ENV PORT=8085 \
    HOST=0.0.0.0 \
    DATA_DIR=/data \
    APPLY_HEADLESS=1 \
    APPLY_BROWSER_CHANNEL=""

# 5. Папка данных: на Railway подключи Volume (Settings → Volumes) с Mount Path /data.
#    Инструкция VOLUME в Dockerfile Railway не поддерживает.

EXPOSE 8085

# Запускаем лаунчер: он поднимает и сайт (Mini App), и Telegram-бота вместе.
CMD ["python", "run_all.py"]
