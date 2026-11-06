# -*- coding: utf-8 -*-
"""
Единая точка запуска для хостинга (Railway / Docker).

Запускает ОДНОВРЕМЕННО два процесса:
  1. webapp/server.py           — сайт + Mini App (слушает порт из переменной PORT).
  2. variant_a_personal/bot_personal.py — Telegram-бот (long polling).

Зачем это нужно: на хостинге приложение живёт в одном контейнере, а сайт и бот —
это две отдельные программы. Раньше запускался только сайт, поэтому бот молчал.
Теперь оба стартуют вместе.

Бот запускается только если задан токен (PERSONAL_BOT_TOKEN или BOT_TOKEN).
Без токена работает только сайт (гостевой режим) — это не ошибка.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time


def _token() -> str:
    return (os.environ.get("PERSONAL_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or "").strip()


def _child_env() -> dict[str, str]:
    """Окружение для дочерних процессов.

    Форсируем UTF-8: на Windows консоль по умолчанию использует cp1251 и падает
    на эмодзи/кириллице в print(). На Linux (Railway) UTF-8 и так по умолчанию,
    поэтому эти переменные ничего не ломают.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> None:
    procs: list[subprocess.Popen] = []

    # 1. Сайт + Mini App (обязательно, слушает порт хостинга).
    procs.append(subprocess.Popen([sys.executable, "webapp/server.py"], env=_child_env()))

    # 2. Telegram-бот (необязательно: нужен токен).
    if _token():
        procs.append(subprocess.Popen([sys.executable, "variant_a_personal/bot_personal.py"], env=_child_env()))
    else:
        print("[run_all] Токен бота не задан → запускаю только сайт (бот не нужен).")

    # Держим контейнер живым, пока работают процессы.
    while True:
        for p in list(procs):
            code = p.poll()
            if code is not None:
                # Процесс упал — перезапускаем его через паузу (автовосстановление).
                print(f"[run_all] Процесс {p.args} завершился с кодом {code}. Перезапускаю...")
                procs.remove(p)
                try:
                    procs.append(subprocess.Popen(p.args, env=_child_env()))
                except Exception as exc:  # noqa: BLE001
                    print(f"[run_all] Не удалось перезапустить: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    main()
