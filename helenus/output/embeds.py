"""
Discord presentation layer. Signals and GEX profiles in, discord.Embed out.

Color discipline is strict and structural:
    Green  = bullish configuration
    Red    = bearish configuration
    Greyed = informational (GEX snapshots with no directional claim)
"""

from __future__ import annotations

import datetime as dt

import discord

from helenus.data.schwab_feed import now_et
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Direction, Signal

COLOR_BULL = discord.Color.from_rgb(46, 204, 113)
COLOR_BEAR = discord.Color.from_rgb(231, 76, 60)
COLOR_INFO = discord.Color.from_rgb(149, 165, 166)

FOOTER_TEMPLATE = (
    "{time} ET | scan2 (mechanical) | Helenus  reads. You aim. | Not financial advice."
)


def _footer(ts: dt.datetime | None = None) -> str:
    ts = ts or now_et()
    return FOOTER_TEMPLATE.format(time=ts.strftime("%H:%M:%S"))


def _color(direction: Direction) -> discord.Color:
    return COLOR_BULL if direction is Direction.BULLISH else COLOR_BEAR


def _fmt_gex(value: float) -> str:
    """Human-scale dollars: $1.24B / $310M."""
    a = abs(value)
    if a >= 1e9:
        return f"${value / 1e9:.2f}B"
    if a >= 1e6:
        return f"${value / 1e6:.0f}M"
    return f"${value / 1e3:.0f}K"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def signal_embed(sig: Signal, gex: GexProfile | None = None) -> discord.Embed:
    arrow = "▲" if sig.direction is Direction.BULLISH else "▼"
    embed = discord.Embed(
        title=f"{arrow} {sig.trigger.value} — {sig.trend_label}",
        color=_color(sig.direction),
        timestamp=now_et(),
    )
    embed.add_field(name="Spot", value=f"{sig.spot:.2f}", inline=True)
    if sig.level is not None:
        embed.add_field(
            name="Level", value=f"{sig.level.label} ({sig.level.price:.2f})", inline=True
        )
    if sig.volume_ratio == sig.volume_ratio:  # not NaN
        embed.add_field(name="Volume", value=f"{sig.volume_ratio:.1f}x 20-MA", inline=True)
    embed.add_field(name="Confidence", value=f"{sig.confidence:.0f}%", inline=True)

    if gex is not None:
        zg = f"{gex.zero_gamma:.0f}" if gex.zero_gamma is not None else "n/a"
        embed.add_field(
            name="Gamma Context",
            value=f"Regime: `{gex.regime}` | Zero-Γ: `{zg}` | Net: `{_fmt_gex(gex.total_net_gex)}`",
            inline=False,
        )
    if sig.notes:
        embed.add_field(name="Read", value="\n".join(f"• {n}" for n in sig.notes), inline=False)

    embed.set_footer(text=_footer())
    return embed


def gex_snapshot_embed(gex: GexProfile) -> discord.Embed:
    """Informational $SPXW structure board (no directional claim => grey)."""
    embed = discord.Embed(
        title="$SPXW 0DTE — Gamma Structure",
        color=COLOR_INFO,
        timestamp=now_et(),
    )
    zg = f"{gex.zero_gamma:.0f}" if gex.zero_gamma is not None else "n/a"
    embed.add_field(name="Spot", value=f"{gex.spot:.2f}", inline=True)
    embed.add_field(name="Zero Gamma", value=zg, inline=True)
    embed.add_field(name="Net GEX", value=_fmt_gex(gex.total_net_gex), inline=True)

    if gex.call_walls:
        embed.add_field(
            name="Call Walls (resistance)",
            value="\n".join(f"`{k:.0f}` — {_fmt_gex(v)}" for k, v in gex.call_walls),
            inline=True,
        )
    if gex.put_walls:
        embed.add_field(
            name="Put Walls (support)",
            value="\n".join(f"`{k:.0f}` — {_fmt_gex(v)}" for k, v in gex.put_walls),
            inline=True,
        )
    embed.set_footer(text=_footer())
    return embed


def premarket_embed(sig: Signal, macro_lines: list[str] | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"☀ Pre-Market Briefing — {sig.trend_label}",
        color=_color(sig.direction),
        timestamp=now_et(),
    )
    embed.add_field(name="Confidence", value=f"{sig.confidence:.0f}%", inline=True)
    embed.add_field(name="Overnight Read", value="\n".join(f"• {n}" for n in sig.notes), inline=False)
    if macro_lines:
        embed.add_field(name="Macro Board", value="\n".join(macro_lines), inline=False)
    embed.set_footer(text=_footer())
    return embed
