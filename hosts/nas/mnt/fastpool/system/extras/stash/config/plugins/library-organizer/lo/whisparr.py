"""Optional post-move rescan in Whisparr so it re-finds files at their new path."""
import requests

from .report import log


class Whisparr:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("enabled")) and bool(cfg.get("api_key")) and "{{" not in str(cfg.get("api_key"))
        self.url = (cfg.get("url") or "").rstrip("/")
        self.version = int(cfg.get("version", 2))
        self.headers = {"X-Api-Key": str(cfg.get("api_key") or "")}
        self.pending = set()

    def _call(self, method, path, **kw):
        r = requests.request(method, f"{self.url}/api/v3{path}", headers=self.headers, timeout=30, **kw)
        r.raise_for_status()
        return r.json()

    def queue(self, whisparr_id):
        if self.enabled and whisparr_id:
            self.pending.add(whisparr_id)

    def describe(self, whisparr_id):
        return f"whisparr v{self.version} id={whisparr_id}"

    def flush(self):
        """Rescan each distinct series (v2) / movie (v3) once; failures only log."""
        for wid in sorted(self.pending):
            try:
                if self.version == 2:
                    series_id = self._call("GET", f"/episode/{wid}")["seriesId"]
                    self._call("POST", "/command", json={"name": "RescanSeries", "seriesId": series_id})
                else:
                    # VERIFY: v3 (Eros) command/id names are untested on a live v3 instance
                    self._call("POST", "/command", json={"name": "RescanMovie", "movieId": wid})
            except Exception as exc:  # noqa: BLE001 - never block a move on Whisparr
                log("warning", f"whisparr rescan failed for id={wid}: {exc}")
        self.pending.clear()
