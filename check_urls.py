"""Проверка URL из check_urls.txt. Разворачивает M3U-плейлисты, дедуплицирует,
подтягивает логотипы из iptv-org/logos.json, ведёт headers.json,
генерирует отчёт (JSON) и живой плейлист (M3U) с #EXTVLCOPT. Без прокси."""

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
HEADERS_FILE = Path('headers.json')
URL_PATTERN = re.compile(r'^https?://')
GROUP_PATTERN = re.compile(r'group-title="([^"]*)"')

GLOBAL_CONCURRENCY = 32
PER_DOMAIN_LIMIT = 8
TIMEOUT = 8

PLAYLIST_MAX_ITEMS = int(os.environ.get('PLAYLIST_MAX_ITEMS', '200'))
CHECK_SEGMENTS = os.environ.get('CHECK_SEGMENTS', '0') == '1'
JITTER_MIN = float(os.environ.get('JITTER_MIN', '0.1'))
JITTER_MAX = float(os.environ.get('JITTER_MAX', '0.5'))
DEBUG_UA = os.environ.get('DEBUG_UA') == '1'
FETCH_LOGOS = os.environ.get('FETCH_LOGOS', '1') == '1'
EMIT_VLCOPT = os.environ.get('EMIT_VLCOPT', '1') == '1'
EXTRA_HEADERS = json.loads(os.environ.get('EXTRA_HEADERS', '{}'))

# UA для #EXTVLCOPT в плейлисте. Согласован с chrome131 из IMPERSONATIONS.
DEFAULT_UA = os.environ.get(
    'DEFAULT_UA',
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
)

LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0"]
_impersonation_by_host: dict[str, str] = {}


def _load_headers_cfg() -> dict:
    if not HEADERS_FILE.exists():
        return {}
    try:
        with open(HEADERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"headers.json: ошибка чтения ({e}), пропускаю")
        return {}


HEADERS_CFG = _load_headers_cfg()


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
        return await getattr(session, method)(
            url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
    except Exception:
        return None


# ---------- Логотипы ----------

def _logo_score(entry):
    score = 0
    if entry.get("in_use"): score += 1000
    fmt = (entry.get("format") or "").upper()
    if fmt == "PNG": score += 30
    elif fmt in ("SVG", "WEBP"): score += 20
    elif fmt in ("JPEG", "JPG"): score += 10
    w, h = entry.get("width") or 0, entry.get("height") or 0
    score += min(w, h) / 1000
    return score


async def load_logo_index():
    if not FETCH_LOGOS:
        return {}
    async with AsyncSession(max_clients=2) as session:
        r = await fetch(session, "get", LOGOS_URL,
                        impersonate=impersonation_for("iptv-org.github.io"))
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
        ch, url = entry.get("channel"), entry.get("url")
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
    if not extinf or not logo_index or _extinf_has_logo(extinf):
        return extinf
    tvg_id = _extinf_get(extinf, "tvg-id")
    if not tvg_id:
        return extinf
    logo = logo_index.get(tvg_id)
    if not logo:
        return extinf
    if extinf.rstrip().endswith('"'):
        return extinf.rstrip() + f' tvg-logo="{logo}"'
    return re.sub(r'(,.*)$', lambda m: f' tvg-logo="{logo}"' + m.group(1), extinf, count=1)


# ---------- Парсинг ----------

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


# ---------- Проверка ----------

def _merge_headers_needed(known: dict | None, discovered: dict | None) -> dict | None:
    """Приводит к формату {referer, origin} (lowercase keys)."""
    out = {}
    for src in (known, discovered):
        if not src: continue
        for k, v in src.items():
            kl = k.lower()
            if kl in ("referer", "origin") and v:
                out[kl] = v
    return out or None


async def deep_check(session, url, gsem, limiter, source=None, extinf=None):
    host = urlparse(url).netloc.lower()
    result = {
        "url": url, "status": "unknown", "http_code": 0,
        "is_hls": False, "hls_type": None, "segments_ok": False,
        "headers_needed": None, "note": "",
        "source": source, "extinf": extinf, "name": name_from_extinf(extinf),
    }
    imp = impersonation_for(host)
    known = HEADERS_CFG.get(host) or {}

    req_headers = {}
    if known.get("referer"): req_headers["Referer"] = known["referer"]
    if known.get("origin"):  req_headers["Origin"] = known["origin"]

    async with gsem, limiter.get(host):
        await jitter()
        r = await fetch(session, "get", url, impersonate=imp,
                        **({"headers": req_headers} if req_headers else {}))

        # network error / 403 → пробуем альтернативные impersonation
        if r is None or r.status_code == 403:
            for alt in random.sample(IMPERSONATIONS, k=min(2, len(IMPERSONATIONS))):
                await jitter()
                r_alt = await fetch(session, "get", url, impersonate=alt,
                                    **({"headers": req_headers} if req_headers else {}))
                if r_alt is not None and r_alt.status_code == 200:
                    r = r_alt; imp = alt; break
            if r is None:
                result["note"] = "network error"
                return result

        result["http_code"] = r.status_code

        # discovery: если 401/403 и для домена ещё нет конфига
        if r.status_code in (401, 403) and not req_headers:
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
                    result["http_code"] = 200
                    result["note"] = f"works with {nm}"
                    HEADERS_CFG[host] = {
                        k.lower(): v for k, v in (
                            ("referer", hdrs.get("Referer")),
                            ("origin",  hdrs.get("Origin")),
                        ) if v
                    }
                    r = r2
                    break

        # фиксируем заголовки, которые пойдут в #EXTVLCOPT
        result["headers_needed"] = _merge_headers_needed(known, HEADERS_CFG.get(host))

        if r.status_code in (404, 410): result["status"] = "dead";    return result
        if r.status_code in (401, 403): result["status"] = "blocked"; return result
        if r.status_code not in (200, 206): return result

        text = r.text[:8192]
        if '#EXTM3U' not in text:
            result["status"] = "alive"
            result["note"] = result["note"] or "not HLS"
            return result

        result["is_hls"] = True
        result["hls_type"] = "VOD" if '#EXT-X-ENDLIST' in text else "LIVE"
        result["status"] = "alive"

        if not CHECK_SEGMENTS:
            return result

        if '#EXT-X-STREAM-INF' in text:
            lines = text.split('\n')
            variant = None
            for i, line in enumerate(lines):
                if '#EXT-X-STREAM-INF' in line and i + 1 < len(lines):
                    v = lines[i + 1].strip()
                    if v and not v.startswith('#'):
                        variant = v; break
            if variant:
                if not variant.startswith('http'): variant = urljoin(url, variant)
                r3 = await fetch(session, "get", variant, impersonate=imp)
                if r3 is not None and r3.status_code in (200, 206):
                    segs = [l.strip() for l in r3.text[:8192].split('\n')
                            if l.strip() and not l.startswith('#')]
                    if segs:
                        seg = segs[0]
                        if not seg.startswith('http'): seg = urljoin(variant, seg)
                        r4 = await fetch(session, "get", seg, impersonate=imp, stream=True)
                        if r4 is not None and r4.status_code in (200, 206):
                            result["segments_ok"] = True
            return result

        segs = [l.strip() for l in text.split('\n')
                if l.strip() and not l.startswith('#')]
        if segs:
            seg = segs[0]
            if not seg.startswith('http'): seg = urljoin(url, seg)
            r5 = await fetch(session, "get", seg, impersonate=imp, stream=True)
            if r5 is not None and r5.status_code in (200, 206):
                result["segments_ok"] = True
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
                    "url": stream, "source": url,
                    "extinf": enrich_extinf(extinf, logo_index),
                })
    return targets


async def check_all(targets):
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = []
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [deep_check(session, t["url"], gsem, limiter,
                            source=t["source"], extinf=t["extinf"])
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


def _extinf_group(extinf):
    m = GROUP_PATTERN.search(extinf or "")
    return m.group(1) if m else ""


def write_alive_playlist(results):
    """alive.m3u с #EXTVLCOPT. Дедуп по URL, мультикатегория сохраняется."""
    alive = [r for r in results if r["status"] == "alive"]
    if not alive:
        info("Живых каналов нет — плейлист не создаю."); return None

    lines, seen_keys = ['#EXTM3U'], set()
    for r in alive:
        extinf = r.get("extinf") or f'#EXTINF:-1,{r.get("name") or r["url"]}'
        key = (_extinf_group(extinf), r["url"])
        if key in seen_keys: continue
        seen_keys.add(key)

        lines.append(extinf)

        if EMIT_VLCOPT:
            host = urlparse(r["url"]).netloc.lower()
            cfg = HEADERS_CFG.get(host) or {}
            ua = cfg.get("user_agent") or DEFAULT_UA
            lines.append(f"#EXTVLCOPT:http-user-agent={ua}")
            hdrs = r.get("headers_needed") or {}
            if hdrs.get("referer"):
                lines.append(f"#EXTVLCOPT:http-referrer={hdrs['referer']}")
            if hdrs.get("origin"):
                lines.append(f"#EXTVLCOPT:http-origin={hdrs['origin']}")

        lines.append(r["url"])

    with open(ALIVE_PLAYLIST_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return len(seen_keys)


def save_headers_config():
    """Пишет headers.json, если он изменился относительно исходного."""
    if HEADERS_CFG == _load_headers_cfg():
        return False
    with open(HEADERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(HEADERS_CFG, f, indent=2, ensure_ascii=False, sort_keys=True)
    return True


async def debug_ua():
    host = "httpbin.org"
    async with AsyncSession(max_clients=1) as session:
        r = await fetch(session, "get", "https://httpbin.org/headers",
                        impersonate=impersonation_for(host))
        print(r.text if r else "httpbin unreachable")


async def run(urls):
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        logo_index = await load_logo_index()
        targets = await resolve_targets(session, urls, logo_index)
    if not targets: return []
    info(f"Уникальных стримов к проверке: {len(targets)}")
    return await check_all(targets)


def main():
    if DEBUG_UA:
        asyncio.run(debug_ua()); return

    print(f"{C.BOLD}{C.MAGENTA}=== URL check ==={C.RESET}")
    urls = load_urls()
    if not urls:
        print(f"В {CHECK_URLS_FILE} нет URL — добавь по одной на строку.")
        return

    results = asyncio.run(run(urls))
    results.sort(key=lambda r: (
        {"dead": 0, "blocked": 1, "unknown": 2, "alive": 3}[r["status"]],
        r["source"] or "", r["name"] or "", r["url"],
    ))

    step("Результаты")
    for r in results:
        icon = {"alive": "OK", "dead": "DEAD", "blocked": "403", "unknown": "??"}[r["status"]]
        color = {"alive": C.GREEN, "dead": C.RED, "blocked": C.YELLOW, "unknown": C.DIM}[r["status"]]
        hls = f" [{r['hls_type']}, seg={'OK' if r['segments_ok'] else 'FAIL'}]" if r["is_hls"] else ""
        hdrs = f" +headers: {list((r['headers_needed'] or {}).keys())}" if r["headers_needed"] else ""
        note = f" — {r['note']}" if r["note"] else ""
        prefix = f"[{r['name']}] " if r["name"] else ""
        print(f"{color}[{icon}]{C.RESET} {prefix}{r['url']}{hls}{hdrs}{note}")

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "alive": sum(1 for r in results if r["status"] == "alive"),
        "dead": sum(1 for r in results if r["status"] == "dead"),
        "blocked": sum(1 for r in results if r["status"] == "blocked"),
        "unknown": sum(1 for r in results if r["status"] == "unknown"),
        "results": results,
    }
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    ok(f"Отчёт: {REPORT_FILE}")

    n_alive = write_alive_playlist(results)
    if n_alive is not None:
        ok(f"Живой плейлист: {ALIVE_PLAYLIST_FILE} ({n_alive} каналов)")

    if save_headers_config():
        ok(f"Заголовки: обновлён {HEADERS_FILE} ({len(HEADERS_CFG)} доменов)")


if __name__ == '__main__':
    main()
