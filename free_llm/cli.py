"""free-llm list | use [N|name|auto] | ask "..." | check | ping [N|name] | reset [name] | serve"""

from __future__ import annotations

import argparse
import sys

from free_llm.router import AllModelsFailed, Router, text_of


def show(router: Router) -> None:
    rows = router.status()
    width = max(len(r["model"]) for r in rows)
    for r in rows:
        mark = "*" if r["picked"] else " "
        state = "ready" if r["ready"] else r["why"]
        used = f"{r['used']}/{r['limit']}" if r["limit"] else f"{r['used']}"
        print(f"{mark}{r['n']:>2}  {r['model']:<{width}}  today {used:<9} {state:<34} {r['note']}")
    print("\n* = your pick (tried first by auto).  free-llm use N  to pick,  use auto  to clear.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="free-llm", description="Free LLMs with automatic fallback.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="models, keys, today's use, rests")
    p = sub.add_parser("use", help="pick the model auto tries first (no argument: choose from the list)")
    p.add_argument("model", nargs="?")
    p = sub.add_parser("ask", help="send one question")
    p.add_argument("text")
    p.add_argument("-m", "--model", default="auto")
    sub.add_parser("check", help="test each provider's key (free: no chat request)")
    p = sub.add_parser("models", help="a provider's live models vs providers.toml (free)")
    p.add_argument("provider")
    p = sub.add_parser("ping", help="send 'Say OK' to one model (spends one request)")
    p.add_argument("model")
    p = sub.add_parser("reset", help="forget rests (of one model, or all)")
    p.add_argument("model", nargs="?")
    p = sub.add_parser("serve", help="run the OpenAI-compatible endpoint")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)

    router = Router()
    try:
        if args.cmd in (None, "list"):
            show(router)
        elif args.cmd == "use":
            name = args.model
            if not name:
                show(router)
                name = input("\nnumber (or auto): ").strip()
            chosen = router.pick(name)
            print(f"auto now starts with {chosen.key}" if chosen else "auto follows `order` again")
        elif args.cmd == "ask":
            reply = router.chat([{"role": "user", "content": args.text}], model=args.model)
            info = reply["x_free_llm"]
            for s in info["skipped"]:
                print(f"  skipped {s['model']}: {s['why']}", file=sys.stderr)
            print(f"[{info['model']}]", file=sys.stderr)
            print(text_of(reply))
        elif args.cmd == "check":
            for name in router.config.providers:
                print(f"{name:<12} {router.check(name)}")
        elif args.cmd == "models":
            if args.provider not in router.config.providers:
                raise KeyError(f"no provider {args.provider!r}; have: {', '.join(router.config.providers)}")
            live = set(router.live_models(args.provider))
            mine = {m.id for m in router.config.models.values() if m.provider == args.provider}
            for mid in sorted(live | mine):
                mark = "  " if mid in live and mid in mine else ("+ " if mid in live else "- ")
                print(f"{mark}{mid}")
            print("\n+ offered, not in providers.toml (may be paid-only: `free-llm ping` it)"
                  "\n- in providers.toml, no longer offered")
        elif args.cmd == "ping":
            reply = router.chat([{"role": "user", "content": "Say OK"}], model=args.model,
                                max_tokens=200)
            print(f"{reply['x_free_llm']['model']}: {text_of(reply).strip()[:80]!r}")
        elif args.cmd == "reset":
            router.reset(args.model)
            print("rests cleared")
        elif args.cmd == "serve":
            from free_llm.server import serve

            serve(router, args.host, args.port, router.config.env.get("FREE_LLM_PROXY_KEY"))
    except KeyError as e:
        print(str(e).strip("'\""), file=sys.stderr)
        return 2
    except AllModelsFailed as e:
        print("No free model answered:", file=sys.stderr)
        for a in e.attempts:
            print(f"  {a['model']}: {a['why']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
