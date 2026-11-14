"""
Полный пайплайн автообновления плейлистов с красивым выводом в Actions
и автоматическим отчётом в README.

Логика:
  0. Дедуп → 1. Живость → 2. Восстановление → 3. Удаление → 4. Заголовки → 5. Применение → 6. Отчёт в README.
"""

import asyncio
import json
import os
import random
import re
import shutil
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
DEAD_META_FILE = Path('dead_meta.json')
README_FILE = Path('README.md')
URL_PATTERN = re.compile(r'^https?://')

MARKER_START = '<!-- STATS:START -->'
MARKER_END = '<!-- STATS:END -->'

GLOBAL_CONCURRENCY = 64
PER_DOMAIN_LIMIT = 4
TIMEOUT_QUICK = 6
TIMEOUT_DETECT = 10

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0", "edge101"]
VLC_UA = "VLC/3.0.18 LibVLC/3.0.18"


# ==================================================================
# 🎨 Цветной вывод и прогресс
# ==================================================================
class C:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'


def info(msg):  print(f"{C.CYAN}ℹ️  {msg}{C.RESET}", flush=True)
def ok(msg):    print(f"{C.GREEN}✅ {msg}{C.RESET}", flush=True)
def warn(msg):  print(f"{C.YELLOW}⚠️  {msg}{C.RESET}", flush=True)
def err(msg):   print(f"{C.RED}❌ {msg}{C.RESET}", flush=True)
def step(msg):  print(f"\n{C.BOLD}{C.MAGENTA}▶ {msg}{C.RESET}", flush=True)


def gh_group(title):
    print(f"::group::{C.BOLD}{title}{C.RESET}", flush=True)


def gh_endgroup():
    print("::endgroup::", flush=True)


def gh_notice(msg):
    print(f"::notice::{msg}", flush=True)


def gh_warning(msg):
    print(f"::warning::{msg}", flush=True)


class Progress:
    """Прогресс-бар [████████░░] 80% (2400/3000), печать на каждый десяток %."""

    def __init__(self, total: int, prefix: str = "Прогресс", width: int = 30):
        self.total = max(total, 1)
        self.prefix = prefix
        self.width = width
        self.current = 0
        self._last_decile = -1
        self._print(force=True)

    def _print(self, force=False):
        pct = int(self.current * 100 / self.total)
        decile = pct // 10
        if not force and decile == self._last_decile and self.current < self.total:
            return
        self._last_decile = decile
        filled = int(self.width * self.current / self.total)
        bar = '█' * filled + '░' * (self.width - filled)
        color = C.GREEN if pct == 100 else (C.YELLOW if pct >= 50 else C.CYAN)
        print(f"{color}{self.prefix} [{bar}] {pct:3d}% ({self.current}/{self.total}){C.RESET}", flush=True)

    def tick(self, n: int = 1):
        self.current = min(self.current + n, self.total)
        self._print()

    def finish(self):
        self.current = self.total
        self._print(force=True)


def write_step_summary(markdown: str):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(markdown + '\n')
        except Exception:
            pass


# ==================================================================
# Семафоры и утилиты
# ==================================================================
class DomainLimiter:
    def __init__(self, per_domain: int):
        self.per_domain = per_domain
        self._sems: dict[str, asyncio.Semaphore] = {}

    def get(self, host: str) -> asyncio.Semaphore:
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self.per_domain)
        return self._sems[host]


async def jitter():
    await asyncio.sleep(random.uniform(0.05, 0.25))


def collect_urls():
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


def collect_urls_by_file():
    """{Path: [urls]} — для пофайловой статистики."""
    result = {}
    for ext in ('*.m3u', '*.m3u8'):
        for fp in Path('.').rglob(ext):
            if fp.name == DEAD_ARCHIVE.name:
                continue
            urls = []
            with open(fp, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if URL_PATTERN.match(s):
                        urls.append(s)
            if urls:
                result[fp] = urls
    return result


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def fetch_with_retry(session, method, url, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            return await getattr(session, method)(url, **kwargs)
        except Exception:
            if attempt == attempts - 1:
                return None
            await asyncio.sleep(0.5 * (2 ** attempt))
    return None


# ==================================================================
# Живость
# ==================================================================
async def quick_alive_one(session, url, global_sem, limiter):
    host = urlparse(url).netloc.lower()
    async with global_sem, limiter.get(host):
        await jitter()

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
                r2 = await fetch_with_retry(session, "get", url,
                                            impersonate=random.choice(IMPERSONATIONS),
                                            headers={"Range": "bytes=0-2047"},
                                            timeout=TIMEOUT_QUICK, allow_redirects=True)
                if r2 is not None and r2.status_code in (200, 206) and '#EXTM3U' in r2.text[:256]:
                    return url, "alive"
                return url, "unknown"

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


async def check_alive_batch(urls, progress: Progress):
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [quick_alive_one(session, u, global_sem, limiter) for u in urls]
        for coro in asyncio.as_completed(tasks):
            url, status = await coro
            results[url] = status
            progress.tick()
    return results


# ==================================================================
# Подбор заголовков
# ==================================================================
async def detect_one_host(session, host, url, limiter, global_sem):
    async with global_sem, limiter.get(host):
        await jitter()
        r = await fetch_with_retry(session, "get", url,
                                   impersonate=random.choice(IMPERSONATIONS),
                                   timeout=TIMEOUT_DETECT)
        if r is None:
            return host, None
        if r.status_code in (200, 404, 410):
            return host, None

        root = f"https://{host}/"
        parts = host.split('.')
        parent = f"https://{'.'.join(parts[-2:])}/" if len(parts) >= 2 else root

        for name, hdrs in [
            ("ref-host",    {"Referer": root}),
            ("ref-parent",  {"Referer": parent}),
            ("both-host",   {"Referer": root, "Origin": root.rstrip('/')}),
            ("both-parent", {"Referer": parent, "Origin": parent.rstrip('/')}),
        ]:
            async with limiter.get(host):
                await jitter()
                r = await fetch_with_retry(session, "get", url,
                                           impersonate=random.choice(IMPERSONATIONS),
                                           headers=hdrs, timeout=TIMEOUT_DETECT)
                if r is not None and r.status_code == 200:
                    result = {}
                    if "Referer" in hdrs: result["referer"] = hdrs["Referer"]
                    if "Origin" in hdrs: result["origin"] = hdrs["Origin"]
                    return host, result

        async with limiter.get(host):
            await jitter()
            try:
                r = await session.get(url, headers={"User-Agent": VLC_UA},
                                      impersonate=None, timeout=TIMEOUT_DETECT)
                if r.status_code == 200:
                    return host, {"user_agent": VLC_UA}
            except Exception:
                pass
        return host, None


async def detect_headers_batch(samples, progress: Progress):
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [detect_one_host(session, h, u, limiter, global_sem) for h, u in samples.items()]
        for coro in asyncio.as_completed(tasks):
            host, cfg = await coro
            if cfg:
                results[host] = cfg
                ok(f"{C.CYAN}{host}{C.RESET} → {cfg}")
            else:
                warn(f"{C.DIM}{host} — не удалось подобрать (работает без хедеров?){C.RESET}")
            progress.tick()
    return results


# ==================================================================
# Архив мёртвых
# ==================================================================
def remove_dead(file_path, dead_set):
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
            archived.append((extinf, lines[i], str(file_path)))
            i += 1
            continue
        new_lines.append(lines[i])
        i += 1
    if archived:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    return archived


def rebuild_dead_m3u(meta):
    with open(DEAD_ARCHIVE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n# Архив мёртвых ссылок (автоматически восстановятся при оживлении)\n\n')
        for url, info in meta.items():
            extinf = info.get('extinf') or '#EXTINF:-1 group-title="Архив", Удалено\n'
            f.write(extinf.rstrip('\n') + '\n')
            f.write(url + '\n\n')


def archive_dead(new_archived):
    if not new_archived:
        return 0
    meta = load_json(DEAD_META_FILE, {})
    now_iso = datetime.now(timezone.utc).isoformat()
    added = 0
    for extinf, url, source in new_archived:
        url = url.strip()
        if url in meta:
            continue
        meta[url] = {
            "extinf": extinf if extinf else '#EXTINF:-1 group-title="Архив", Удалено\n',
            "source": source,
            "archived_at": now_iso,
        }
        added += 1
    if added:
        save_json(DEAD_META_FILE, meta)
        rebuild_dead_m3u(meta)
    return added


def restore_revived(alive_urls, dead_meta):
    revived = [u for u in dead_meta if u in alive_urls]
    if not revived:
        return []
    restored = []
    for url in revived:
        info_ = dead_meta[url]
        source = Path(info_.get("source", ""))
        extinf = info_.get("extinf") or '#EXTINF:-1 group-title="Восстановлено", Оживший поток\n'
        if not source.exists():
            warn(f"источник {source} не найден — {url} остаётся в архиве")
            continue
        already_in = False
        with open(source, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip() == url:
                    already_in = True
                    break
        if already_in:
            info(f"{url} уже в {source} — убираю из архива без добавления")
            del dead_meta[url]
            continue
        with open(source, 'a', encoding='utf-8') as f:
            f.write(extinf.rstrip('\n') + '\n')
            f.write(url + '\n\n')
        del dead_meta[url]
        restored.append(url)
        ok(f"{url} → {source}")
    if restored:
        save_json(DEAD_META_FILE, dead_meta)
        rebuild_dead_m3u(dead_meta)
    return restored


# ==================================================================
# 📊 Генерация README-отчёта
# ==================================================================
README_TEMPLATE = """# 🎬 Кинозалы — IPTV плейлисты

[![Auto-update](https://github.com/{repo}/actions/workflows/update-playlists.yml/badge.svg)](https://github.com/{repo}/actions/workflows/update-playlists.yml)
[![Last Commit](https://img.shields.io/github/last-commit/{repo}?color=purple)](https://github.com/{repo}/commits)
[![Repo Size](https://img.shields.io/github/repo-size/{repo}?color=orange)](https://github.com/{repo})

Автоматически обновляемые M3U-плейлисты с актуальным `User-Agent` и `Referer`/`Origin`.
Каждый пуш запускает проверку живости, дедупликацию, чистку мёртвых ссылок и применение
HTTP-заголовков через GitHub Actions.

<!-- STATS:START -->
<!-- STATS:END -->

## 🚀 Как использовать

1. Скопируйте ссылку на нужный `.m3u` файл:
