"""Send a chat request to the first free model that can take it.

`Router.chat(messages)` walks the models (your pick first, then `order`), skips any without a
key, resting, busy or at its daily_limit, and on a failure rests that model and moves on:

* 429 "per day" / daily / RPD / out of neurons -> rests until midnight in the provider's reset_tz
* 429 weekly (Ollama)                          -> rests until midnight, then tries again
* 429 session limit (Ollama)                   -> rests 1 h
* 429 per minute                               -> rests for Retry-After, or 60 s
* 5xx, timeout, network                        -> rests 30 s
* 401/403 (bad key), 404 (no such model)       -> rests 6 h
* any other 4xx                                -> no rest, next model

Rests, today's counts and your pick live in .state.json next to providers.toml, so every
process on this machine (the CLI, the proxy, your app) shares them.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from free_llm.config import HOME, Config, Model, load

HOUR = 3600


def http_request(method: str, url: str, api_key: str, payload: dict | None, timeout: float):
    """-> (status, body text, headers). Status 0 means the request never got an answer."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "free-llm/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}", {}


class AllModelsFailed(RuntimeError):
    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        lines = "; ".join(f"{a['model']} ({a['why']})" for a in attempts) or "no model is set up"
        super().__init__(f"no free model answered: {lines}")


def next_midnight(now: float, tz: str) -> float:
    local = datetime.fromtimestamp(now, ZoneInfo(tz))
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def today(now: float, tz: str) -> str:
    return datetime.fromtimestamp(now, ZoneInfo(tz)).date().isoformat()


def classify(status: int, body: str, headers: dict, now: float, tz: str) -> tuple[str, float | None]:
    """-> (why it failed, rest until when, or None for no rest)."""
    low = body.lower()
    if status == 0:
        return f"no answer: {body[:120]}", now + 30
    if status in (401, 403):
        return f"key refused ({status})", now + 6 * HOUR
    if status == 404:
        return "model not found (404)", now + 6 * HOUR
    if status in (402, 429):
        daily = ("perday", "per day", "daily", "rpd", "neurons", "insufficient", "balance", "1113")
        if any(w in low for w in daily):
            return f"daily quota spent ({status})", next_midnight(now, tz)
        if "weekly" in low:  # Ollama: tried again each midnight until the week turns
            return f"weekly limit reached ({status})", next_midnight(now, tz)
        if "session" in low and "limit" in low:  # Ollama's session limit lasts hours
            return f"session limit reached ({status})", now + HOUR
        retry = {k.lower(): v for k, v in headers.items()}.get("retry-after", "")
        wait = float(retry) if retry.replace(".", "", 1).isdigit() else 60.0
        return f"rate limited ({status})", now + min(wait, 15 * 60)
    if status == 408 or status >= 500:
        return f"provider error ({status})", now + 30
    return f"request refused ({status}): {body[:200]}", None


class State:
    """.state.json: {"pick": key|null, "rest": {key: {"until", "why"}}, "count": {key: {"day", "n"}}}"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("pick", None)
        data.setdefault("rest", {})
        data.setdefault("count", {})
        return data

    def change(self, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            data = self.read()
            fn(data)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            tmp.replace(self.path)
            return data


class Router:
    def __init__(
        self,
        config: Config | None = None,
        *,
        state_path: str | Path | None = None,
        request: Callable = http_request,
        clock: Callable[[], float] = time.time,
        timeout: float = 120.0,
    ):
        self.config = config or load()
        self.state = State(Path(state_path or os.environ.get("FREE_LLM_STATE") or HOME / ".state.json"))
        self._request = request
        self._clock = clock
        self.timeout = timeout
        self._slots = {
            name: threading.BoundedSemaphore(p.max_concurrent)
            for name, p in self.config.providers.items()
            if p.max_concurrent
        }

    # ── choosing ──────────────────────────────────────────────────────────

    def candidates(self, model: str | None = "auto") -> list[Model]:
        """One model when you name it; for "auto", your pick and then `order`."""
        if model and model != "auto":
            return [self.config.resolve(model)]
        keys = list(self.config.order)
        pick = self.state.read()["pick"]
        if pick in self.config.models:
            keys = [pick] + [k for k in keys if k != pick]
        return [self.config.models[k] for k in keys]

    def why_not(self, m: Model, state: dict | None = None) -> str | None:
        """Why this model would be skipped right now, or None when it is ready."""
        state = state or self.state.read()
        missing = self.config.missing(m.provider)
        if missing:
            return "no key: set " + ", ".join(missing) + " in .env"
        now = self._clock()
        rest = state["rest"].get(m.key)
        if rest and rest["until"] > now:
            left = rest["until"] - now
            span = f"{left / HOUR:.1f} h" if left >= HOUR else f"{left / 60:.0f} min"
            return f"resting {span}: {rest['why']}"
        if m.daily_limit and self.used_today(m, state) >= m.daily_limit:
            return f"daily_limit {m.daily_limit} reached"
        return None

    def used_today(self, m: Model, state: dict | None = None) -> int:
        state = state or self.state.read()
        c = state["count"].get(m.key)
        tz = self.config.providers[m.provider].reset_tz
        return c["n"] if c and c["day"] == today(self._clock(), tz) else 0

    def status(self) -> list[dict]:
        state = self.state.read()
        return [
            {
                "n": i,
                "model": m.key,
                "note": m.note,
                "used": self.used_today(m, state),
                "limit": m.daily_limit,
                "picked": m.key == state["pick"],
                "ready": (why := self.why_not(m, state)) is None,
                "why": why or "",
            }
            for i, m in enumerate(self.config.listed(), 1)
        ]

    def pick(self, model: str | None) -> Model | None:
        """Put a model first for "auto" (None or "auto" goes back to plain `order`)."""
        chosen = None if not model or model == "auto" else self.config.resolve(model)
        self.state.change(lambda s: s.update(pick=chosen.key if chosen else None))
        return chosen

    def reset(self, model: str | None = None) -> None:
        """Forget rests (of one model, or all)."""
        key = self.config.resolve(model).key if model else None

        def clear(s: dict) -> None:
            s["rest"] = {k: v for k, v in s["rest"].items() if key and k != key}

        self.state.change(clear)

    # ── sending ───────────────────────────────────────────────────────────

    def chat(self, messages: list[dict], model: str | None = "auto", **params: Any) -> dict:
        """An OpenAI /chat/completions reply from the first model that answers.

        `params` go into the request body as they are (max_tokens, temperature, tools, ...).
        The reply carries `x_free_llm = {"model": used, "skipped": [...]}`.
        """
        named = bool(model and model != "auto")
        params.pop("stream", None)
        tried: list[dict] = []
        for m in self.candidates(model):
            why = self.why_not(m)
            if why:
                tried.append({"model": m.key, "why": why})
                continue
            slot = self._slots.get(m.provider)
            if slot and not slot.acquire(timeout=self.timeout if named else 0.01):
                tried.append({"model": m.key, "why": "busy (one request at a time)"})
                continue
            try:
                data, why = self._send(m, messages, params)
            finally:
                if slot:
                    slot.release()
            if data is not None:
                data["x_free_llm"] = {"model": m.key, "skipped": tried}
                return data
            tried.append({"model": m.key, "why": why})
        raise AllModelsFailed(tried)

    def _send(self, m: Model, messages: list[dict], params: dict) -> tuple[dict | None, str]:
        p = self.config.providers[m.provider]
        body = {**p.extra_body, **m.extra_body, **params, "model": m.id, "messages": messages}
        status, text, headers = self._request(
            "POST", f"{self.config.url(m.provider)}/chat/completions",
            self.config.api_key(m.provider), body, self.timeout,
        )
        now = self._clock()
        data = None
        if 200 <= status < 300:
            try:
                data = json.loads(text)
            except ValueError:
                pass
            if not isinstance(data, dict) or not data.get("choices"):
                data, status, text = None, 502, f"reply without choices: {text[:200]}"
        if data is not None:
            day = today(now, p.reset_tz)

            def count(s: dict) -> None:
                c = s["count"].get(m.key)
                s["count"][m.key] = {"day": day, "n": (c["n"] + 1) if c and c["day"] == day else 1}
                s["rest"].pop(m.key, None)

            self.state.change(count)
            return data, ""

        why, until = classify(status, text, headers, now, p.reset_tz)
        if until:
            self.state.change(lambda s: s["rest"].update({m.key: {"until": until, "why": why}}))
        return None, why

    def check(self, provider: str) -> str:
        """Ask a provider for its model list: proves the key without spending chat quota."""
        missing = self.config.missing(provider)
        if missing:
            return "no key: set " + ", ".join(missing)
        status, text, _ = self._request(
            "GET", f"{self.config.url(provider)}/models", self.config.api_key(provider), None, 30
        )
        if 200 <= status < 300:
            return "key works"
        if status in (401, 403):
            return f"key refused ({status})"
        if status in (404, 405):
            return "no /models here; try `free-llm ping` (spends one request)"
        return f"answer {status}: {text[:150]}"


def text_of(reply: dict) -> str:
    return (reply["choices"][0].get("message") or {}).get("content") or ""
