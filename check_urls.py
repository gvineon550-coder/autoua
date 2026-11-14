"""Проверка отдельных URL из check_urls.txt. Плейлисты не трогает."""

import asyncio
import json
import random
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
from curl_cffi.requests import AsyncSession

CHECK_URLS_FILE = Path('check_urls.txt')
REPORT_FILE = Path('check_urls_report.json')
URL_PATTERN = re.compile(r'^https?://')

GLOBAL_CONCURRENCY = 32
PER_DOMAIN_LIMIT = 8
TIMEOUT = 8

IMPERSONATIONS = ["chrome124", "chrome131", "chrome120", "firefox133", "safari17_0"]


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
    await asyncio.sleep(random.uniform(0.05, 0.2))


async def fetch(session, method, url, **kwargs):
    try:
        return await getattr(session, method)(
            url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
    except Exception:
        return None


async def deep_check(session, url, gsem, limiter):
    host = urlparse(url).netloc.lower()
    result = {
        "url": url, "status": "unknown", "http_code": 0,
        "is_hls": False, "hls_type": None, "segments_ok": False,
        "headers_needed": None, "note": "",
    }
    async with gsem, limiter.get(host):
        await jitter()
        r = await fetch(session, "get", url,
                        impersonate=random.choice(IMPERSONATIONS))
        if r is None:
            result["note"] = "network error"
            return result
        result["http_code"] = r.status_code

        # Если 401/403 — пробуем заголовки
        if r.status_code in (401, 403):
            root = f"https://{host}/"
            parts = host.split('.')
            parent = f"https://{'.'.join(parts[-2:])}/" if len(parts) >= 2 else root
            for name, hdrs in [
                ("ref-host", {"Referer": root}),
                ("ref-parent", {"Referer": parent}),
                ("both-host", {"Referer": root, "Origin": root.rstrip('/')}),
                ("both-parent", {"Referer": parent, "Origin": parent.rstrip('/')}),
            ]:
                await jitter()
                r2 = await fetch(session, "get", url,
                                 impersonate=random.choice(IMPERSONATIONS),
                                 headers=hdrs)
                if r2 is not None and r2.status_code == 200:
                    result["http_code"] = 200
                    result["headers_needed"] = hdrs
                    result["note"] = f"works with {name}"
                    r = r2
                    break

        if r.status_code in (404, 410):
            result["status"] = "dead"
            return result
        if r.status_code in (401, 403):
            result["status"] = "blocked"
            return result
        if r.status_code not in (200, 206):
            return result

        text = r.text[:8192]
        if '#EXTM3U' not in text:
            result["status"] = "alive"
            result["note"] = result["note"] or "not HLS"
            return result

        result["is_hls"] = True
        result["hls_type"] = "VOD" if '#EXT-X-ENDLIST' in text else "LIVE"
        result["status"] = "alive"

        # Master-манифест → берём первый variant, проверяем его сегмент
        if '#EXT-X-STREAM-INF' in text:
            lines = text.split('\n')
            variant = None
            for i, line in enumerate(lines):
                if '#EXT-X-STREAM-INF' in line and i + 1 < len(lines):
                    v = lines[i + 1].strip()
                    if v and not v.startswith('#'):
                        variant = v
                        break
            if variant:
                if not variant.startswith('http'):
                    variant = urljoin(url, variant)
                r3 = await fetch(session, "get", variant,
                                 impersonate=random.choice(IMPERSONATIONS))
                if r3 is not None and r3.status_code in (200, 206):
                    seg_text = r3.text[:8192]
                    segs = [l.strip() for l in seg_text.split('\n')
                            if l.strip() and not l.startswith('#')]
                    if segs:
                        seg = segs[0]
                        if not seg.startswith('http'):
                            seg = urljoin(variant, seg)
                        r4 = await fetch(session, "get", seg,
                                         impersonate=random.choice(IMPERSONATIONS),
                                         stream=True)
                        if r4 is not None and r4.status_code in (200, 206):
                            result["segments_ok"] = True
            return result

        # Media-манифест → проверяем первый сегмент
        segs = [l.strip() for l in text.split('\n')
                if l.strip() and not l.startswith('#')]
        if segs:
            seg = segs[0]
            if not seg.startswith('http'):
                seg = urljoin(url, seg)
            r5 = await fetch(session, "get", seg,
                             impersonate=random.choice(IMPERSONATIONS),
                             stream=True)
            if r5 is not None and r5.status_code in (200, 206):
                result["segments_ok"] = True
        return result


async def check_all(urls):
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    limiter = DomainLimiter(PER_DOMAIN_LIMIT)
    results = []
    async with AsyncSession(max_clients=GLOBAL_CONCURRENCY) as session:
        tasks = [deep_check(session, u, gsem, limiter) for u in urls]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
    return results


def load_urls():
    if not CHECK_URLS_FILE.exists():
        return []
    urls = []
    with open(CHECK_URLS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith('#') and URL_PATTERN.match(s):
                urls.append(s)
    return urls


def main():
    print(f"{C.BOLD}{C.MAGENTA}=== URL check ==={C.RESET}")
    urls = load_urls()
    if not urls:
        print(f"В {CHECK_URLS_FILE} нет URL — добавь по одной на строку и запушь снова.")
        return
    info(f"URL к проверке: {len(urls)}")

    results = asyncio.run(check_all(urls))
    results.sort(key=lambda r: (
        {"dead": 0, "blocked": 1, "unknown": 2, "alive": 3}[r["status"]],
        r["url"],
    ))

    step("Результаты")
    for r in results:
        icon = {"alive": "OK", "dead": "DEAD", "blocked": "403", "unknown": "??"}[r["status"]]
        color = {"alive": C.GREEN, "dead": C.RED, "blocked": C.YELLOW, "unknown": C.DIM}[r["status"]]
        hls = ""
        if r["is_hls"]:
            hls = f" [{r['hls_type']}, seg={'OK' if r['segments_ok'] else 'FAIL'}]"
        hdrs = ""
        if r["headers_needed"]:
            hdrs = f" +headers: {list(r['headers_needed'].keys())}"
        note = f" — {r['note']}" if r["note"] else ""
        print(f"{color}[{icon}]{C.RESET} {r['url']}{hls}{hdrs}{note}")

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


if __name__ == '__main__':
    main()
