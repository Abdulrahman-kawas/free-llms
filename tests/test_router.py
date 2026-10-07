"""Offline: a fake provider answers instead of the network."""

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from free_llm.config import load
from free_llm.router import AllModelsFailed, Router, classify, next_midnight, text_of
from free_llm.server import ThreadingHTTPServer, make_handler

TOML = """
order = ["a:m1", "a:m2", "b:x"]
[providers.a]
base_url = "https://a.test/v1"
key_env = "A_KEY"
extra_body = { reasoning_effort = "minimal" }
models = [ { id = "m1", daily_limit = 2 }, { id = "m2" } ]
[providers.b]
base_url = "https://b.test/{B_ACCOUNT}/v1"
key_env = "B_KEY"
max_concurrent = 1
models = [ { id = "x", extra_body = { reasoning_effort = "low" } } ]
"""
NOW = 1_790_000_000.0


def ok(text="hi"):
    return 200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}), {}


class Fake:
    """Answers per model id from a script; records every request."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, method, url, key, payload, timeout):
        self.calls.append((url, key, payload))
        answer = self.script[payload["model"]]
        return answer.pop(0) if isinstance(answer, list) else answer


@pytest.fixture
def make(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FREE_LLM_STATE", raising=False)
    (tmp_path / "p.toml").write_text(TOML)

    def build(script, env="A_KEY=ka\nB_KEY=kb\nB_ACCOUNT=acc\n", clock=lambda: NOW):
        (tmp_path / ".env").write_text(env)
        cfg = load(tmp_path / "p.toml", tmp_path / ".env")
        fake = Fake(script)
        return Router(cfg, state_path=tmp_path / "s.json", request=fake, clock=clock), fake

    return build


def test_first_model_answers_with_its_extra_body(make):
    r, fake = make({"m1": ok("yo")})
    reply = r.chat([{"role": "user", "content": "q"}], temperature=0)
    assert text_of(reply) == "yo" and reply["x_free_llm"]["model"] == "a:m1"
    url, key, body = fake.calls[0]
    assert url == "https://a.test/v1/chat/completions" and key == "ka"
    assert body["reasoning_effort"] == "minimal" and body["temperature"] == 0


def test_daily_quota_falls_back_and_rests_until_midnight(make):
    r, fake = make({"m1": (429, "Quota exceeded ...PerDayPerProject...", {}), "m2": ok()})
    reply = r.chat([{"role": "user", "content": "q"}])
    assert reply["x_free_llm"]["model"] == "a:m2"
    assert reply["x_free_llm"]["skipped"][0]["why"].startswith("daily quota")
    rest = r.state.read()["rest"]["a:m1"]
    assert rest["until"] == next_midnight(NOW, "UTC")
    r.chat([{"role": "user", "content": "q"}])  # m1 is skipped without a request now
    assert [c[2]["model"] for c in fake.calls] == ["m1", "m2", "m2"]


def test_per_minute_rest_uses_retry_after(make):
    r, _ = make({"m1": (429, "too many per minute", {"Retry-After": "7"}), "m2": ok()})
    r.chat([{"role": "user", "content": "q"}])
    assert r.state.read()["rest"]["a:m1"]["until"] == NOW + 7


def test_daily_limit_is_counted_here(make):
    r, fake = make({"m1": ok(), "m2": ok()})
    for _ in range(3):
        r.chat([{"role": "user", "content": "q"}])
    assert [c[2]["model"] for c in fake.calls] == ["m1", "m1", "m2"]
    assert r.used_today(r.config.models["a:m1"]) == 2


def test_missing_key_or_url_var_is_skipped(make):
    r, fake = make({"x": ok("from b")}, env="B_KEY=kb\n")
    with pytest.raises(AllModelsFailed) as e:
        r.chat([{"role": "user", "content": "q"}])
    whys = [a["why"] for a in e.value.attempts]
    assert whys[0] == "no key: set A_KEY in .env" and "B_ACCOUNT" in whys[2]
    assert fake.calls == []


def test_url_var_is_filled_and_model_extra_body_wins(make):
    r, fake = make({"x": ok()})
    r.chat([{"role": "user", "content": "q"}], model="b:x")
    url, _, body = fake.calls[0]
    assert url == "https://b.test/acc/v1/chat/completions" and body["reasoning_effort"] == "low"


def test_named_model_does_not_fall_back(make):
    r, fake = make({"m2": (500, "boom", {}), "m1": ok()})
    with pytest.raises(AllModelsFailed):
        r.chat([{"role": "user", "content": "q"}], model="m2")
    assert len(fake.calls) == 1


def test_pick_goes_first_then_order(make):
    r, fake = make({"x": (401, "bad key", {}), "m1": ok()})
    r.pick("3")  # number from `free-llm list`
    reply = r.chat([{"role": "user", "content": "q"}])
    assert reply["x_free_llm"]["model"] == "a:m1"
    assert [c[2]["model"] for c in fake.calls] == ["x", "m1"]
    assert "key refused" in r.status()[2]["why"]
    r.reset()
    assert r.status()[2]["ready"] and r.status()[2]["picked"]


def test_bad_request_moves_on_without_rest(make):
    r, _ = make({"m1": (400, "tools not supported", {}), "m2": ok()})
    r.chat([{"role": "user", "content": "q"}])
    assert "a:m1" not in r.state.read()["rest"]


def test_busy_provider_is_skipped_by_auto(make):
    r, fake = make({"x": ok()})
    r.pick("b:x")
    r._slots["b"].acquire()  # someone else holds Z AI's single slot
    fake.script.update({"m1": ok("a")})
    reply = r.chat([{"role": "user", "content": "q"}])
    assert reply["x_free_llm"]["model"] == "a:m1"
    assert reply["x_free_llm"]["skipped"][0]["why"].startswith("busy")


def test_classify():
    assert classify(429, "Rate limit reached on requests per day (RPD)", {}, NOW, "UTC")[0].startswith("daily")
    assert classify(429, "used up your daily free allocation of 10,000 neurons", {}, NOW, "UTC")[0].startswith("daily")
    assert classify(429, "you've reached your weekly usage limit", {}, NOW, "UTC")[0].startswith("weekly")
    assert classify(429, "you've reached your session usage limit", {}, NOW, "UTC")[1] == NOW + 3600
    assert classify(503, "", {}, NOW, "UTC") == ("provider error (503)", NOW + 30)
    assert classify(0, "URLError", {}, NOW, "UTC")[1] == NOW + 30
    assert classify(422, "bad", {}, NOW, "UTC")[1] is None


def test_real_config_loads():
    cfg = load(Path(__file__).parent.parent / "providers.toml", "/nonexistent")
    assert cfg.order[0] == "gemini:gemini-3.6-flash"
    assert {"gemini", "groq", "zai", "cloudflare", "ollama"} <= set(cfg.providers)
    # Ollama names carry ":tag"; the provider prefix still resolves, and so does the bare name
    assert cfg.resolve("ollama:gpt-oss:120b") is cfg.resolve("gpt-oss:120b")
    assert cfg.models["ollama:gpt-oss:120b"].provider == "ollama"


def test_proxy_speaks_openai(make):
    r, _ = make({"m1": ok("proxied")})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(r, "secret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def post(body, key="secret"):
        req = urllib.request.Request(f"{base}/v1/chat/completions", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read().decode(), res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    try:
        status, body, headers = post({"model": "auto", "messages": [{"role": "user", "content": "q"}]})
        assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "proxied"
        assert headers["X-Free-LLM-Model"] == "a:m1"
        status, body, _ = post({"model": "auto", "stream": True,
                                "messages": [{"role": "user", "content": "q"}]})
        assert '"content": "proxied"' in body and body.rstrip().endswith("data: [DONE]")
        assert post({"messages": []}, key="wrong")[0] == 401
        assert post({"model": "nope", "messages": []})[0] == 404
    finally:
        httpd.shutdown()
