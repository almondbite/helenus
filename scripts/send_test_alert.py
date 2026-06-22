"""
End-to-end test of the REAL alert path: render an actual Signal through
embeds.signal_embed (the same builder the live bot uses) and post it to the
alert channel. Clearly marked as a test. One message.

    python scripts/send_test_alert.py
"""

from __future__ import annotations

import asyncio

import discord

from helenus.config import DISCORD_CHANNEL_ID, DISCORD_TOKEN
from helenus.engine.scan2 import Direction, KeyLevel, Signal, TriggerType
from helenus.output import embeds

# Modeled on today's real 10:31 alert, flagged so nobody mistakes it for live.
SIG = Signal(
    trigger=TriggerType.VOLUME_CONFIRMATION,
    direction=Direction.BEARISH,
    level=KeyLevel(7490.0, "Round 7490", 0.6),
    spot=7487.57,
    volume_ratio=2.5,
    confidence=66.0,
    trend_label="BEARISH WITH TREND",
    notes=["TEST ALERT — verifying Helenus' live signal_embed delivery path. Not a real signal."],
)


def main() -> None:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            ch = client.get_channel(DISCORD_CHANNEL_ID)
            if ch is None:
                print(f"FAIL: channel {DISCORD_CHANNEL_ID} did not resolve")
                return
            msg = await asyncio.wait_for(
                ch.send(embed=embeds.signal_embed(SIG)), timeout=15
            )
            # Print BEFORE teardown — gateway close can be slow headless.
            print(f"SENT ok -> #{ch.name} message id={msg.id}")
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
