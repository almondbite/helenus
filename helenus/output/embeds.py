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


# Discord's hard limits on embed parts. A verdict that exceeds them gets the
# whole send rejected with 400 Invalid Form Body, so every free-text value that
# Claude can fill is clipped to fit. (Field 1024 / description 4096 / name 256.)
_FIELD_VALUE_MAX = 1024
_DESC_MAX = 4096
_FIELD_NAME_MAX = 256
_TOTAL_MAX = 6000


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _field(embed: discord.Embed, name: str, value: str, inline: bool = False) -> None:
    """add_field that clips name/value to Discord's limits (and never sends an
    empty value, which also 400s)."""
    embed.add_field(
        name=_clip(name, _FIELD_NAME_MAX),
        value=_clip(value, _FIELD_VALUE_MAX) or "—",
        inline=inline,
    )


def _fit(embed: discord.Embed) -> discord.Embed:
    """Final safety net: even with every part individually within its limit, the
    whole embed must stay under 6000 chars. If it's over (many long fields), trim
    the description to make room rather than let Discord 400 the send."""
    over = len(embed) - _TOTAL_MAX
    if over > 0 and embed.description:
        embed.description = _clip(embed.description, max(0, len(embed.description) - over))
    return embed


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
        # The thesis + risk flags live in the description (4096) rather than a
        # field (1024) — Claude's read routinely runs longer than a field allows.
        description=_clip("\n".join(f"• {n}" for n in sig.notes), _DESC_MAX) or None,
        color=_color(sig.direction),
        timestamp=now_et(),
    )
    _field(embed, "Spot", f"{sig.spot:.2f}", inline=True)
    if sig.level is not None:
        _field(embed, "Level", f"{sig.level.label} ({sig.level.price:.2f})", inline=True)
    if sig.volume_ratio == sig.volume_ratio:  # not NaN
        _field(embed, "Volume", f"{sig.volume_ratio:.1f}x 20-MA", inline=True)
    _field(embed, "Confidence", f"{sig.confidence:.0f}%", inline=True)

    if gex is not None:
        zg = f"{gex.zero_gamma:.0f}" if gex.zero_gamma is not None else "n/a"
        _field(
            embed,
            "Gamma Context",
            f"Regime: `{gex.regime}` | Zero-Γ: `{zg}` | Net: `{_fmt_gex(gex.total_net_gex)}`",
        )
    if vol_profile is not None:
        _field(embed, "Options Flow", _flow_line(vol_profile, vanna))

    embed.set_footer(text=_footer())
    return _fit(embed)


def volume_profile_embed(
    vp: VolumeProfile, vanna: VannaReading | None = None
) -> discord.Embed:
    """
    0DTE options-volume summary: where call/put volume sits relative to spot
    (ITM/OTM split, above vs below). Greys out unless a vanna setup is live.
    """
    active = vanna is not None and vanna.active
    embed = discord.Embed(
        title="$SPXW 0DTE — Options Volume",
        color=COLOR_BULL if active else COLOR_INFO,
        timestamp=now_et(),
    )
    if vp.by_strike.empty:
        embed.description = "_No 0DTE volume yet._"

    _field(embed, "Spot", f"{vp.spot:.2f}", inline=True)
    _field(embed, "Above spot", f"`{_fmt_vol(vp.above_spot_vol)}`", inline=True)
    _field(embed, "Below spot", f"`{_fmt_vol(vp.below_spot_vol)}`", inline=True)
    _field(embed, "Call/Put", f"`{vp.call_put_ratio:.2f}x`", inline=True)
    _field(
        embed,
        "OTM calls / ITM calls",
        f"`{_fmt_vol(vp.otm_call_vol)}` / `{_fmt_vol(vp.itm_call_vol)}`",
        inline=True,
    )
    _field(
        embed,
        "OTM puts / ITM puts",
        f"`{_fmt_vol(vp.otm_put_vol)}` / `{_fmt_vol(vp.itm_put_vol)}`",
        inline=True,
    )
    _field(embed, "OTM C/P", f"`{vp.otm_call_put_ratio:.2f}x`", inline=True)
    if vanna is not None:
        _field(embed, "Vanna", f"`{vanna.label}` — {vanna.note}")

    embed.set_footer(text=_footer())
    return _fit(embed)


def gex_snapshot_embed(gex: GexProfile) -> discord.Embed:
    """Informational $SPXW structure board (no directional claim => grey)."""
    embed = discord.Embed(
        title="$SPXW 0DTE — Gamma Structure",
        color=COLOR_INFO,
        timestamp=now_et(),
    )
    zg = f"{gex.zero_gamma:.0f}" if gex.zero_gamma is not None else "n/a"
    _field(embed, "Spot", f"{gex.spot:.2f}", inline=True)
    _field(embed, "Zero Gamma", zg, inline=True)
    _field(embed, "Net GEX", _fmt_gex(gex.total_net_gex), inline=True)

    if gex.call_walls:
        _field(
            embed,
            "Call Walls (resistance)",
            "\n".join(f"`{k:.0f}` — {_fmt_gex(v)}" for k, v in gex.call_walls),
            inline=True,
        )
    if gex.put_walls:
        _field(
            embed,
            "Put Walls (support)",
            "\n".join(f"`{k:.0f}` — {_fmt_gex(v)}" for k, v in gex.put_walls),
            inline=True,
        )
    embed.set_footer(text=_footer())
    return _fit(embed)


def review_embed(review: dict, stats: dict) -> discord.Embed:
    """Accuracy review: deterministic MFE/MAE stats + Claude's pattern read."""
    acc = stats.get("accuracy_pct", 0.0)
    embed = discord.Embed(
        title="🔁 Accuracy Review — scan2 feedback loop",
        description=_clip(review.get("summary", ""), _DESC_MAX),
        color=COLOR_BULL if acc >= 50 else COLOR_BEAR if acc < 35 else COLOR_INFO,
        timestamp=now_et(),
    )
    _field(
        embed,
        "Scorecard",
        (
            f"`{stats.get('count', 0)}` graded | "
            f"**{acc:.0f}%** accurate "
            f"(`{stats.get('accurate', 0)}`✓ / `{stats.get('mixed', 0)}`~ / "
            f"`{stats.get('inaccurate', 0)}`✗)\n"
            f"Avg MFE `{stats.get('avg_mfe_pts', 0)}` / "
            f"MAE `{stats.get('avg_mae_pts', 0)}` pts | "
            f"ratio `{stats.get('avg_ratio', 0)}`"
        ),
    )

    def _bullets(items: list[str], limit: int = 5) -> str:
        return "\n".join(f"• {x}" for x in items[:limit]) or "—"

    if review.get("accurate_patterns"):
        _field(embed, "What works", _bullets(review["accurate_patterns"]))
    if review.get("inaccurate_patterns"):
        _field(embed, "What misses", _bullets(review["inaccurate_patterns"]))
    if review.get("suggestions"):
        _field(embed, "Tune", _bullets(review["suggestions"]))
    embed.set_footer(text=_footer())
    return _fit(embed)


def premarket_embed(sig: Signal, macro_lines: list[str] | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"☀ Pre-Market Briefing — {sig.trend_label}",
        description=_clip("\n".join(f"• {n}" for n in sig.notes), _DESC_MAX) or None,
        color=_color(sig.direction),
        timestamp=now_et(),
    )
    _field(embed, "Confidence", f"{sig.confidence:.0f}%", inline=True)
    if macro_lines:
        _field(embed, "Macro Board", "\n".join(macro_lines))
    embed.set_footer(text=_footer())
    return _fit(embed)
