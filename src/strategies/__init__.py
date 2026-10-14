"""
Strategy factory.

    from src.strategies import get_strategy, available
    strat = get_strategy("trend_follow", breakout_len=40)

New strategies: add a module with a Strategy subclass and register it in REGISTRY.
"""
from typing import Dict, List, Type

from .base import Strategy, EntrySignal, ExitSignal
from .trend_follow import TrendFollow
from .range_breakout import RangeBreakout
from .double_bottom import DoubleBottom

REGISTRY: Dict[str, Type[Strategy]] = {
    TrendFollow.name: TrendFollow,
    RangeBreakout.name: RangeBreakout,
    DoubleBottom.name: DoubleBottom,
}

DEFAULT_STRATEGY = TrendFollow.name


def available() -> List[str]:
    return sorted(REGISTRY)


def get_strategy(name: str, **params) -> Strategy:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; available: {available()}") from None
    return cls(**params)


def parse_params(items) -> dict:
    """['breakout_len=40', 'rs_min_pct=0.6'] -> {'breakout_len': 40, 'rs_min_pct': 0.6}"""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        try:
            val = int(v)
        except ValueError:
            try:
                val = float(v)
            except ValueError:
                val = {"true": True, "false": False}.get(v.lower(), v)
        out[k.strip()] = val
    return out


__all__ = ["Strategy", "EntrySignal", "ExitSignal", "REGISTRY", "DEFAULT_STRATEGY",
           "available", "get_strategy", "parse_params"]
