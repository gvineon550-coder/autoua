"""
Применяет к плейлистам:
  1. Дедупликацию (удаляет дубликаты URL в пределах одной группы).
  2. Заголовки #EXTVLCOPT: User-Agent + (опционально) Referer/Origin из headers.json.

Вызывается из detect_headers.py в конце пайплайна, но может запускаться и отдельно:
    python add_user_agent.py
"""

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

# ------------------------------------------------------------------
# Константы
# ------------------------------------------------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)
DEFAULT_UA_LINE = f"#EXTVLCOPT:http-user-agent={DEFAULT_UA}\n"

URL_PATTERN = re.compile(r'^https?://')
GROUP_PATTERN = re.compile(r'group-title="([^"]*)"')

HEADERS_FILE = Path('headers.json')
DEAD_ARCHIVE = Path('dead.m3u')


# ------------------------------------------------------------------
# Чтение конфига
# ------------------------------------------------------------------
def load_headers() -> dict:
    """Читает headers.json. Если файла нет или он битый — возвращает {}."""
    if not HEADERS_FILE.exists():
        return {}
    try:
        with open(HEADERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        print(f"⚠️ headers.json: ошибка JSON ({e}), применяю только UA")
        return {}


# ------------------------------------------------------------------
# Дедупликация
# ------------------------------------------------------------------
def extract_group_title(extinf_line: str) -> str:
    m = GROUP_PATTERN.search(extinf_line)
    return m.group(1) if m else ""


def dedupe_playlist(file_path: Path) -> int:
    """
    Удаляет дубликаты URL в пределах одной группы (group-title).
    Одинаковый URL в РАЗНЫХ группах считается мультикатегорией и остаётся.
    Возвращает количество удалённых блоков.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    seen = set()             # ключи (group_title, url)
    current_group = ""
    removed = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('#EXTINF'):
            current_group = extract_group_title(stripped)
            new_lines.append(line)
            i += 1
            continue

        if stripped.startswith('#EXTVLCOPT'):
            new_lines.append(line)
            i += 1
            continue

        if URL_PATTERN.match(stripped):
            key = (current_group, stripped)
            if key in seen:
                # Откат назад: удаляем EXTVLCOPT и EXTINF этого блока
                while new_lines and new_lines[-1].startswith('#EXTVLCOPT'):
                    new_lines.pop()
                if new_lines and new_lines[-1].startswith('#EXTINF'):
                    new_lines.pop()
                removed += 1
                i += 1
                continue
            seen.add(key)
            new_lines.append(line)
            i += 1
            continue

        new_lines.append(line)
        i += 1

    if removed:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        print(f"🧹 {file_path}: удалено дубликатов {removed}")
    return removed


# ------------------------------------------------------------------
# Вставка заголовков
# ------------------------------------------------------------------
def build_header_lines(url: str, headers_cfg: dict) -> list[str]:
    """
    Возвращает список строк #EXTVLCOPT для одного URL:
      - Всегда User-Agent (из headers.json если указан, иначе дефолтный Chrome).
      - Опционально Referer/Origin, если найдены для домена.
    """
    lines = []
    host = urlparse(url).netloc.lower()
    cfg = headers_cfg.get(host) or {}

    # UA: либо переопределён для домена, либо дефолтный
    ua = cfg.get("user_agent") or DEFAULT_UA
    lines.append(f"#EXTVLCOPT:http-user-agent={ua}\n")

    # Referer / Origin
    if cfg.get("referer"):
        lines.append(f"#EXTVLCOPT:http-referrer={cfg['referer']}\n")
    if cfg.get("origin"):
        lines.append(f"#EXTVLCOPT:http-origin={cfg['origin']}\n")

    return lines


def process_file(file_path: Path, headers_cfg: dict) -> int:
    """Перезаписывает заголовки для всех URL в файле. Возвращает число URL."""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    inserted = 0

    for line in lines:
        stripped = line.strip()
        if URL_PATTERN.match(stripped):
            # Убираем старые EXTVLCOPT, чтобы не дублировались
            while new_lines and new_lines[-1].startswith('#EXTVLCOPT:'):
                new_lines.pop()
            new_lines.extend(build_header_lines(stripped, headers_cfg))
            inserted += 1
        new_lines.append(line)

    new_content = ''.join(new_lines)
    old_content = ''.join(lines)
    if new_content != old_content:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"✅ {file_path} ({inserted} URL)")
    return inserted


# ------------------------------------------------------------------
# Точка входа
# ------------------------------------------------------------------
def main():
    headers_cfg = load_headers()

    # Бэкап headers.json перед любой возможной порчей (нужен только при отладке)
    if HEADERS_FILE.exists():
        shutil.copy(HEADERS_FILE, HEADERS_FILE.with_suffix('.json.bak'))

    for ext in ('*.m3u', '*.m3u8'):
        for fp in Path('.').rglob(ext):
            if fp.name == DEAD_ARCHIVE.name:
                continue
            dedupe_playlist(fp)
            process_file(fp, headers_cfg)


if __name__ == '__main__':
    main()
