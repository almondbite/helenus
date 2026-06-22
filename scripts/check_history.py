"""
Read-only: connect and print the last few messages in the alert channel, so we
can confirm whether the bot's test embeds actually landed. Sends nothing.

    python scripts/check_history.py
"""

from __future__ import annotations

import asyncio

import discord

from helenus.config import DISCORD_CHANNEL_ID, DISCORD_TOKEN


def main() -> None:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            ch = client.get_channel(DISCORD_CHANNEL_ID)
            print(f"Reading #{getattr(ch, 'name', '?')} ({DISCORD_CHANNEL_ID})")
            count = 0
            async for m in ch.history(limit=8):
                count += 1
                etitle = m.embeds[0].title if m.embeds else None
                print(
                    f"  [{m.created_at:%H:%M:%S}] {m.author} | "
                    f"content={m.content!r} | embed_title={etitle!r}"
                )
            if count == 0:
                print("  (channel is empty)")
        except Exception as e:  # noqa: BLE001
            print(f"history read failed: {e!r}")
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
