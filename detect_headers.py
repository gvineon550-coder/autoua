"""
Полный пайплайн автообновления плейлистов:

  0. Дедупликация.
  1. Проверка живости URL (async + Semaphore на домен).
  2. Валидация HLS: ответ должен содержать #EXTM3U (иначе 200 — фикция).
  3. Удаление мёртвых (streak >= MIN_DEAD_STREAK прогонов подряд) + архив в dead.m3u.
  4. Подбор Referer/Origin/UA для новых доменов (QUICK) или всех (FULL).
  5. Применение заголовков к плейлистам.

Режимы:
  QUICK (FULL_CHECK=false) — по умолчанию, при пуше. Только новые URL + новые домены.
  FULL  (FULL_CHECK=true)  — при schedule и workflow_dispatch. Все URL и домены.

Переменные окружения:
  FULL_CHECK     — "true" / "false"
  MIN_DEAD_STREAK — сколько прогонов подряд URL должен быть мёртв для удаления (по умолчанию 2)
"""

import asyncio
import json
import os
import random
import shutil
import re
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
from curl_cffi.requests import AsyncSession

import add_user_agent

# ------------------------------------------------------------------
# Настройки
# ------------------------------------------------------------------
FULL_CHECK = os.environ.get("FULL_CHECK", "false").lower() == "true"
MIN_DEAD_STREAK = int(os.environ.get("MIN_DEAD_STREAK", "2"))

HEADERS_FILE = Path('headers.json')
ALIVE_FILE = Path('alive.json')
CACHE_FILE = Path('alive_cache.json')
DEAD_ARCHIVE = Path('dead.m3u')
URL_PATTERN = re.compile(r'^https?://')

GLOBAL_CONCURRENCY = 64      # одновременных запросов суммарно
PER_DOMAIN_LIMIT = 4         # одновременных запросов на ОДИН домен (вежливость к CDN)
TIMEOUT_QUICK = 6
TIMEOUT_DETECT = 10

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0", "edge101"]
VLC_UA = "VLC/3.0.18 LibVLC/3.0.18"


# ------------------------------------------------------------------
# Семафоры
# ------------------------------------------------------------------
class DomainLimiter:
    """Отдельный семафор на каждый домен. Не даёт CDN получить больше PER_DOMAIN_LIMIT запросов."""
    def __init__(self, per_domain: int):
        self.per_domain = per_domain
        self._sems: dict[str, asyncio.Semaphore] = {}

    def get(self, host: str) -> asyncio.Semaphore:
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self.per_domain)
        return self._sems[host]


async def jitter():
    """Небольшая случайная пауза между запросами — чтобы не палиться как бот."""
    await asyncio.sleep(random.uniform(0.05, 0.25))


# ------------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------------
def collect_urls():
    """Все URL из всех плейлистов + по одному примеру URL на каждый домен."""
    all_urls, samples = [], {}
    for ext in ('*.m3u', '*.m3u8'):
        for fp in Path('.').rglob(ext):
            if fp.name == DEAD_ARCHIVE.name:
                continue
            with open(fp, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if URL_PATTERN.match(s):
                        all_urls.append(s)
                        host = urlparse(s).netloc.lower()
                        if host and host not in samples:
                            samples[host] = s
    return all_urls, samples


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_json(path: Path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# Retry для транзиентных ошибок
# ------------------------------------------------------------------
async def fetch_with_retry(session, method: str, url: str, attempts: int = 3, **kwargs):
    """Три попытки с экспоненциальной паузой (0.5 → 1 → 2 сек)."""
    for attempt in range(attempts):
        try:
            return await getattr(session, method)(url, **kwargs)
        except Exception:
            if attempt == attempts - 1:
                return None
            await asyncio.sleep(0.5 * (2 ** attempt))
    return None


# ------------------------------------------------------------------
# Проверка живости одного URL
# ------------------------------------------------------------------
async def quick_alive_one(session, url, global_sem, limiter):
    """
    Возвращает (url, status), где status ∈ {alive, dead, unknown}.
    - alive: 200/206 И в ответе есть #EXTM3U, ИЛИ 401/403 (работает, но нужны заголовки).
    - dead: 404/410.
    - unknown: сеть/таймаут/200 без манифеста.
    """
    host = urlparse(url).netloc.lower()
    async with global_sem, limiter.get(host):
        await jitter()

        # HEAD — быстрый способ узнать код без тела
        r = await fetch_with_retry(session, "head", url,
                                   impersonate=random.choice(IMPERSONATIONS),
                                   timeout=TIMEOUT_QUICK, allow_redirects=True)
        if r is not None:
            code = r.status_code
            if code in (401, 403):
                return url, "alive"
            if code in (404, 410):
                return url, "dead"
            if code in (200, 206):
                # 200 от HEAD — мало, нужно проверить тело, что это манифест
                r2 = await fetch_with_retry(session, "get", url,
                                            impersonate=random.choice(IMPERSONATIONS),
                                            headers={"Range": "bytes=0-2047"},
                                            timeout=TIMEOUT_QUICK, allow_redirects=True)
                if r2 is not None and r2.status_code in (200, 206) and '#EXTM3U' in r2.text[:256]:
                    return url, "alive"
                return url, "unknown"

        # Fallback GET (некоторые CDN не любят HEAD)
        r = await fetch_with_retry(session, "get", url,
                                   impersonate=random.choice(IMPERSONATIONS),
                                   headers={"Range": "bytes=0-2047"},
                                   timeout=TIMEOUT_QUICK, allow_redirects=True)
        if r is not None:
            code = r.status_code
            if code in (401, 403):
                return url, "alive"
            if code in (404, 410):
                return url, "dead"
            if code in (200, 206) and '#EXTM3U' in r.text[:256]:
                return url, "alive"

        return url, "unknown"


async def check_alive_batch(urls) -> dict[str, str]:
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [quick_alive_one(session, u, global_sem, limiter) for u in urls]
        for coro in asyncio.as_completed(tasks):
            url, status = await coro
            results[url] = status
    return results


# ------------------------------------------------------------------
# Подбор заголовков для домена
# ------------------------------------------------------------------
async def detect_one_host(session, host, url, limiter, global_sem):
    """
    Пробует комбинации заголовков, чтобы найти рабочую для этого домена.
    Возвращает (host, dict | None), где dict — {'referer':..., 'origin':..., 'user_agent':...}.
    """
    async with global_sem, limiter.get(host):
        await jitter()

        # Baseline: только браузерный UA (impersonate).
        r = await fetch_with_retry(session, "get", url,
                                   impersonate=random.choice(IMPERSONATIONS),
                                   timeout=TIMEOUT_DETECT)
        if r is None:
            return host, None
        baseline = r.status_code
        if baseline in (200, 404, 410):
            return host, None  # работает без хитростей, или мёртв — не наша задача
        # Если baseline 403/401 — будем подбирать

        root = f"https://{host}/"
        parts = host.split('.')
        parent = f"https://{'.'.join(parts[-2:])}/" if len(parts) >= 2 else root

        candidates = [
            ("ref-host",    {"Referer": root}),
            ("ref-parent",  {"Referer": parent}),
            ("both-host",   {"Referer": root, "Origin": root.rstrip('/')}),
            ("both-parent", {"Referer": parent, "Origin": parent.rstrip('/')}),
        ]

        for name, hdrs in candidates:
            async with limiter.get(host):
                await jitter()
                r = await fetch_with_retry(session, "get", url,
                                           impersonate=random.choice(IMPERSONATIONS),
                                           headers=hdrs, timeout=TIMEOUT_DETECT)
                if r is not None and r.status_code == 200:
                    result = {}
                    if "Referer" in hdrs:
                        result["referer"] = hdrs["Referer"]
                    if "Origin" in hdrs:
                        result["origin"] = hdrs["Origin"]
                    return host, result

        # Финальный fallback: VLC UA (иногда помогает, когда браузерный UA забанен)
        async with limiter.get(host):
            await jitter()
            try:
                r = await session.get(url,
                                      headers={"User-Agent": VLC_UA},
                                      impersonate=None,
                                      timeout=TIMEOUT_DETECT)
                if r.status_code == 200:
                    return host, {"user_agent": VLC_UA}
            except Exception:
                pass

        return host, None


async def detect_headers_batch(samples: dict[str, str]) -> dict[str, dict]:
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [detect_one_host(session, h, u, limiter, global_sem) for h, u in samples.items()]
        for coro in asyncio.as_completed(tasks):
            host, cfg = await coro
            if cfg:
                results[host] = cfg
                print(f"🔍 {host} → {cfg}")
    return results


# ------------------------------------------------------------------
# Удаление мёртвых ссылок + архив
# ------------------------------------------------------------------
def remove_dead(file_path: Path, dead_set: set) -> list:
    """Удаляет блок (EXTINF + EXTVLCOPT + URL) для мёртвых URL. Возвращает архив [(extinf, url), ...]."""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines, archived, i = [], [], 0
    while i < len(lines):
        stripped = lines[i].strip()
        if URL_PATTERN.match(stripped) and stripped in dead_set:
            while new_lines and new_lines[-1].startswith('#EXTVLCOPT:'):
                new_lines.pop()
            extinf = None
            if new_lines and new_lines[-1].startswith('#EXTINF'):
                extinf = new_lines.pop()
            archived.append((extinf, lines[i]))
            i += 1
            continue
        new_lines.append(lines[i])
        i += 1

    if archived:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    return archived


def append_archive(archived_all) -> int:
    """Дописывает в dead.m3u только те URL, которых там ещё нет."""
    if not archived_all:
        return 0

    existing = set()
    if DEAD_ARCHIVE.exists():
        with open(DEAD_ARCHIVE, 'r', encoding='utf-8') as f:
            for line in f:
                if URL_PATTERN.match(line.strip()):
                    existing.add(line.strip())

    new_entries = [(e, u) for e, u in archived_all if u.strip() not in existing]
    if not new_entries:
        return 0

    mode = 'a' if DEAD_ARCHIVE.exists() else 'w'
    with open(DEAD_ARCHIVE, mode, encoding='utf-8') as f:
        if mode == 'w':
            f.write('#EXTM3U\n# Архив мёртвых ссылок (удалены автоматически, streak >= MIN_DEAD_STREAK)\n\n')
        for extinf, url in new_entries:
            f.write((extinf or '#EXTINF:-1 group-title="Архив", Удалено\n').rstrip('\n') + '\n')
            f.write(url.rstrip('\n') + '\n\n')
    return len(new_entries)


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def main():
    # --- 0. Дедуп до любых проверок (чтобы не проверять одно и то же дважды) ---
    print("🧹 Дедупликация...")
    for ext in ('*.m3u', '*.m3u8'):
        for fp in Path('.').rglob(ext):
            if fp.name == DEAD_ARCHIVE.name:
                continue
            add_user_agent.dedupe_playlist(fp)

    all_urls, samples = collect_urls()
    cache = load_cache()
    now_iso = datetime.now(timezone.utc).isoformat()
    all_url_set = set(all_urls)

    print(f"\n📄 URL: {len(all_urls)}, доменов: {len(samples)}")
    print(f"📦 Кэш: {len(cache)}")
    print(f"🔧 Режим: {'FULL' if FULL_CHECK else 'QUICK (только новые)'}")

    # --- 1. Проверка живости ---
    urls_to_check = [u for u in all_urls if FULL_CHECK or u not in cache]
    print(f"🩺 К проверке: {len(urls_to_check)}")

    if urls_to_check:
        results = asyncio.run(check_alive_batch(urls_to_check))
        for url, status in results.items():
            entry = cache.get(url, {})
            prev = entry.get("status")
            if status == "dead":
                streak = entry.get("dead_streak", 0) + 1 if prev == "dead" else 1
                cache[url] = {"status": "dead", "checked_at": now_iso, "dead_streak": streak}
            elif status == "alive":
                cache[url] = {"status": "alive", "checked_at": now_iso, "dead_streak": 0}
            else:
                cache[url] = {"status": "unknown", "checked_at": now_iso,
                              "dead_streak": entry.get("dead_streak", 0)}

    # --- 2. Чистим кэш от URL, которых больше нет в плейлистах ---
    orphans = [u for u in cache if u not in all_url_set]
    for u in orphans:
        del cache[u]
    if orphans:
        print(f"🧹 Из кэша убрано: {len(orphans)}")

    # --- 3. Удаление мёртвых (только в FULL, только при streak >= MIN_DEAD_STREAK) ---
    to_remove = set()
    if FULL_CHECK:
        for url in all_urls:
            e = cache.get(url)
            if e and e.get("status") == "dead" and e.get("dead_streak", 0) >= MIN_DEAD_STREAK:
                to_remove.add(url)

    if to_remove:
        print(f"\n🗑️ Удаление {len(to_remove)} (мертвы {MIN_DEAD_STREAK}+ прогона)...")
        archived_all = []
        for ext in ('*.m3u', '*.m3u8'):
            for fp in Path('.').rglob(ext):
                if fp.name == DEAD_ARCHIVE.name:
                    continue
                arch = remove_dead(fp, to_remove)
                if arch:
                    print(f"   {fp}: -{len(arch)}")
                    archived_all.extend(arch)
        if archived_all:
            print(f"📦 dead.m3u: +{append_archive(archived_all)}")

    # --- 4. Подбор заголовков ---
    existing_headers = {}
    if HEADERS_FILE.exists():
        try:
            with open(HEADERS_FILE, 'r', encoding='utf-8') as f:
                existing_headers = json.load(f)
        except json.JSONDecodeError:
            pass

    if FULL_CHECK:
        detect_domains = list(samples.keys())
    else:
        detect_domains = [h for h in samples if h not in existing_headers]

    if detect_domains:
        print(f"\n🔍 Подбор заголовков для {len(detect_domains)} домен(ов)...")
        new_results = asyncio.run(detect_headers_batch({h: samples[h] for h in detect_domains}))
        if FULL_CHECK:
            final = {h: v for h, v in new_results.items() if h in samples}
        else:
            existing_headers.update(new_results)
            final = {h: v for h, v in existing_headers.items() if h in samples}

        # Бэкап перед перезаписью (полезно при отладке)
        if HEADERS_FILE.exists():
            shutil.copy(HEADERS_FILE, HEADERS_FILE.with_suffix('.json.bak'))

        save_json(HEADERS_FILE, final)
        print(f"💾 headers.json: {len(final)} правил")

    # --- 5. Применение заголовков ---
    print("\n🔧 Применение заголовков...")
    add_user_agent.main()

    # --- 6. Статистика ---
    alive = sum(1 for e in cache.values() if e.get("status") == "alive")
    dead = sum(1 for e in cache.values() if e.get("status") == "dead")
    unknown = sum(1 for e in cache.values() if e.get("status") == "unknown")
    save_json(ALIVE_FILE, {
        "total": len(all_urls),
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
        "removed": len(to_remove),
        "mode": "full" if FULL_CHECK else "quick",
        "updated_at": now_iso,
    })
    save_json(CACHE_FILE, cache)
    print(f"\n✅ Живых: {alive}, мёртвых: {dead}, неизвестно: {unknown}, удалено: {len(to_remove)}")


if __name__ == '__main__':
    main()
