"""Polite HTTP: per-host delay with jitter, retries with backoff, block detection."""
import random
import time
from urllib.parse import urlsplit

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
RETRY_STATUS = {429, 500, 502, 503, 504}


class Blocked(Exception):
    """The site answers with a bot challenge or keeps refusing: stop using it for this run."""


class Fetcher:
    def __init__(self, delay=2.0, attempts=4, sleep=time.sleep, clock=time.monotonic):
        self.delay = delay
        self.attempts = attempts
        self.sleep = sleep
        self.clock = clock
        self.last = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    def _wait(self, host):
        gap = self.delay * random.uniform(0.7, 1.3)
        due = self.last.get(host, 0) + gap
        now = self.clock()
        if due > now:
            self.sleep(due - now)
        self.last[host] = self.clock()

    def request(self, method, url, referer=None, missing_ok=False, **kw):
        host = urlsplit(url).hostname
        headers = kw.pop("headers", {})
        if referer:
            headers["Referer"] = referer
        for attempt in range(1, self.attempts + 1):
            self._wait(host)
            try:
                r = self.session.request(method, url, headers=headers, timeout=60, **kw)
            except requests.RequestException:
                if attempt == self.attempts:
                    raise
                self.sleep(self.delay * 2 ** attempt)
                continue
            if r.status_code == 404 and missing_ok:
                return None
            if r.status_code in (403, 503) and ("Just a moment" in r.text[:5000] or "cf-chl" in r.text[:20000]):
                raise Blocked(f"{host}: bot challenge")
            if r.status_code in RETRY_STATUS and attempt < self.attempts:
                wait = r.headers.get("Retry-After")
                self.sleep(float(wait) if wait and wait.isdigit() else self.delay * 2 ** attempt * 2)
                continue
            if r.status_code == 429:
                raise Blocked(f"{host}: rate limited")
            r.raise_for_status()
            return r
        raise Blocked(f"{host}: no answer")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)
