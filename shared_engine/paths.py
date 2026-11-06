# -*- coding: utf-8 -*-
"""
Единое место, куда программа складывает свои файлы данных
(базы SQLite, скриншоты автопилота, профиль браузера).

Зачем это нужно:
 - По умолчанию всё хранится в папке `data/` рядом с проектом.
 - Через переменную окружения `DATA_DIR` можно перенести данные в любую папку.
   На хостинге (Railway) к контейнеру подключают «диск» (Volume) и монтируют
   его, например, в `/data`. Тогда данные не стираются при перезапуске.

Переменная `DATA_DIR` читается каждый раз заново (не кэшируется), поэтому
можно менять её между запусками без правки кода.
"""

from __future__ import annotations

import os
from pathlib import Path

# Корень проекта (папка job-hunter-bots): shared_engine/ -> ../
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Папка данных. Создаёт её, если её ещё нет. Возвращает путь."""
    raw = (os.environ.get("DATA_DIR") or "").strip()
    d = Path(raw) if raw else (PROJECT_ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_path(filename: str) -> Path:
    """Полный путь к файлу данных с именем `filename` внутри папки данных."""
    return data_dir() / filename
