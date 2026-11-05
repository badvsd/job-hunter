# -*- coding: utf-8 -*-
"""Автопилот откликов: очередь задач и Playwright-воркер."""

from .autoapply import (
    AUTO_PLATFORMS,
    STATUSES,
    STATUS_LABELS,
    ApplyQueue,
    ApplyResult,
    ApplyWorker,
    answer_question_text,
    apply_task,
    is_transient_error,
    pick_radio_option,
    platform_of,
    queue,
    settings,
    worker,
)

__all__ = [
    "AUTO_PLATFORMS",
    "STATUSES",
    "STATUS_LABELS",
    "ApplyQueue",
    "ApplyResult",
    "ApplyWorker",
    "answer_question_text",
    "apply_task",
    "is_transient_error",
    "pick_radio_option",
    "platform_of",
    "queue",
    "settings",
    "worker",
]
