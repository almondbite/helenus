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
from helenus.engine.charm import CharmProfile
from helenus.engine.flow import VannaReading, VolumeProfile
from helenus.engine.gex import GexProfile
from helenus.engine.intermarket import IntermarketProfile
from helenus.engine.scalp import ScalpReading
from helenus.engine.displacement import DisplacementReading
from helenus.engine.orb import ORBReading
from helenus.engine.moc import MocReading
from helenus.engine.scan2 import Direction, Signal, TriggerType

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


def _charm_line(charm: CharmProfile) -> str:
    """One-line charm summary for signal embeds."""
    sign = "+" if charm.net_charm >= 0 else "−"
    return (
        f"Bias: `{charm.bias}` ({charm.intensity}) | "
        f"Net: `{sign}{_fmt_gex(abs(charm.net_charm))}` | "
        f"{charm.minutes_to_expiry:.0f}m to settle"
    )


def _regime_short(regime: str) -> str:
    """POSITIVE_GAMMA[_CONFLICTED] -> 'POS', NEGATIVE... -> 'NEG', else 'UNK'."""
    return regime.split("_")[0][:3].upper() if regime else "UNK"


def _leg_part(leg) -> str:
    return f"{leg.symbol} `{leg.pct_change:+.2f}%`/{_regime_short(leg.regime)}"


def _intermarket_line(im: IntermarketProfile, signal_dir: Direction | None = None) -> str:
    """One-line intermarket summary. With a signal direction, leads with the
    alignment verdict + applied boost; otherwise just the board."""
    parts: list[str] = []
    if signal_dir is not None:
        verdict = im.alignment(signal_dir)
        boost = im.confidence_boost(signal_dir)
        parts.append(f"`{verdict}`" + (f" (+{boost:.0f})" if boost > 0 else ""))
    if im.es is not None:
        parts.append(
            f"ES `{im.es.pct_change:+.2f}%` imb `{im.es.imbalance:+.2f}` "
            f"flow `{_fmt_vol(im.es.volume_flow)}`"
        )
    parts += [_leg_part(leg) for leg in (im.qqq, im.spy) if leg is not None]
    return " | ".join(parts) if parts else "no intermarket data"


def _scalp_plan(scalp: ScalpReading) -> str:
    """The EMA-ignition trade plan line: strike + entry premium + premium target."""
    c = scalp.target_contract
    if c is None:
        return "no strike selected"
    strike = f"{c.strike:.0f}{'C' if c.side == 'call' else 'P'}"
    tgt = f" → `{scalp.premium_target:.2f}`" if scalp.premium_target is not None else ""
    lvl = (
        f"{scalp.target_level.label} @ {scalp.target_level.price:.0f}"
        if scalp.target_level is not None else "open air"
    )
    return f"`{strike}` @ `{c.premium:.2f}`{tgt}  (target: {lvl})"


def _scalp_flags(scalp: ScalpReading) -> str:
    """The EMA state + booster/avoidance flags for the scalp."""
    bits = [f"EMA `{scalp.ema_stack}` ({scalp.cross_type})"]
    if scalp.slow_grind:
        bits.append("⚠ slow grind (high+GEX)")
    if scalp.front_run:
        bits.append("🟢 front-run")
    if scalp.premium_divergence:
        bits.append("🟢 divergence")
    if scalp.vanna_headwind:
        bits.append("⚠ vanna headwind")
    if scalp.dual_bleed:
        bits.append("⚠ dual-bleed")
    if scalp.chop_count:
        bits.append(f"{scalp.chop_count} crosses")
    return " | ".join(bits)


def _disp_line(d: DisplacementReading) -> str:
    """The displacement trade plan: candle stats, FVG retrace zone, MSS, sweep."""
    bits = [f"body `{abs(d.body_pts):.1f}pt` ({d.body_frac:.0%}) `{d.vol_ratio:.1f}x` vol"]
    if d.trend_direction is not None and d.midpoint is not None:
        held = "holds" if d.holding_above_mid else "lost"
        bits.append(f"trend `{d.trend_direction.value}` ({held} 50% `{d.midpoint:.0f}`)")
    if d.fvg and d.fvg_low is not None:
        bits.append(f"FVG `{d.fvg_low:.0f}–{d.fvg_high:.0f}`")
    if d.mss and d.mss_level is not None:
        bits.append(f"MSS `{d.mss_level:.0f}`")
    if d.swept and d.swept_level is not None:
        bits.append(f"🟢 swept `{d.swept_level:.0f}`")
    return " | ".join(bits)


def _orb_line(o: ORBReading) -> str:
    """The ORB trade plan: range, entry edge, R-targets, stop, filters."""
    tgt = " / ".join(f"{t:.0f}" for t in o.targets) if o.targets else "—"
    flags = ("vol " + ("✓" if o.volume_ok else "✗")) + (" vwap " + ("✓" if o.vwap_ok else "✗"))
    entry = f"{o.entry:.0f}" if o.entry is not None else "—"
    rng = f"{o.range_pts:.0f}pt" if o.range_pts is not None else "—"
    return f"entry `{entry}` (range `{rng}`) | targets `{tgt}` | stop `{o.stop:.0f}` | {flags}"


def _moc_line(moc: MocReading) -> str:
    """The MOC close-play read: phase, the premium-behavior reversal or
    capitulation candle, the 5m heuristic, and GEX pin/overshoot."""
    bits = [f"`{moc.phase}` ({moc.minutes_to_close:+.0f}m)"]
    if moc.reversal_direction is not None:
        tag = "🟢 " if moc.reversal_active else ""
        bits.append(
            f"{tag}reversal `{moc.reversal_direction.value}` "
            f"({moc.basing_side} based, `{moc.volume_surge_ratio:.1f}×` vol)"
        )
    if moc.capitulation:
        bits.append(
            f"⚡ capitulation `{moc.cap_side}` `{moc.cap_high:.2f}→{moc.cap_close:.2f}`"
        )
    if moc.heuristic_bias is not None:
        bits.append(f"5m `{moc.heuristic_color}`→`{moc.heuristic_bias.value}`")
    bits.append(f"GEX `{moc.gex_state}`")
    return " | ".join(bits)


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
    charm: CharmProfile | None = None,
    intermarket: IntermarketProfile | None = None,
    scalp: ScalpReading | None = None,
    displacement: DisplacementReading | None = None,
    orb: ORBReading | None = None,
    moc: MocReading | None = None,
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
    if charm is not None:
        _field(embed, "Charm (delta-decay)", _charm_line(charm))
    if intermarket is not None:
        _field(embed, "Intermarket", _intermarket_line(intermarket, sig.direction))
    # Scalp prefers the value carried on the Signal (the read at fire time) but
    # accepts an explicit override for callers that pass the live reading.
    sc = scalp if scalp is not None else sig.scalp
    if sc is not None and sc.target_contract is not None:
        _field(embed, "Scalp Plan", _scalp_plan(sc))
        _field(embed, "Scalp State", _scalp_flags(sc))
    if displacement is not None and displacement.detected and sig.trigger is TriggerType.DISPLACEMENT:
        _field(embed, "Displacement", _disp_line(displacement))
    if orb is not None and orb.entry is not None and sig.trigger is TriggerType.ORB_BREAKOUT:
        _field(embed, "ORB Plan", _orb_line(orb))
    if moc is not None and sig.trigger in (TriggerType.MOC_REVERSAL, TriggerType.CAPITULATION):
        _field(embed, "MOC", _moc_line(moc))

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


def intermarket_embed(
    im: IntermarketProfile, signal_dir: Direction | None = None
) -> discord.Embed:
    """Informational intermarket board: /ES Level-1 microstructure + SPY/QQQ 0DTE
    gamma structure. Grey (no standalone directional claim without a signal)."""
    embed = discord.Embed(
        title="Intermarket Convergence — /ES + SPY + QQQ",
        color=COLOR_INFO,
        timestamp=now_et(),
    )
    if im.es is not None:
        es = im.es
        _field(embed, "/ES (futures)", f"`{es.pct_change:+.2f}%` | last `{es.last:.2f}`", inline=True)
        _field(
            embed,
            "ES order book",
            f"imb `{es.imbalance:+.2f}` | bid `{_fmt_vol(es.bid_size)}` / ask `{_fmt_vol(es.ask_size)}`",
            inline=True,
        )
        _field(embed, "ES flow", f"`{_fmt_vol(es.volume_flow)}` /interval", inline=True)
    for leg in (im.qqq, im.spy):
        if leg is not None:
            zg = f"{leg.zero_gamma:.0f}" if leg.zero_gamma is not None else "n/a"
            _field(
                embed,
                leg.symbol,
                f"`{leg.pct_change:+.2f}%` ({leg.confirms_label}) | "
                f"regime `{leg.regime}` | zero-Γ `{zg}`",
                inline=True,
            )
    _field(embed, "SPX regime (ref)", f"`{im.spx_regime}`", inline=True)
    if signal_dir is not None:
        _field(embed, f"Alignment vs {signal_dir.value}", _intermarket_line(im, signal_dir))
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


def charm_snapshot_embed(charm: CharmProfile) -> discord.Embed:
    """Informational charm (delta-decay) board: OTM-wing dealer un-hedging.

    Greened when SUPPORTIVE, reddened when OVERHEAD, grey when balanced — charm
    *is* a directional drift bias, unlike the neutral gamma board."""
    color = (
        COLOR_BULL if charm.bias == "SUPPORTIVE"
        else COLOR_BEAR if charm.bias == "OVERHEAD"
        else COLOR_INFO
    )
    embed = discord.Embed(
        title="$SPXW 0DTE — Charm (delta-decay) Structure",
        description=f"_{charm.drift}_",
        color=color,
        timestamp=now_et(),
    )
    _field(embed, "Spot", f"{charm.spot:.2f}", inline=True)
    _field(embed, "Bias", f"{charm.bias} ({charm.intensity})", inline=True)
    _field(embed, "To settle", f"{charm.minutes_to_expiry:.0f} min", inline=True)
    sign = "+" if charm.net_charm >= 0 else "−"
    _field(embed, "Net Charm", f"{sign}{_fmt_gex(abs(charm.net_charm))}", inline=True)
    _field(embed, "Put Support", _fmt_gex(charm.put_support), inline=True)
    _field(embed, "Call Overhead", _fmt_gex(charm.call_overhead), inline=True)

    if charm.support_walls:
        _field(
            embed,
            "Charm Support (OTM puts)",
            "\n".join(f"`{k:.0f}` — {_fmt_gex(v)}" for k, v in charm.support_walls),
            inline=True,
        )
    if charm.resistance_walls:
        _field(
            embed,
            "Charm Resistance (OTM calls)",
            "\n".join(f"`{k:.0f}` — {_fmt_gex(abs(v))}" for k, v in charm.resistance_walls),
            inline=True,
        )
    embed.set_footer(text=_footer())
    return _fit(embed)


def scalp_snapshot_embed(scalp: ScalpReading) -> discord.Embed:
    """Informational EMA-ignition board: the 5/9/200 EMA state, the gate stack,
    and (when a cross is live) the delta-targeted strike + premium target.

    Greened/reddened by the fresh trigger's direction; grey when no cross is live."""
    color = _color(scalp.direction) if scalp.direction is not None else COLOR_INFO
    head = scalp.note or "no fresh 5/9 cross or reclaim"
    embed = discord.Embed(
        title="$SPXW 0DTE — EMA Ignition (1m 5/9 scalp)",
        description=f"_{head}_",
        color=color,
        timestamp=now_et(),
    )
    f5 = f"{scalp.ema_fast:.2f}" if scalp.ema_fast is not None else "—"
    f9 = f"{scalp.ema_slow:.2f}" if scalp.ema_slow is not None else "—"
    f200 = f"{scalp.ema_trend:.2f}" if scalp.ema_trend is not None else "—"
    _field(embed, "5 / 9 EMA", f"`{f5}` / `{f9}` ({scalp.ema_stack})", inline=True)
    _field(embed, "200 EMA", f"`{f200}`", inline=True)
    _field(embed, "Cross", f"`{scalp.cross_type}`", inline=True)

    if scalp.direction is not None:
        gates = (
            f"vanna {_ok(scalp.vanna_ok)} | confirm {_ok(scalp.confirm_ok)} | "
            f"room {_ok(scalp.room_ok)} | chop {_ok(scalp.chop_ok)} | "
            f"bleed {_ok(scalp.bleed_ok)} | spread {_ok(scalp.spread_ok)}"
            + (" | ⚠ slow grind" if scalp.slow_grind else "")
        )
        _field(embed, "Gates", gates)
        _field(embed, "Plan", _scalp_plan(scalp))
        _field(embed, "State", _scalp_flags(scalp))
        _field(
            embed, "Verdict",
            "🟢 **ACTIVE** — all gates passed" if scalp.active else "⛔ gated (see above)",
        )
    embed.set_footer(text=_footer())
    return _fit(embed)


def _ok(flag: bool) -> str:
    return "✓" if flag else "✗"


def displacement_snapshot_embed(disp: DisplacementReading) -> discord.Embed:
    """Informational displacement board: the institutional thrust candle and which
    of the three pillars (FVG / MSS / sweep) are present. Greened/reddened by the
    thrust direction; grey when no qualifying candle exists."""
    color = _color(disp.direction) if disp.direction is not None else COLOR_INFO
    embed = discord.Embed(
        title="$SPX — Displacement (institutional thrust)",
        description=f"_{disp.note}_",
        color=color,
        timestamp=now_et(),
    )
    if not disp.detected:
        embed.set_footer(text=_footer())
        return _fit(embed)
    _field(embed, "Candle", _disp_line(disp))
    _field(embed, "FVG", _ok(disp.fvg), inline=True)
    _field(embed, "MSS", _ok(disp.mss), inline=True)
    _field(embed, "Sweep", _ok(disp.swept), inline=True)
    _field(
        embed, "Verdict",
        "🟢 **ACTIVE** — candle gate (FVG/MSS/sweep are boosters)"
        if disp.active else "⛔ no qualifying displacement candle",
    )
    embed.set_footer(text=_footer())
    return _fit(embed)


def orb_snapshot_embed(orb: ORBReading) -> discord.Embed:
    """Informational opening-range board: the locked range, the breakout state, and
    the R-multiple plan. Greened/reddened on an active breakout; grey otherwise."""
    color = _color(orb.direction) if (orb.direction is not None and orb.active) else COLOR_INFO
    embed = discord.Embed(
        title="$SPX — Opening Range Breakout",
        description=f"_{orb.note}_",
        color=color,
        timestamp=now_et(),
    )
    if orb.range_high is not None:
        _field(
            embed, "Opening Range",
            f"`{orb.range_low:.2f}`–`{orb.range_high:.2f}` ({orb.range_pts:.0f}pt) "
            + ("🔒 locked" if orb.locked else "building"),
        )
    if orb.direction is not None and orb.entry is not None:
        _field(embed, f"{orb.direction.value} Breakout", _orb_line(orb))
        _field(
            embed, "Verdict",
            "🟢 **ACTIVE** — filters passed" if orb.active
            else "⛔ filtered (volume / VWAP)",
        )
    embed.set_footer(text=_footer())
    return _fit(embed)


def moc_snapshot_embed(moc: MocReading) -> discord.Embed:
    """Informational Market-On-Close board: the window phase, the premium-behavior
    reversal, the capitulation candle, and the close-play priors. Greened/reddened
    by the reversal (or heuristic) bias; grey when nothing is leaning."""
    bias = moc.reversal_direction or moc.heuristic_bias
    color = _color(bias) if bias is not None else COLOR_INFO
    embed = discord.Embed(
        title="$SPX — Market-On-Close (power-hour play)",
        description=f"_{moc.note}_",
        color=color,
        timestamp=now_et(),
    )
    _field(embed, "Window", f"`{moc.phase}` | `{moc.minutes_to_close:+.0f}m` to close", inline=True)
    _field(
        embed, "GEX",
        f"`{moc.gex_state}`" + (f" @ `{moc.nearest_wall:.0f}`" if moc.nearest_wall else ""),
        inline=True,
    )
    if moc.heuristic_bias is not None:
        _field(embed, "5m heuristic", f"`{moc.heuristic_color}` → `{moc.heuristic_bias.value}`", inline=True)

    cp = f"{moc.call_premium:.2f}" if moc.call_premium is not None else "—"
    pp = f"{moc.put_premium:.2f}" if moc.put_premium is not None else "—"
    _field(
        embed, "ATM premium / volume",
        f"Call `{cp}` (`{_fmt_vol(moc.call_volume)}`) | Put `{pp}` (`{_fmt_vol(moc.put_volume)}`)",
    )
    if moc.reversal_direction is not None:
        _field(
            embed, "Reversal",
            f"{'🟢 **ACTIVE** ' if moc.reversal_active else ''}`{moc.reversal_direction.value}` — "
            f"{moc.basing_side} based + `{moc.volume_surge_ratio:.1f}×` volume"
            + ("" if moc.reversal_active else "  _(outside 3:50–3:55 window)_"),
        )
    if moc.capitulation:
        _field(
            embed, "Capitulation",
            f"⚡ `{moc.cap_side}` premium wicked `{moc.cap_high:.2f}` → closed "
            f"`{moc.cap_close:.2f}` (wick `{moc.cap_wick_frac:.0%}`) — undercut then reclaim",
        )
    embed.set_footer(text=_footer())
    return _fit(embed)


def moc_briefing_embed(sig: Signal, lines: list[str] | None = None) -> discord.Embed:
    """The ~3:47 ET MOC setup briefing (posted directly, never graded). Mirrors the
    pre-market briefing — the close-play bias + what to watch into the auction."""
    embed = discord.Embed(
        title=f"🔔 MOC Setup Briefing — {sig.trend_label}",
        description=_clip("\n".join(f"• {n}" for n in sig.notes), _DESC_MAX) or None,
        color=_color(sig.direction),
        timestamp=now_et(),
    )
    _field(embed, "Confidence", f"{sig.confidence:.0f}%", inline=True)
    if lines:
        _field(embed, "Close Window", "\n".join(lines))
    embed.set_footer(text=_footer())
    return _fit(embed)


def stream_status_embed(status: dict) -> discord.Embed:
    """Informational real-time websocket board: connection state + which streams
    are live/fresh, the ATM option subs, and last-tick ages. Green when connected
    and at least one stream is fresh, red when enabled-but-down, grey when off."""
    enabled = status.get("enabled")
    connected = status.get("connected")
    opt, es, spy = status.get("options", {}), status.get("es", {}), status.get("spy", {})
    any_fresh = any(s.get("fresh") for s in (opt, es, spy))
    # Green = connected & delivering; red = connected but silent (a real problem);
    # grey = disabled or still connecting/reconnecting (transient, not alarming).
    color = (
        COLOR_BULL if (connected and any_fresh)
        else COLOR_BEAR if connected
        else COLOR_INFO
    )
    state = "disabled" if not enabled else "connected" if connected else "connecting / reconnecting"
    embed = discord.Embed(
        title="📡 Real-time Stream — schwab-py StreamClient",
        description=f"_Connection: {state}_",
        color=color,
        timestamp=now_et(),
    )

    def _line(s: dict) -> str:
        mark = "🟢" if s.get("fresh") else "⚪"
        age = s.get("age_s")
        return f"{mark} {'fresh' if s.get('fresh') else 'stale'}" + (f" ({age:.0f}s ago)" if age is not None else " (no ticks)")

    roles = opt.get("roles", {})
    syms = " / ".join(f"`{roles.get(r) or '—'}`" for r in ("call", "put"))
    _field(embed, "Options (ATM C/P)", f"{_line(opt)}\n{syms}")
    _field(embed, f"/ES ({es.get('symbol', '—')})", _line(es), inline=True)
    spy_vol = spy.get("minute_volume")
    spy_extra = f" | vol `{_fmt_vol(spy_vol)}`" if spy_vol else ""
    _field(embed, f"SPY chart ({spy.get('symbol', '—')})", _line(spy) + spy_extra, inline=True)
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
