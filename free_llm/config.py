"""Read providers.toml (what exists) and .env (the keys).

The folder that holds both is FREE_LLM_HOME, or this package's parent folder. A project can
bring its own model list with FREE_LLM_CONFIG=/path/to/providers.toml. A variable already in
the environment wins over the same one in .env, so a server can set keys its own way.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("FREE_LLM_HOME") or Path(__file__).resolve().parent.parent)
_VAR = re.compile(r"\{([A-Z0-9_]+)\}")


@dataclass
class Model:
    provider: str
    id: str
    daily_limit: int | None = None
    note: str = ""
    extra_body: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"


@dataclass
class Provider:
    name: str
    base_url: str
    key_env: str
    reset_tz: str = "UTC"
    max_concurrent: int | None = None
    extra_body: dict = field(default_factory=dict)


@dataclass
class Config:
    providers: dict[str, Provider]
    models: dict[str, Model]  # "provider:id" -> Model, in file order
    order: list[str]
    env: dict[str, str]

    def api_key(self, provider: str) -> str | None:
        return self.env.get(self.providers[provider].key_env) or None

    def url(self, provider: str) -> str | None:
        """The base URL with its {VARS} filled, or None while one of them is missing."""
        base = self.providers[provider].base_url
        names = _VAR.findall(base)
        if any(not self.env.get(n) for n in names):
            return None
        return _VAR.sub(lambda m: self.env[m.group(1)], base).rstrip("/")

    def missing(self, provider: str) -> list[str]:
        """The .env variables this provider still needs."""
        p = self.providers[provider]
        wanted = [p.key_env, *_VAR.findall(p.base_url)]
        return [n for n in wanted if not self.env.get(n)]

    def resolve(self, name: str) -> Model:
        """A model by "provider:id", by its bare id, or by its number in `free-llm list`."""
        name = name.strip()
        if name in self.models:
            return self.models[name]
        if name.isdigit():
            listed = self.listed()
            if 1 <= int(name) <= len(listed):
                return listed[int(name) - 1]
        hits = [m for m in self.models.values() if m.id == name]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise KeyError(f"{name!r} is on several providers, say which: "
                           + ", ".join(m.key for m in hits))
        raise KeyError(f"no model {name!r}; see `free-llm list`")

    def listed(self) -> list[Model]:
        """The models in `order` first, then the rest, as `free-llm list` numbers them."""
        rest = [m for k, m in self.models.items() if k not in self.order]
        return [self.models[k] for k in self.order] + rest


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        env[name.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return env


def load(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Config:
    config_path = Path(config_path or os.environ.get("FREE_LLM_CONFIG") or HOME / "providers.toml")
    env_path = Path(env_path or HOME / ".env")
    raw = tomllib.loads(config_path.read_text())

    providers: dict[str, Provider] = {}
    models: dict[str, Model] = {}
    for name, p in raw.get("providers", {}).items():
        providers[name] = Provider(
            name=name,
            base_url=p["base_url"],
            key_env=p["key_env"],
            reset_tz=p.get("reset_tz", "UTC"),
            max_concurrent=p.get("max_concurrent"),
            extra_body=p.get("extra_body", {}),
        )
        for m in p.get("models", []):
            model = Model(
                provider=name,
                id=m["id"],
                daily_limit=m.get("daily_limit"),
                note=m.get("note", ""),
                extra_body=m.get("extra_body", {}),
            )
            models[model.key] = model

    order = list(raw.get("order") or models)
    unknown = [k for k in order if k not in models]
    if unknown:
        raise ValueError(f"{config_path}: `order` names models that are not listed: {unknown}")

    env = {**read_env(env_path), **os.environ}
    return Config(providers=providers, models=models, order=order, env=env)
