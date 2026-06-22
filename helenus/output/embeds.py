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
import pandas as pd

from helenus.config import CONFIG
from helenus.data.schwab_feed import now_et
from helenus.engine.flow import VannaReading, VolumeProfile
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


def _fmt_vol(v: float) -> str:
    """Human-scale contract volume: 18.0k / 740."""
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"


def _flow_line(vp: VolumeProfile, vanna: VannaReading | None) -> str:
    """One-line options-flow summary for signal embeds."""
    parts = [
        f"OTM C/P `{vp.otm_call_put_ratio:.2f}x`",
        f"Above `{_fmt_vol(vp.above_spot_vol)}` / Below `{_fmt_vol(vp.below_spot_vol)}`",
    ]
    if vanna is not None:
        tag = "🟢 " if vanna.active else ""
        parts.append(f"{tag}Vanna: `{vanna.label}` (VIX {vanna.vix_change:+.2f})")
    return " | ".join(parts)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def signal_embed(
    sig: Signal,
    gex: GexProfile | None = None,
    vol_profile: VolumeProfile | None = None,
    vanna: VannaReading | None = None,
) -> discord.Embed:
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
    if vol_profile is not None:
        embed.add_field(
            name="Options Flow", value=_flow_line(vol_profile, vanna), inline=False
        )
    if sig.notes:
        embed.add_field(name="Read", value="\n".join(f"• {n}" for n in sig.notes), inline=False)

    embed.set_footer(text=_footer())
    return embed


def volume_profile_embed(
    vp: VolumeProfile, vanna: VannaReading | None = None
) -> discord.Embed:
    """
    0DTE options-volume ladder: an ASCII histogram of per-strike volume around
    spot, so you can *see* where the distribution sits above vs below price.
    Greys out unless a vanna setup is live (then green).
    """
    active = vanna is not None and vanna.active
    embed = discord.Embed(
        title="$SPXW 0DTE — Options Volume Ladder",
        color=COLOR_BULL if active else COLOR_INFO,
        timestamp=now_et(),
    )

    bs = vp.by_strike
    n = CONFIG.flow.ladder_strikes
    if not bs.empty:
        above = bs[bs.index > vp.spot].sort_index().head(n).sort_index(ascending=False)
        below = bs[bs.index < vp.spot].sort_index(ascending=False).head(n)
        window = pd.concat([above, below])
        max_vol = float(window["total"].max()) or 1.0
        width = 12

        def _bar(total: float) -> str:
            fill = int(round(total / max_vol * width))
            return "█" * fill + "░" * (width - fill)

        def _row(strike: float, row) -> str:
            total = float(row.total)
            cv, pv = float(row.call_volume), float(row.put_volume)
            side = "C" if cv >= pv else "P"
            return f"{strike:>7.0f} {side} {_bar(total)} {_fmt_vol(total):>6}"

        lines = [_row(s, r) for s, r in above.iterrows()]
        lines.append(f"{'─' * 9} spot {vp.spot:.2f} {'─' * 9}")
        lines += [_row(s, r) for s, r in below.iterrows()]
        embed.description = "```\n" + "\n".join(lines) + "\n```"
    else:
        embed.description = "_No 0DTE volume yet._"

    embed.add_field(
        name="Above spot", value=f"`{_fmt_vol(vp.above_spot_vol)}`", inline=True
    )
    embed.add_field(
        name="Below spot", value=f"`{_fmt_vol(vp.below_spot_vol)}`", inline=True
    )
    embed.add_field(
        name="Call/Put", value=f"`{vp.call_put_ratio:.2f}x`", inline=True
    )
    embed.add_field(
        name="OTM calls / ITM calls",
        value=f"`{_fmt_vol(vp.otm_call_vol)}` / `{_fmt_vol(vp.itm_call_vol)}`",
        inline=True,
    )
    embed.add_field(
        name="OTM puts / ITM puts",
        value=f"`{_fmt_vol(vp.otm_put_vol)}` / `{_fmt_vol(vp.itm_put_vol)}`",
        inline=True,
    )
    embed.add_field(
        name="OTM C/P", value=f"`{vp.otm_call_put_ratio:.2f}x`", inline=True
    )
    if vanna is not None:
        embed.add_field(name="Vanna", value=f"`{vanna.label}` — {vanna.note}", inline=False)

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


def review_embed(review: dict, stats: dict) -> discord.Embed:
    """Accuracy review: deterministic MFE/MAE stats + Claude's pattern read."""
    acc = stats.get("accuracy_pct", 0.0)
    embed = discord.Embed(
        title="🔁 Accuracy Review — scan2 feedback loop",
        description=review.get("summary", ""),
        color=COLOR_BULL if acc >= 50 else COLOR_BEAR if acc < 35 else COLOR_INFO,
        timestamp=now_et(),
    )
    embed.add_field(
        name="Scorecard",
        value=(
            f"`{stats.get('count', 0)}` graded | "
            f"**{acc:.0f}%** accurate "
            f"(`{stats.get('accurate', 0)}`✓ / `{stats.get('mixed', 0)}`~ / "
            f"`{stats.get('inaccurate', 0)}`✗)\n"
            f"Avg MFE `{stats.get('avg_mfe_pts', 0)}` / "
            f"MAE `{stats.get('avg_mae_pts', 0)}` pts | "
            f"ratio `{stats.get('avg_ratio', 0)}`"
        ),
        inline=False,
    )

    def _bullets(items: list[str], limit: int = 5) -> str:
        return "\n".join(f"• {x}" for x in items[:limit]) or "—"

    if review.get("accurate_patterns"):
        embed.add_field(
            name="What works", value=_bullets(review["accurate_patterns"]), inline=False
        )
    if review.get("inaccurate_patterns"):
        embed.add_field(
            name="What misses", value=_bullets(review["inaccurate_patterns"]), inline=False
        )
    if review.get("suggestions"):
        embed.add_field(
            name="Tune", value=_bullets(review["suggestions"]), inline=False
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
