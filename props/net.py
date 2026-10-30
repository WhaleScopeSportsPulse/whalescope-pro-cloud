"""Small JSON GET helper with an in-memory TTL cache (stdlib only)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "WhaleScopeProps/1.0 (+https://github.com/whalescopesportspulse/whalescope-pro-cloud)"
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
# Minimum seconds between requests to one host, so a full slate refresh
# stays under public rate limits.
MIN_INTERVAL = {"api.elections.kalshi.com": 0.4}  # Kalshi's public limit is shared with Signal Lab
_next_slot: dict[str, float] = {}


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    gap = MIN_INTERVAL.get(host)
    if not gap:
        return
    with _lock:
        now = time.time()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + gap
    if slot > now:
        time.sleep(slot - now)


def get_json(url: str, params: dict | None = None, ttl: float = 0, timeout: float = 15) -> object:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    now = time.time()
    if ttl:
        with _lock:
            hit = _cache.get(url)
            if hit and hit[0] > now:
                return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(5):
        _throttle(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                continue
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(2 ** attempt)
    else:
        raise last  # type: ignore[misc]
    if ttl:
        _store(url, now + ttl, data)
    return data


def _store(key: str, expires: float, value: object) -> None:
    with _lock:
        _cache[key] = (expires, value)
        if len(_cache) % 500 == 0:  # drop expired entries now and then
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                del _cache[k]


def memo(key: str, ttl: float, fn):
    """Cache fn()'s result under key for ttl seconds (shares get_json's cache)."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fn()
    _store(key, now + ttl, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()
