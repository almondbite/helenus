"""
Discord command tester — verify !gex / !scan / !flow / !stats / !review render
and respond, with NO Schwab feed and NO Anthropic key required.

It connects to Discord using HELENUS_DISCORD_TOKEN from your .env, registers the
same five commands the real bot exposes, and backs them with *synthetic* market
data + a throwaway journal. `!review` shows a canned review (no Claude call), so
nothing here costs money or needs the API.

Run it two ways:

    python scripts/test_commands_bot.py preview   # print the embeds to console, exit
    python scripts/test_commands_bot.py           # connect to Discord; type the
                                                   # commands in any channel the bot
                                                   # can see

⚠ In the Discord Developer Portal, enable the bot's **Message Content Intent**
  (Bot → Privileged Gateway Intents) or prefix commands won't fire.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import discord
from discord.ext import commands

from helenus.config import DISCORD_TOKEN
from helenus.engine import flow, gex
from helenus.engine.scan2 import Bar, MarketState
from helenus.journal import Journal, OpenAlert, grade_excursion, new_alert_id
from helenus.output import embeds


# --------------------------------------------------------------------------- #
# Synthetic market data
# --------------------------------------------------------------------------- #

def _chain() -> dict:
    """A canned $SPXW 0DTE chain around spot 5475 (Schwab-shaped JSON)."""
    spot = 5475.0
    calls = [  # (strike, oi, gamma, volume)
        (5455, 800, 0.03, 9000), (5465, 600, 0.04, 8000), (5475, 1200, 0.06, 14000),
        (5480, 1500, 0.05, 14800), (5485, 1300, 0.045, 11400),
        (5490, 1000, 0.04, 9100), (5495, 700, 0.03, 6000),
    ]
    puts = [
        (5455, 1400, 0.04, 3400), (5465, 1200, 0.045, 6100), (5470, 1600, 0.05, 10200),
        (5475, 1000, 0.06, 7000), (5480, 900, 0.05, 5000), (5490, 700, 0.04, 3000),
    ]

    def side(rows: list[tuple]) -> dict:
        m: dict[str, list[dict]] = {}
        for strike, oi, gamma, vol in rows:
            m.setdefault(f"{strike:.1f}", []).append(
                {"strikePrice": strike, "openInterest": oi, "gamma": gamma,
                 "totalVolume": vol}
            )
        return {"2026-06-19:0": m}

    return {
        "underlying": {"mark": spot},
        "callExpDateMap": side(calls),
        "putExpDateMap": side(puts),
    }


def _market_state() -> MarketState:
    """A short downtrending tape with an elevated last bar (so !scan has values)."""
    st = MarketState(prior_close=5488.0)
    now = dt.datetime.now()
    close = 5495.0
    for i in range(25):
        close -= 0.8 if i < 18 else 0.1          # drift down, then flatten
        vol = 5000.0 if i < 24 else 9000.0       # last bar elevated -> ~1.8x ratio
        st.push_bar(Bar(ts=now, open=close, high=close + 1, low=close - 1,
                        close=close, volume=vol))
    return st


def _vanna() -> flow.VannaReading:
    return flow.VannaReading(
        vix_change=-0.70, vix_falling=True, otm_call_flow=6200, otm_put_flow=1100,
        call_flow_dominance=5100, active=True, label="VANNA RALLY BUILDING",
        note=("VIX -0.70 (falling) with fresh OTM call flow 6,200 vs put 1,100 "
              "(5.6x) — cheaper calls drawing buyers; dealer hedging supports spot."),
    )


def _journal() -> Journal:
    """Throwaway journal seeded with a few graded alerts (for !stats / !review)."""
    path = os.path.join(tempfile.gettempdir(), "helenus_demo_journal.jsonl")
    if os.path.exists(path):
        os.remove(path)
    j = Journal(path=path)

    def seed(trigger, direction, entry, high, low, last, conf):
        a = OpenAlert(id=new_alert_id(), ts_open=dt.datetime.now().isoformat(),
                      trigger=trigger, direction=direction, entry=entry,
                      confidence=conf, context={"regime": "NEGATIVE_GAMMA"})
        j.log_alert(a)
        j.log_outcome(a.id, grade_excursion(direction, entry, high, low, last, 30))

    seed("Vanna Rally", "BULLISH", 5475, 5495, 5473, 5492, 78)        # ACCURATE
    seed("Sweep & Recover", "BEARISH", 5497, 5499, 5485, 5487, 73)    # ACCURATE
    seed("Volume Confirmation", "BULLISH", 5450, 5451, 5442, 5443, 40)  # INACCURATE
    return j


_REVIEW = {
    "summary": "Vanna setups in negative gamma after a falling tape follow through; "
               "low-confidence counter-trend crosses into call walls stall.",
    "accurate_patterns": [
        "Active vanna in NEGATIVE_GAMMA after a hard down move (2/2 accurate)",
        "Sweep & Recover rejections at round-number call walls",
    ],
    "inaccurate_patterns": [
        "Volume-confirmation crosses with confidence < 50 and no flow support",
    ],
    "suggestions": ["Down-weight sub-50 confidence crosses lacking OTM flow"],
}


PROFILE = gex.build_profile(_chain())
VOL = flow.build_volume_profile(_chain())
VANNA = _vanna()
STATE = _market_state()
JOURNAL = _journal()


# --------------------------------------------------------------------------- #
# Command payloads (shared by the live commands and `preview`)
# --------------------------------------------------------------------------- #

def gex_payload():
    return "embed", embeds.gex_snapshot_embed(PROFILE)


def flow_payload():
    return "embed", embeds.volume_profile_embed(VOL, VANNA)


def review_payload():
    return "embed", embeds.review_embed(_REVIEW, JOURNAL.stats())


def scan_payload():
    st, spot = STATE, PROFILE.spot
    ratio, trend = st.volume_ratio(), st.trend_direction()
    levels = st.key_levels(spot)
    lines = [
        f"Spot: `{spot:.2f}`",
        f"Bars: `{len(st.bars)}` | Vol ratio: `{ratio:.2f}x`"
        if ratio == ratio else f"Bars: `{len(st.bars)}` | Vol ratio: `building`",
        f"Trend: `{trend.value if trend else 'NONE'}`",
        "Levels: " + ", ".join(f"`{lv.label} {lv.price:.0f}`" for lv in levels[:6]),
    ]
    return "text", "\n".join(lines)


def stats_payload():
    s = JOURNAL.stats()
    if s.get("count", 0) == 0:
        return "text", "No graded alerts yet."
    lines = [
        f"Graded: `{s['count']}` | Accuracy: **{s['accuracy_pct']}%**",
        f"✓ `{s['accurate']}`  ~ `{s['mixed']}`  ✗ `{s['inaccurate']}`",
        f"Avg MFE `{s['avg_mfe_pts']}` / MAE `{s['avg_mae_pts']}` pts | ratio `{s['avg_ratio']}`",
    ]
    for trig, d in s["by_trigger"].items():
        lines.append(f"`{trig}`: {d['ACCURATE']}/{d['n']} accurate")
    return "text", "\n".join(lines)


PAYLOADS = {
    "gex": gex_payload, "scan": scan_payload, "flow": flow_payload,
    "stats": stats_payload, "review": review_payload,
}


# --------------------------------------------------------------------------- #
# Preview (no Discord) — print to console
# --------------------------------------------------------------------------- #

def _print_payload(name: str) -> None:
    kind, payload = PAYLOADS[name]()
    print(f"\n========== !{name} ==========")
    if kind == "text":
        print(payload)
        return
    e: discord.Embed = payload
    if e.title:
        print(f"[{e.title}]")
    if e.description:
        print(e.description)
    for f in e.fields:
        print(f"  {f.name}: {f.value}")


def preview() -> None:
    # Force UTF-8 so the ladder glyphs print on a cp1252 Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for name in PAYLOADS:
        _print_payload(name)
    print("\n(preview only — no Discord connection)")


# --------------------------------------------------------------------------- #
# Live Discord bot
# --------------------------------------------------------------------------- #

def _make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    async def _send(ctx, name: str) -> None:
        kind, payload = PAYLOADS[name]()
        await ctx.send(payload) if kind == "text" else await ctx.send(embed=payload)

    for cmd_name in PAYLOADS:
        async def handler(ctx, _n=cmd_name):  # bind name per loop iteration
            await _send(ctx, _n)
        bot.add_command(commands.Command(handler, name=cmd_name))

    @bot.event
    async def on_ready() -> None:
        print(f"Connected as {bot.user}. Type !gex / !scan / !flow / !stats / !review "
              "in a channel the bot can see. Ctrl+C to stop.")

    return bot


def main() -> None:
    if "preview" in sys.argv[1:]:
        preview()
        return
    if not DISCORD_TOKEN:
        raise SystemExit("HELENUS_DISCORD_TOKEN is not set (see .env).")
    _make_bot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
