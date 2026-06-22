"""
Read-only Discord diagnostic: connect, list every guild + text channel the bot
can see, mark the configured HELENUS_CHANNEL_ID, and report whether it resolves
and what permissions the bot holds there. Sends NOTHING — pure inspection.

    python scripts/diagnose_channel.py
"""

from __future__ import annotations

import asyncio

import discord

from helenus.config import DISCORD_CHANNEL_ID, DISCORD_TOKEN

REQUIRED = ("view_channel", "send_messages", "embed_links")
SHORT = {"view_channel": "view", "send_messages": "send", "embed_links": "embed"}


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("HELENUS_DISCORD_TOKEN is not set (.env).")
    print(f"Configured HELENUS_CHANNEL_ID = {DISCORD_CHANNEL_ID}")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"Logged in as {client.user}  |  in {len(client.guilds)} server(s)\n")
        found = None
        for g in client.guilds:
            print(f"Server: {g.name}  (id {g.id})")
            for ch in g.text_channels:
                p = ch.permissions_for(g.me)
                flags = " ".join(
                    f"{'+' if getattr(p, n) else '-'}{SHORT[n]}" for n in REQUIRED
                )
                mark = "  <== CONFIGURED" if ch.id == DISCORD_CHANNEL_ID else ""
                print(f"   #{ch.name:<22} id {ch.id}  [{flags}]{mark}")
                if ch.id == DISCORD_CHANNEL_ID:
                    found = ch
            print()

        # get_channel is exactly what the alert path uses.
        resolved = client.get_channel(DISCORD_CHANNEL_ID)
        print(f"get_channel({DISCORD_CHANNEL_ID}) -> {resolved!r}")
        if found is None:
            print(
                "\nDIAGNOSIS: the configured channel is NOT in any server the bot "
                "is in. Alerts journal but never post (alert_channel() is None). "
                "Fix: invite the bot to the right server, or copy the correct "
                "channel id from the list above into HELENUS_CHANNEL_ID."
            )
        else:
            missing = [n for n in REQUIRED if not getattr(found.permissions_for(found.guild.me), n)]
            if missing:
                print(f"\nDIAGNOSIS: channel resolves but missing perms: {missing}.")
            else:
                print("\nChannel resolves with all required perms — push should work.")
        await client.close()

    async def runner() -> None:
        try:
            await asyncio.wait_for(client.start(DISCORD_TOKEN), timeout=40)
        except asyncio.TimeoutError:
            print("Timed out connecting to Discord (40s).")
            await client.close()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
