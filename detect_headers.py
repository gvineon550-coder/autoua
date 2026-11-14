"""
Auto-update playlists pipeline:
  dedupe -> alive check -> restore revived -> remove dead -> detect headers -> apply -> README report.
"""

import asyncio
import json
import os
import random
import re
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
from curl_cffi.requests import AsyncSession

import add_user_agent

# ============================================================
# Настройки
# ============================================================
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

GLOBAL_CONCURRENCY = 128
PER_DOMAIN_LIMIT = 24
TIMEOUT_QUICK = 4
TIMEOUT_DETECT = 6

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0", "edge101"]
VLC_UA = "VLC/3.0.18 LibVLC/3.0.18"


# ============================================================
# Цветной вывод
# ============================================================
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


def info(m):  print(f"{C.CYAN}ℹ️  {m}{C.RESET}", flush=True)
def ok(m):    print(f"{C.GREEN}✅ {m}{C.RESET}", flush=True)
def warn(m):  print(f"{C.YELLOW}⚠️  {m}{C.RESET}", flush=True)
def step(m):  print(f"\n{C.BOLD}{C.MAGENTA}▶ {m}{C.RESET}", flush=True)

def gh_group(t):    print(f"::group::{C.BOLD}{t}{C.RESET}", flush=True)
def gh_endgroup():  print("::endgroup::", flush=True)
def gh_notice(m):   print(f"::notice::{m}", flush=True)
def gh_warning(m):  print(f"::warning::{m}", flush=True)


class Progress:
    def __init__(self, total, prefix="Прогресс", width=30):
        self.total = max(total, 1)
        self.prefix = prefix
        self.width = width
        self.current = 0
        self._last = -1
        self._print(force=True)

    def _print(self, force=False):
        pct = int(self.current * 100 / self.total)
        d = pct // 10
        if not force and d == self._last and self.current < self.total:
            return
        self._last = d
        filled = int(self.width * self.current / self.total)
        bar = '█' * filled + '░' * (self.width - filled)
        col = C.GREEN if pct == 100 else (C.YELLOW if pct >= 50 else C.CYAN)
        print(f"{col}{self.prefix} [{bar}] {pct:3d}% ({self.current}/{self.total}){C.RESET}", flush=True)

    def tick(self):
        self.current = min(self.current + 1, self.total)
        self._print()

    def finish(self):
        self.current = self.total
        self._print(force=True)


def write_step_summary(md):
    p = os.environ.get('GITHUB_STEP_SUMMARY')
    if p:
        try:
            with open(p, 'a', encoding='utf-8') as f:
                f.write(md + '\n')
        except Exception:
            pass


# ============================================================
# Семафоры и утилиты
# ============================================================
class DomainLimiter:
    def __init__(self, n):
        self.n = n
        self._sems = {}

    def get(self, host):
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self.n)
        return self._sems[host]


async def jitter():
    await asyncio.sleep(random.uniform(0.01, 0.05))


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
                        h = urlparse(s).netloc.lower()
                        if h and h not in samples:
                            samples[h] = s
    return all_urls, samples


def collect_urls_by_file():
    r = {}
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
                r[fp] = urls
    return r


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
    for i in range(attempts):
        try:
            return await getattr(session, method)(url, **kwargs)
        except Exception:
            if i == attempts - 1:
                return None
            await asyncio.sleep(0.5 * (2 ** i))
    return None


# ============================================================
# Живость
# ============================================================
async def quick_alive_one(session, url, gsem, limiter):
    host = urlparse(url).netloc.lower()
    async with gsem, limiter.get(host):
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


async def check_alive_batch(urls, progress):
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [quick_alive_one(session, u, gsem, limiter) for u in urls]
        for coro in asyncio.as_completed(tasks):
            url, status = await coro
            results[url] = status
            progress.tick()
    return results


# ============================================================
# Подбор заголовков
# ============================================================
async def detect_one_host(session, host, url, limiter, gsem):
    async with gsem, limiter.get(host):
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


async def detect_headers_batch(samples, progress):
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = {}
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [detect_one_host(session, h, u, limiter, gsem) for h, u in samples.items()]
        for coro in asyncio.as_completed(tasks):
            host, cfg = await coro
            if cfg:
                results[host] = cfg
                ok(f"{C.CYAN}{host}{C.RESET} → {cfg}")
            else:
                warn(f"{C.DIM}{host} — не удалось подобрать{C.RESET}")
            progress.tick()
    return results


# ============================================================
# Архив мёртвых
# ============================================================
def remove_dead(file_path, dead_set):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    new_lines, archived, i = [], [], 0
    while i < len(lines):
        s = lines[i].strip()
        if URL_PATTERN.match(s) and s in dead_set:
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
        f.write('#EXTM3U\n# Архив мёртвых ссылок\n\n')
        for url, i_ in meta.items():
            extinf = i_.get('extinf') or '#EXTINF:-1 group-title="Архив", Удалено\n'
            f.write(extinf.rstrip('\n') + '\n')
            f.write(url + '\n\n')


def archive_dead(new_archived):
    if not new_archived:
        return 0
    meta = load_json(DEAD_META_FILE, {})
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for extinf, url, source in new_archived:
        url = url.strip()
        if url in meta:
            continue
        meta[url] = {
            "extinf": extinf if extinf else '#EXTINF:-1 group-title="Архив", Удалено\n',
            "source": source,
            "archived_at": now,
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
        i_ = dead_meta[url]
        source = Path(i_.get("source", ""))
        extinf = i_.get("extinf") or '#EXTINF:-1 group-title="Восстановлено", Оживший поток\n'
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
            info(f"{url} уже в {source}")
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


# ============================================================
# 📊 README-отчёт
# ============================================================
def health_emoji(pct):
    if pct >= 99: return "🟢", "Отличное"
    if pct >= 95: return "🟡", "Хорошее"
    if pct >= 85: return "🟠", "Удовлетворительное"
    return "🔴", "Требует внимания"


def progress_bar_md(pct, width=25):
    filled = int(round(width * pct / 100))
    return f"`[{'█' * filled}{'░' * (width - filled)}]` **{pct:.1f}%**"


def build_report_markdown(ctx):
    now_str = ctx['now_iso'].replace('T', ' ').replace('+00:00', ' UTC')[:19] + " UTC"
    alive, dead, unknown = ctx['alive'], ctx['dead'], ctx['unknown']
    total = ctx['total'] or 1
    alive_pct = 100 * alive / total
    dead_pct = 100 * dead / total
    unknown_pct = 100 * unknown / total
    emoji, label = health_emoji(alive_pct)

    L = []
    L.append("## 📊 Отчёт обновления")
    L.append("")
    L.append(f"**Последнее обновление:** `{now_str}` · **Режим:** `{ctx['mode']}` · "
             f"**Время:** `{ctx['duration']:.1f} сек`")
    L.append("")
    L.append("### 📈 Сводка")
    L.append("")
    L.append("| Метрика | Значение |")
    L.append("|---|---:|")
    L.append(f"| 📄 Всего URL | **{total}** |")
    L.append(f"| ✅ Живых | **{alive}** ({alive_pct:.1f}%) |")
    L.append(f"| ❌ Мёртвых | **{dead}** ({dead_pct:.1f}%) |")
    L.append(f"| ⚠️ Неизвестно | **{unknown}** ({unknown_pct:.1f}%) |")
    L.append(f"| 🗑️ Удалено за прогон | **{ctx['removed']}** |")
    L.append(f"| 🎉 Восстановлено | **{ctx['restored_count']}** |")
    L.append(f"| 📦 В архиве | **{ctx['archive_size']}** |")
    L.append("")
    L.append(f"**Здоровье плейлиста:** {emoji} {label} — {progress_bar_md(alive_pct)}")
    L.append("")

    if ctx['files_stats']:
        L += ["### 📁 Плейлисты", "",
              "| Файл | URL | Живых | Мёртвых | Доля живых |",
              "|---|---:|---:|---:|---:|"]
        for fname, st in ctx['files_stats']:
            f_alive = st['alive']
            f_total = st['total'] or 1
            f_pct = 100 * f_alive / f_total
            f_emoji, _ = health_emoji(f_pct)
            L.append(f"| `{fname}` | {st['total']} | {f_alive} | {st['dead']} | {f_emoji} {f_pct:.1f}% |")
        L.append("")

    if ctx['domains_stats']:
        L += ["### 🌐 Домены", "", "| Домен | URL | Заголовки |", "|---|---:|---|"]
        for host, st in ctx['domains_stats']:
            hdrs = ctx['headers_cfg'].get(host, {})
            parts = []
            if 'user_agent' in hdrs: parts.append("`UA`")
            if 'referer' in hdrs: parts.append("`Referer`")
            if 'origin' in hdrs: parts.append("`Origin`")
            hdrs_str = " · ".join(parts) if parts else "— _только UA по умолчанию_"
            L.append(f"| `{host}` | {st['count']} | {hdrs_str} |")
        L.append("")

    if ctx['restored_urls']:
        L.append(f"### 🎉 Восстановленные URL ({len(ctx['restored_urls'])})")
        L.append("")
        for u in ctx['restored_urls'][:15]:
            L.append(f"- `{u}`")
        if len(ctx['restored_urls']) > 15:
            L.append(f"- _…и ещё {len(ctx['restored_urls']) - 15}_")
        L.append("")

    if ctx['removed_list']:
        L.append(f"### 🗑️ Удалённые за прогон ({len(ctx['removed_list'])})")
        L.append("")
        for u in ctx['removed_list'][:15]:
            L.append(f"- `{u}`")
        if len(ctx['removed_list']) > 15:
            L.append(f"- _…и ещё {len(ctx['removed_list']) - 15}_")
        L.append("")

    if ctx['archive_size'] > 0:
        L += [f"### 🗄️ Архив мёртвых ссылок ({ctx['archive_size']})", "",
              "Полный список: [`dead.m3u`](dead.m3u). Ссылки восстанавливаются "
              "автоматически при следующем прогоне, если сервер снова отвечает.", ""]
        items = sorted(ctx['dead_meta'].items(),
                       key=lambda kv: kv[1].get('archived_at', ''), reverse=True)[:5]
        if items:
            L.append("**Недавно добавленные:**")
            L.append("")
            for url, _ in items:
                L.append(f"- `{url}`")
            L.append("")

    L += ["---", "",
          "<sub>🤖 Отчёт генерируется автоматически между маркерами "
          "`STATS:START` / `STATS:END`. Не редактируйте вручную.</sub>"]
    return "\n".join(L)


def load_readme_template(repo):
    """Шаблон собран из коротких строк — не обрезается при копировании."""
    return (
        f"# 🎬 IPTV плейлисты\n\n"
        f"[![Auto-update](https://github.com/{repo}/actions/workflows/update-playlists.yml/badge.svg)]"
        f"(https://github.com/{repo}/actions/workflows/update-playlists.yml)\n\n"
        f"Автоматически обновляемые M3U-плейлисты с актуальным User-Agent и Referer/Origin.\n\n"
        f"<!-- STATS:START -->\n"
        f"<!-- STATS:END -->\n\n"
        f"## 🚀 Как использовать\n\n"
        f"1. Скопируйте ссылку на нужный `.m3u` файл:\n"
        f"   `https://raw.githubusercontent.com/{repo}/main/zerkalo.m3u`\n"
        f"2. Вставьте в IPTV-плеер (VLC, Kodi, TiviMate, OTT Navigator и др.).\n\n"
        f"## ⚙️ Как это работает\n\n"
        f"- `detect_headers.py` — проверяет живость URL, чистит мёртвые, подбирает заголовки.\n"
        f"- `add_user_agent.py` — вставляет `#EXTVLCOPT` в плейлисты.\n"
        f"- GitHub Actions запускается при пуше, по расписанию (пн 04:00 UTC) и вручную.\n\n"
        f"## 🛠 Ручной запуск\n\n"
        f"Actions → **Auto-update playlists** → **Run workflow**.\n"
    )


def update_readme_report(ctx):
    repo = os.environ.get('GITHUB_REPOSITORY', 'owner/repo')
    if not README_FILE.exists():
        info("README.md не найден — создаю автоматически")
        README_FILE.write_text(load_readme_template(repo), encoding='utf-8')
    content = README_FILE.read_text(encoding='utf-8')
    report = build_report_markdown(ctx)
    if MARKER_START in content and MARKER_END in content:
        pat = re.compile(re.escape(MARKER_START) + r'.*?' + re.escape(MARKER_END), re.DOTALL)
        new_content = pat.sub(f"{MARKER_START}\n{report}\n{MARKER_END}", content)
    else:
        warn("Маркеры не найдены — дописываю отчёт в конец")
        new_content = content.rstrip() + f"\n\n{MARKER_START}\n{report}\n{MARKER_END}\n"
    README_FILE.write_text(new_content, encoding='utf-8')
    ok("README.md обновлён")


# ============================================================
# main
# ============================================================
def main():
    start = datetime.now(timezone.utc)

    print(f"{C.BOLD}{C.MAGENTA}╔══════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}║   🎬 Auto-update playlists — начало работы        ║{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}╚══════════════════════════════════════════════════╝{C.RESET}")
    info(f"Режим: {C.BOLD}{'FULL (полная проверка)' if FULL_CHECK else 'QUICK (только новые)'}{C.RESET}")
    info(f"Старт: {start.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # 0. Дедуп
    step("0/6 · Дедупликация")
    gh_group("Дедупликация плейлистов")
    dupes = 0
    for ext in ('*.m3u', '*.m3u8'):
        for fp in Path('.').rglob(ext):
            if fp.name == DEAD_ARCHIVE.name:
                continue
            dupes += add_user_agent.dedupe_playlist(fp)
    gh_endgroup()
    ok(f"Удалено дубликатов: {C.BOLD}{dupes}{C.RESET}")

    all_urls, samples = collect_urls()
    all_set = set(all_urls)
    cache = load_json(CACHE_FILE, {})
    dead_meta = load_json(DEAD_META_FILE, {})
    now_iso = datetime.now(timezone.utc).isoformat()

    to_clear = [u for u in dead_meta if u in all_set]
    for u in to_clear:
        del dead_meta[u]
    if to_clear:
        save_json(DEAD_META_FILE, dead_meta)
        rebuild_dead_m3u(dead_meta)
        info(f"Убрано из архива: {len(to_clear)}")

    print()
    info(f"📄 URL в плейлистах: {C.BOLD}{len(all_urls)}{C.RESET}")
    info(f"🌐 Доменов: {C.BOLD}{len(samples)}{C.RESET}")
    info(f"📦 Кэш: {C.BOLD}{len(cache)}{C.RESET}")
    info(f"🗄️ В архиве: {C.BOLD}{len(dead_meta)}{C.RESET}")

    # 1. Живость
    step("1/6 · Проверка работоспособности URL")
    urls_to_check = [u for u in all_urls if FULL_CHECK or u not in cache]
    dead_to_check = []
    if FULL_CHECK and dead_meta:
        dead_to_check = [u for u in dead_meta if u not in all_set]
    all_to_check = list(set(urls_to_check + dead_to_check))
    info(f"К проверке: {C.BOLD}{len(all_to_check)}{C.RESET} "
         f"(новые: {len(urls_to_check)}, из архива: {len(dead_to_check)})")

    results = {}
    if all_to_check:
        gh_group(f"Проверка {len(all_to_check)} URL")
        prog = Progress(len(all_to_check), prefix="🩺 Проверка URL")
        results = asyncio.run(check_alive_batch(all_to_check, prog))
        prog.finish()
        gh_endgroup()
    else:
        info("Нечего проверять — все URL в кэше")

    # 2. Кэш
    step("2/6 · Обновление кэша")
    for url in urls_to_check:
        status = results.get(url, "unknown")
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
    orphans = [u for u in cache if u not in all_set]
    for u in orphans:
        del cache[u]
    if orphans:
        info(f"Из кэша убрано: {len(orphans)}")
    ok(f"Кэш: {len(cache)} записей")

    # 3. Восстановление
    step("3/6 · Восстановление URL из архива")
    restored_urls = []
    if dead_to_check:
        alive_arch = {u for u in dead_to_check if results.get(u) == "alive"}
        if alive_arch:
            gh_group(f"Восстановление {len(alive_arch)} URL")
            ok(f"Ожило: {len(alive_arch)}")
            restored_urls = restore_revived(alive_arch, dead_meta)
            gh_endgroup()
            ok(f"Восстановлено: {C.BOLD}{C.GREEN}{len(restored_urls)}{C.RESET}")
        else:
            info("Из архива пока ничего не ожило 👍")
    else:
        info("Пропущено (архив пуст или QUICK)")

    # 4. Удаление
    step("4/6 · Удаление мёртвых URL")
    to_remove = set()
    if FULL_CHECK:
        for url in all_urls:
            e = cache.get(url)
            if e and e.get("status") == "dead" and e.get("dead_streak", 0) >= MIN_DEAD_STREAK:
                to_remove.add(url)
    if to_remove:
        gh_group(f"Удаление {len(to_remove)} (streak ≥ {MIN_DEAD_STREAK})")
        arch_all = []
        for ext in ('*.m3u', '*.m3u8'):
            for fp in Path('.').rglob(ext):
                if fp.name == DEAD_ARCHIVE.name:
                    continue
                a = remove_dead(fp, to_remove)
                if a:
                    warn(f"{fp}: -{len(a)}")
                    arch_all.extend(a)
        added = archive_dead(arch_all)
        if added:
            ok(f"В архив добавлено: {added}")
        gh_endgroup()
    else:
        info("Удалять нечего 👍")

    # 5. Заголовки
    step("5/6 · Подбор заголовков")
    existing_h = load_json(HEADERS_FILE, {})
    if FULL_CHECK:
        detect_domains = list(samples.keys())
    else:
        detect_domains = [h for h in samples if h not in existing_h]
    if detect_domains:
        info(f"Доменов к подбору: {len(detect_domains)}")
        gh_group(f"Подбор для {len(detect_domains)}")
        prog = Progress(len(detect_domains), prefix="🔍 Подбор")
        new_r = asyncio.run(detect_headers_batch(
            {h: samples[h] for h in detect_domains}, prog))
        prog.finish()
        gh_endgroup()
        if FULL_CHECK:
            final = {h: v for h, v in new_r.items() if h in samples}
        else:
            existing_h.update(new_r)
            final = {h: v for h, v in existing_h.items() if h in samples}
        save_json(HEADERS_FILE, final)
        ok(f"headers.json: {C.BOLD}{len(final)}{C.RESET} правил")
    else:
        info("Все домены уже в headers.json 👍")

    # 6. Применение + README
    step("6/6 · Применение заголовков и генерация README")
    gh_group("Применение заголовков")
    add_user_agent.main()
    gh_endgroup()

    alive_count = sum(1 for e in cache.values() if e.get("status") == "alive")
    dead_count = sum(1 for e in cache.values() if e.get("status") == "dead")
    unknown_count = sum(1 for e in cache.values() if e.get("status") == "unknown")

    urls_by_file = collect_urls_by_file()
    files_stats = []
    for fp, urls in urls_by_file.items():
        f_alive = sum(1 for u in urls if cache.get(u, {}).get("status") == "alive")
        f_dead = sum(1 for u in urls if cache.get(u, {}).get("status") == "dead")
        files_stats.append((fp.name, {"total": len(urls), "alive": f_alive, "dead": f_dead}))
    files_stats.sort(key=lambda x: x[1]["total"], reverse=True)

    host_counts = {}
    for u in all_urls:
        h = urlparse(u).netloc.lower()
        host_counts[h] = host_counts.get(h, 0) + 1
    domains_stats = [(h, {"count": c}) for h, c in
                     sorted(host_counts.items(), key=lambda x: x[1], reverse=True)]

    current_h = load_json(HEADERS_FILE, {})
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    update_readme_report({
        "now_iso": now_iso, "duration": duration,
        "mode": "FULL" if FULL_CHECK else "QUICK",
        "total": len(all_urls),
        "alive": alive_count, "dead": dead_count, "unknown": unknown_count,
        "removed": len(to_remove), "removed_list": list(to_remove),
        "restored_count": len(restored_urls), "restored_urls": restored_urls,
        "archive_size": len(dead_meta), "dead_meta": dead_meta,
        "files_stats": files_stats, "domains_stats": domains_stats,
        "headers_cfg": current_h,
    })

    save_json(ALIVE_FILE, {
        "total": len(all_urls), "alive": alive_count, "dead": dead_count,
        "unknown": unknown_count, "removed": len(to_remove),
        "restored_count": len(restored_urls), "restored_urls": restored_urls,
        "archive_size": len(dead_meta),
        "mode": "full" if FULL_CHECK else "quick",
        "updated_at": now_iso,
    })
    save_json(CACHE_FILE, cache)

    print()
    print(f"{C.BOLD}{C.MAGENTA}╔══════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}║   📊 Итоги прогона                                ║{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}╚══════════════════════════════════════════════════╝{C.RESET}")
    print(f"  {C.GREEN}✅ Живых:{C.RESET}        {C.BOLD}{alive_count}{C.RESET}")
    print(f"  {C.RED}❌ Мёртвых:{C.RESET}       {C.BOLD}{dead_count}{C.RESET}")
    print(f"  {C.YELLOW}⚠️  Неизвестно:{C.RESET}    {C.BOLD}{unknown_count}{C.RESET}")
    print(f"  {C.RED}🗑️  Удалено:{C.RESET}       {C.BOLD}{len(to_remove)}{C.RESET}")
    print(f"  {C.GREEN}🎉 Восстановлено:{C.RESET} {C.BOLD}{len(restored_urls)}{C.RESET}")
    print(f"  {C.CYAN}📦 В архиве:{C.RESET}      {C.BOLD}{len(dead_meta)}{C.RESET}")
    print(f"  {C.BLUE}⏱️  Время:{C.RESET}         {C.BOLD}{duration:.1f} сек{C.RESET}")
    print()

    summary = (
        f"## 🎬 Auto-update playlists — итоги\n\n"
        f"**Режим:** `{'FULL' if FULL_CHECK else 'QUICK'}` · "
        f"**Время:** `{duration:.1f} сек` · **Когда:** `{now_iso}`\n\n"
        f"| Метрика | Значение |\n|---|---:|\n"
        f"| 📄 URL в плейлистах | **{len(all_urls)}** |\n"
        f"| 🌐 Доменов | **{len(samples)}** |\n"
        f"| ✅ Живых | **{alive_count}** |\n"
        f"| ❌ Мёртвых | **{dead_count}** |\n"
        f"| ⚠️ Неизвестно | **{unknown_count}** |\n"
        f"| 🗑️ Удалено | **{len(to_remove)}** |\n"
        f"| 🎉 Восстановлено | **{len(restored_urls)}** |\n"
        f"| 📦 В архиве | **{len(dead_meta)}** |\n"
    )
    if restored_urls:
        summary += "\n### 🎉 Восстановленные URL\n"
        for u in restored_urls[:20]:
            summary += f"- `{u}`\n"
    write_step_summary(summary)

    if restored_urls:
        gh_notice(f"🎉 Восстановлено URL из архива: {len(restored_urls)}")
    if dead_count > 0:
        gh_warning(f"Мёртвых URL: {dead_count} (удалятся при streak ≥ {MIN_DEAD_STREAK})")


if __name__ == '__main__':
    main()

# === КОНЕЦ ФАЙЛА ===
