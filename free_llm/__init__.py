"""Free LLM providers behind one call, with automatic fallback.

    from free_llm import Router, text_of
    reply = Router().chat([{"role": "user", "content": "Hi"}])          # auto
    reply = Router().chat(messages, model="groq:openai/gpt-oss-120b")   # this one only
    print(text_of(reply), reply["x_free_llm"]["model"])
"""

from free_llm.config import HOME, Config, Model, load
from free_llm.router import AllModelsFailed, Router, text_of

__all__ = ["HOME", "AllModelsFailed", "Config", "Model", "Router", "load", "text_of"]
