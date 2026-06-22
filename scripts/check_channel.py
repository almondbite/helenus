"""
Verify Helenus can PUSH to the configured alert channel — the same path the
alert workers use (get_channel(HELENUS_CHANNEL_ID).send(...)). Connects, resolves
the channel by ID, prints the bot's effective permissions there, and sends one
test embed. Read-only except for that single confirmation message.

    python scripts/check_channel.py
"""

from __future__ import annotations

import discord

from helenus.config import DISCORD_CHANNEL_ID, DISCORD_TOKEN

REQUIRED = ("view_channel", "send_messages", "embed_links")
SHORT = {"view_channel": "view", "send_messages": "send", "embed_links": "embed"}


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("HELENUS_DISCORD_TOKEN is not set (.env).")
    if not DISCORD_CHANNEL_ID:
        raise SystemExit("HELENUS_CHANNEL_ID is not set or is 0 (.env).")

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"Logged in as {client.user}")
        print(f"Configured HELENUS_CHANNEL_ID = {DISCORD_CHANNEL_ID}")
        print(f"Bot is in {len(client.guilds)} server(s).\n")

        # Enumerate everything the bot can actually see, so the correct channel
        # id is visible even if the configured one is wrong.
        target = None
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
                    target = ch
            print()

        if target is None:
            print("✗ The configured HELENUS_CHANNEL_ID was NOT found in any server "
                  "the bot is in. Either the ID is wrong (copy the right one from "
                  "the list above) or the bot isn't in that server.")
            await client.close()
            return

        perms = target.permissions_for(target.guild.me)
        missing = [n for n in REQUIRED if not getattr(perms, n)]
        if missing:
            print(f"✗ Missing required permission(s) in #{target.name}: "
                  f"{', '.join(missing)} — alerts would silently fail.")
            await client.close()
            return

        print(f"All required perms present in #{target.name}. Sending test embed...")
        try:
            import asyncio
            msg = await asyncio.wait_for(
                target.send(embed=discord.Embed(
                    title="Helenus channel check",
                    description="If you can read this, alert push works ✅",
                    color=0x2ecc71,
                )),
                timeout=15,
            )
            print(f"✓ Test embed sent to #{target.name} (message id {msg.id}).")
        except asyncio.TimeoutError:
            print("✗ send() timed out after 15s — REST POST to Discord did not "
                  "complete (network/proxy issue, not a permission problem).")
        except discord.Forbidden:
            print("✗ send() forbidden despite permission flags — check "
                  "category/role overrides.")
        except Exception as e:  # noqa: BLE001
            print(f"✗ send() raised: {e!r}")
        await client.close()

    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
