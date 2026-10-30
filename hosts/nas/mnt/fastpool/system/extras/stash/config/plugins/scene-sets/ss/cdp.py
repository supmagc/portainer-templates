"""Load a page in the headless Chrome Stash's scrapers use (chromedp) and evaluate JavaScript in it — for sites
behind a bot check that plain requests can't pass (the Kink scraper does the same via `useCDP: true`).

Every page gets its own throwaway browser context (like an incognito window): cookies set for it — the age gate,
a member session — never land in chromedp's shared profile, which Stash's scrapers use too."""
import itertools
import json
import time

import requests
import websocket

# Chrome's DevTools endpoint only accepts a Host header that is "localhost" or an IP
HOST_HEADER = {"Host": "localhost"}


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, base, sleep=time.sleep):
        self.base = base.rstrip("/")
        self.sleep = sleep

    @classmethod
    def from_setting(cls, url):
        """Accepts Stash's scraperCDPPath form (http://chromedp:9222/json/version) or the bare base URL."""
        url = (url or "").strip()
        if not url.startswith("http"):
            raise CDPError(f"no usable Chrome DevTools URL ({url or 'not set'}); set Stash's Chrome CDP path or cdp_url")
        return cls(url.split("/json", 1)[0])

    def evaluate(self, url, expression, ready="true", cookies=(), timeout=25):
        """Open url in a fresh browser context (cookies set first), wait until `ready` is truthy or timeout, return
        the value of `expression`."""
        version = requests.get(f"{self.base}/json/version", headers=HOST_HEADER, timeout=30).json()
        path = "/devtools/" + version["webSocketDebuggerUrl"].split("/devtools/", 1)[1]
        ws = websocket.create_connection(self.base.replace("http", "ws", 1) + path, timeout=60,
                                         host="localhost", suppress_origin=True)
        conn = _Conn(ws)
        try:
            context = conn.call("Target.createBrowserContext", disposeOnDetach=True)["browserContextId"]
            try:
                if cookies:
                    conn.call("Storage.setCookies", cookies=list(cookies), browserContextId=context)
                target = conn.call("Target.createTarget", url="about:blank", browserContextId=context)["targetId"]
                page = conn.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
                conn.call("Page.navigate", page, url=url)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    self.sleep(0.5)
                    if conn.eval(page, f"document.readyState === 'complete' && !!({ready})"):
                        break
                return conn.eval(page, expression)
            finally:
                conn.call("Target.disposeBrowserContext", browserContextId=context)
        finally:
            ws.close()


class _Conn:
    """One DevTools websocket; commands go to the browser, or to a page with its flat-mode session id."""

    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)

    def call(self, method, session=None, **params):
        n = next(self.ids)
        msg = {"id": n, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:  # skip events until our answer arrives
            reply = json.loads(self.ws.recv())
            if reply.get("id") == n:
                if "error" in reply:
                    raise CDPError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result") or {}

    def eval(self, session, expression):
        res = self.call("Runtime.evaluate", session, expression=expression, returnByValue=True)
        if res.get("exceptionDetails"):
            raise CDPError(f"script error: {res['exceptionDetails'].get('text')}")
        return (res.get("result") or {}).get("value")
