"""
Re-send the two alerts that fired today but never reached Discord (the oversized
embed 400'd the send and killed bar_worker). Reconstructs each Signal from its
journal record, renders the now-fixed signal_embed, and posts it to the channel —
clearly marked as a historical re-send, not a live signal.

    python scripts/resend_alerts.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import discord

from helenus.config import DISCORD_CHANNEL_ID, DISCORD_TOKEN
from helenus.engine.scan2 import Direction, KeyLevel, Signal, TriggerType
from helenus.journal import Journal
from helenus.output import embeds

RESEND_IDS = ("6194ffd6", "cd91c483")  # morning bearish, afternoon bullish


def _signal_from_alert(a: dict) -> tuple[Signal, str]:
    ctx = a.get("context", {})
    trigger = next(t for t in TriggerType if t.value == a["trigger"])
    direction = Direction(a["direction"])

    level = None
    raw = ctx.get("level")
    if raw:  # stored as "<label> <price>", e.g. "Round 7490 7490.00"
        label, _, price = raw.rpartition(" ")
        try:
            level = KeyLevel(float(price), label, 0.6)
        except ValueError:
            level = None

    notes = [ctx["thesis"]] if ctx.get("thesis") else []
    sig = Signal(
        trigger=trigger,
        direction=direction,
        level=level,
        spot=float(a["entry"]),
        volume_ratio=float("nan"),  # not persisted; omit the Volume field
        confidence=float(a.get("confidence", 0.0)),
        trend_label=ctx.get("trend_label", direction.value),
        notes=notes,
    )
    when = dt.datetime.fromisoformat(a["ts"]).strftime("%H:%M")
    return sig, when


def main() -> None:
    records = Journal().read()
    alerts = {r["id"]: r for r in records if r.get("type") == "alert"}
    to_send = [alerts[i] for i in RESEND_IDS if i in alerts]
    if not to_send:
        raise SystemExit("None of the target alerts found in the journal.")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            ch = client.get_channel(DISCORD_CHANNEL_ID)
            if ch is None:
                print(f"FAIL: channel {DISCORD_CHANNEL_ID} did not resolve")
                return
            for a in to_send:
                sig, when = _signal_from_alert(a)
                embed = embeds.signal_embed(sig)
                marker = (
                    f"↺ **Re-sent** — original {sig.trigger.value} alert fired "
                    f"**{when} ET today**; delivery failed at the time (oversized "
                    f"embed). Historical re-post, not a live signal.\n\n"
                )
                embed.description = (marker + (embed.description or ""))[:4096]
                msg = await asyncio.wait_for(ch.send(embed=embed), timeout=15)
                print(f"SENT {a['id']} ({sig.direction.value} @ {sig.spot:.2f}, "
                      f"orig {when} ET) -> message id={msg.id}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {e!r}")
        finally:
            await client.close()

    async def runner() -> None:
        try:
            await asyncio.wait_for(client.start(DISCORD_TOKEN), timeout=30)
        except asyncio.TimeoutError:
            print("connect timed out")
            await client.close()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
