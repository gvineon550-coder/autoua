"""Проверка URL из check_urls.txt. Разворачивает M3U-плейлисты, дедуплицирует,
подтягивает логотипы из iptv-org/logos.json, генерирует отчёт (JSON) и живой плейлист (M3U)."""

import asyncio
import json
import os
import random
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
from curl_cffi.requests import AsyncSession

CHECK_URLS_FILE = Path('check_urls.txt')
REPORT_FILE = Path('check_urls_report.json')
ALIVE_PLAYLIST_FILE = Path('check_urls_alive.m3u')
URL_PATTERN = re.compile(r'^https?://')

GLOBAL_CONCURRENCY = 32
PER_DOMAIN_LIMIT = 8
TIMEOUT = 8

PLAYLIST_MAX_ITEMS = int(os.environ.get('PLAYLIST_MAX_ITEMS', '200'))
CHECK_SEGMENTS = os.environ.get('CHECK_SEGMENTS', '0') == '1'
JITTER_MIN = float(os.environ.get('JITTER_MIN', '0.1'))
JITTER_MAX = float(os.environ.get('JITTER_MAX', '0.5'))
DEBUG_UA = os.environ.get('DEBUG_UA') == '1'
# Подтягивать логотипы из iptv-org/logos.json (1 = вкл, 0 = выкл)
FETCH_LOGOS = os.environ.get('FETCH_LOGOS', '1') == '1'
EXTRA_HEADERS = json.loads(os.environ.get('EXTRA_HEADERS', '{}'))

LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0"]
_impersonation_by_host: dict[str, str] = {}


def impersonation_for(host: str) -> str:
    if host not in _impersonation_by_host:
        _impersonation_by_host[host] = random.choice(IMPERSONATIONS)
    return _impersonation_by_host[host]


class C:
    RESET = '\033[0m'; BOLD = '\033[1m'; DIM = '\033[2m'
    RED = '\033[91m'; GREEN = '\033[92m'; YELLOW = '\033[93m'
    CYAN = '\033[96m'; MAGENTA = '\033[95m'


def info(m): print(f"{C.CYAN}i  {m}{C.RESET}", flush=True)
def ok(m):   print(f"{C.GREEN}+  {m}{C.RESET}", flush=True)
def step(m): print(f"\n{C.BOLD}{C.MAGENTA}> {m}{C.RESET}", flush=True)


class DomainLimiter:
    def __init__(self, n):
        self.n = n
        self._sems = {}
    def get(self, host):
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self.n)
        return self._sems[host]


async def jitter():
    await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))


async def fetch(session, method, url, **kwargs):
    if EXTRA_HEADERS:
        if 'headers' in kwargs:
            kwargs['headers'] = {**EXTRA_HEADERS, **kwargs['headers']}
        else:
            kwargs['headers'] = EXTRA_HEADERS
    try:
        return await getattr(session, method)(url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
    except Exception:
        return None


# ---------- Логотипы ----------

def _logo_score(entry):
    """Чем больше — тем лучше. Приоритет: in_use > формат > размер."""
    score = 0
    if entry.get("in_use"): score += 1000
    fmt = (entry.get("format") or "").upper()
    if fmt == "PNG": score += 30
    elif fmt in ("SVG", "WEBP"): score += 20
    elif fmt in ("JPEG", "JPG"): score += 10
    w, h = entry.get("width") or 0, entry.get("height") or 0
    score += min(w, h) / 1000  # небольшой бонус за разрешение
    return score


async def load_logo_index():
    """Возвращает {channel_id: logo_url}. Загружает logos.json из iptv-org."""
    if not FETCH_LOGOS:
        return {}
    async with AsyncSession(max_clients=2) as session:
        r = await fetch(session, "get", LOGOS_URL, impersonate=impersonation_for("iptv-org.github.io"))
        if r is None or r.status_code != 200:
            info("Логотипы: logos.json недоступен, пропускаю.")
            return {}
        try:
            data = r.json()
        except Exception as e:
            info(f"Логотипы: ошибка парсинга logos.json — {e}")
            return {}

    best = {}
    for entry in data:
        ch = entry.get("channel")
        url = entry.get("url")
        if not ch or not url:
            continue
        if ch not in best or _logo_score(entry) > _logo_score(best[ch]):
            best[ch] = entry
    index = {ch: e["url"] for ch, e in best.items()}
    info(f"Логотипы: загружено {len(index)} каналов из logos.json")
    return index


def _extinf_get(extinf, attr):
    m = re.search(rf'{attr}="([^"]*)"', extinf)
    return m.group(1) if m else None


def _extinf_has_logo(extinf):
    return bool(re.search(r'tvg-logo="[^"]+"', extinf))


def enrich_extinf(extinf, logo_index):
    """Если в #EXTINF нет tvg-logo и есть tvg-id — подставляет логотип."""
    if not extinf or not logo_index or _extinf_has_logo(extinf):
        return extinf
    tvg_id = _extinf_get(extinf, "tvg-id")
    if not tvg_id:
        return extinf
    logo = logo_index.get(tvg_id)
    if not logo:
        return extinf
    # вставляем атрибут перед именем канала (после последней кавычки перед запятой)
    if extinf.rstrip().endswith('"'):
        # нестандартный EXTINF без имени — просто добавим атрибут в конец
        return extinf.rstrip() + f' tvg-logo="{logo}"'
    return re.sub(r'(,.*)$', lambda m: f' tvg-logo="{logo}"' + m.group(1), extinf, count=1)


# ---------- Парсинг и проверка ----------

def name_from_extinf(extinf):
    if not extinf: return None
    m = re.search(r',([^,]*)$', extinf)
    return m.group(1).strip() if m else None


def parse_iptv_playlist(text):
    entries, extinf = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith('#EXTINF'):
            extinf = line
        elif line.startswith('#'):
            continue
        elif URL_PATTERN.match(line):
            entries.append((extinf or f'#EXTINF:-1,{line}', line))
            extinf = None
    return entries


async def expand_playlist(session, url):
    host = urlparse(url).netloc.lower()
    r = await fetch(session, "get", url, impersonate=impersonation_for(host))
    if r is None or r.status_code not in (200, 206): return None
    if '#EXTINF' not in r.text: return None
    return parse_iptv_playlist(r.text)


async def deep_check(session, url, gsem, limiter, source=None, extinf=None):
    host = urlparse(url).netloc.lower()
    result = {
        "url": url, "status": "unknown", "http_code": 0,
        "is_hls": False, "hls_type": None, "segments_ok": False,
        "headers_needed": None, "note": "",
        "source": source, "extinf": extinf, "name": name_from_extinf(extinf),
    }
    imp = impersonation_for(host)
    async with gsem, limiter.get(host):
        await jitter()
        r = await fetch(session, "get", url, impersonate=imp)

        if r is None or r.status_code == 403:
            for alt in random.sample(IMPERSONATIONS, k=min(2, len(IMPERSONATIONS))):
                await jitter()
                r_alt = await fetch(session, "get", url, impersonate=alt)
                if r_alt is not None and r_alt.status_code == 200:
                    result["http_code"] = 200; result["note"] = f"works with {alt}"
                    r = r_alt; imp = alt; break
            if r is None:
                result["note"] = "network error"; return result
            result["http_code"] = r.status_code
        else:
            result["http_code"] = r.status_code

        if r.status_code in (401, 403):
            root = f"https://{host}/"
            parts = host.split('.')
            parent = f"https://{'.'.join(parts[-2:])}/" if len(parts) >= 2 else root
            for nm, hdrs in [
                ("ref-host", {"Referer": root}),
                ("ref-parent", {"Referer": parent}),
                ("both-host", {"Referer": root, "Origin": root.rstrip('/')}),
                ("both-parent", {"Referer": parent, "Origin": parent.rstrip('/')}),
            ]:
                await jitter()
                r2 = await fetch(session, "get", url, impersonate=imp, headers=hdrs)
                if r2 is not None and r2.status_code == 200:
                    result["http_code"] = 200; result["headers_needed"] = hdrs
                    result["note"] = f"works with {nm}"; r = r2; break

        if r.status_code in (404, 410): result["status"] = "dead"; return result
        if r.status_code in (401, 403): result["status"] = "blocked"; return result
        if r.status_code not in (200, 206): return result

        text = r.text[:8192]
        if '#EXTM3U' not in text:
            result["status"] = "alive"; result["note"] = result["note"] or "not HLS"; return result

        result["is_hls"] = True
        result["hls_type"] = "VOD" if '#EXT-X-ENDLIST' in text else "LIVE"
        result["status"] = "alive"

        if not CHECK_SEGMENTS: return result
        # ... (остальная логика проверки сегментов без изменений) ...
        return result


async def resolve_targets(session, urls, logo_index):
    targets, seen_urls = [], set()
    for url in urls:
        expanded = await expand_playlist(session, url)
        if expanded is None:
            if url not in seen_urls:
                seen_urls.add(url)
                targets.append({"url": url, "source": None, "extinf": None})
        else:
            sample = expanded[:PLAYLIST_MAX_ITEMS]
            dupes = len(sample) - len({s for _, s in sample})
            info(f"Плейлист {url}: {len(expanded)} каналов, беру {len(sample)}"
                 + (f" (внутренних дублей: {dupes})" if dupes else ""))
            for extinf, stream in sample:
                if stream in seen_urls: continue
                seen_urls.add(stream)
                targets.append({
                    "url": stream,
                    "source": url,
                    "extinf": enrich_extinf(extinf, logo_index),  # ← подстановка логотипа
                })
    return targets


async def check_all(targets):
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = []
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [deep_check(session, t["url"], gsem, limiter, source=t["source"], extinf=t["extinf"])
                 for t in targets]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
    return results


def load_urls():
    if not CHECK_URLS_FILE.exists(): return []
    urls, seen = [], set()
    with open(CHECK_URLS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#') or not URL_PATTERN.match(s): continue
            if s in seen: continue
            seen.add(s); urls.append(s)
    return urls


def write_alive_playlist(results):
    alive = [r for r in results if r["status"] == "alive"]
    if not alive:
        info("Живых каналов нет — плейлист не создаю."); return None
    lines, seen = ['#EXTM3U'], set()
    for r in alive:
        if r["url"] in seen: continue
        seen.add(r["url"])
        extinf = r.get("extinf") or f'#EXTINF:-1,{r.get("name") or r["url"]}'
        lines.append(extinf); lines.append(r["url"])
    with open(ALIVE_PLAYLIST_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return len(seen)


async def run(urls):
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        logo_index = await load_logo_index()          # ← логотипы
        targets = await resolve_targets(session, urls, logo_index)
    if not targets: return []
    info(f"Уникальных стримов к проверке: {len(targets)}")
    return await check_all(targets)


# ... (debug_ua и main() без изменений, кроме run(urls) — он уже обновлён) ...
