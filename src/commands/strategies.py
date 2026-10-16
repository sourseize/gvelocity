"""velocity strategies — list the registered strategies, their parameters and warm-up."""
import textwrap

from ..strategies import available, REGISTRY, DEFAULT_STRATEGY

HELP = "List the available strategies, their parameters and warm-up"
NEEDS_DB = False


def configure(p) -> None:
    p.add_argument("--params-only", action="store_true", help="One line per strategy: name and parameters")


def render() -> str:
    wrap = lambda text, indent: textwrap.fill(text, width=96, initial_indent=indent, subsequent_indent=indent)
    out = [f"Strategies ({len(available())} registered, default: {DEFAULT_STRATEGY})", ""]
    for i, name in enumerate(available(), 1):
        cls = REGISTRY[name]
        out.append(f"{i:2d}. {name}  —  {cls.label}{'  (default)' if name == DEFAULT_STRATEGY else ''}")
        out.append(wrap(cls.description, "      "))
        out.append(f"      warm-up: {cls().warmup()} bars · time stop: {cls.defaults.get('time_stop_days', cls.time_stop_days)} d")
        out.append(wrap("params: " + "  ".join(f"{k}={v}" for k, v in cls.defaults.items()), "      "))
        out.append("")
    out.append("Override a parameter with --param key=value (repeatable), e.g. velocity serve --param breakout_len=40")
    return "\n".join(out)


def run(args) -> int:
    if args.params_only:
        for name in available():
            print(f"{name}: " + " ".join(f"{k}={v}" for k, v in REGISTRY[name].defaults.items()))
    else:
        print(render())
    return 0
