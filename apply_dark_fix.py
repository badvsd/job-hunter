# -*- coding: utf-8 -*-
"""Правка белого текста на белом в тёмной теме: webapp/static/index.html.
Запуск из папки job-hunter-bots:   python apply_dark_fix.py
Сначала делает резервную копию index.html.bak. Повторный запуск безопасен."""
import shutil, sys
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("webapp/static/index.html")
if not target.exists():
    sys.exit(f"Не найден файл: {target}. Запустите скрипт из папки job-hunter-bots или укажите путь аргументом.")

MARK = "/* dark-theme form fix */"
CSS = MARK + """
html{color-scheme:light}
html[data-theme="dark"]{color-scheme:dark}
:where(input,textarea,select){
  background-color:var(--surface-2);
  color:var(--ink);
  border-color:var(--line);
  caret-color:var(--clay);
}
:where(input,textarea)::placeholder{color:var(--ink-3);opacity:1}
select option{background:var(--surface);color:var(--ink)}
input:-webkit-autofill{
  -webkit-text-fill-color:var(--ink);
  box-shadow:0 0 0 1000px var(--surface) inset;
}
"""

src = target.read_text(encoding="utf-8")
changed = []
if MARK not in src:
    i = src.rfind("</style>")
    if i == -1:
        sys.exit("В файле нет </style> — правка не применена.")
    src = src[:i] + CSS + src[i:]
    changed.append("добавлен CSS для полей в тёмной теме")
if "color:var(--muted)" in src:
    n = src.count("color:var(--muted)")
    src = src.replace("color:var(--muted)", "color:var(--ink-2)")
    changed.append(f"заменено color:var(--muted) -> var(--ink-2): {n} шт.")
if not changed:
    sys.exit("Уже применено, менять нечего.")
shutil.copy2(target, target.with_suffix(".html.bak"))
target.write_text(src, encoding="utf-8")
print("Готово:", "; ".join(changed))
