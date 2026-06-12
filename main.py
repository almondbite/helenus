"""Helenus entrypoint: `python main.py`."""

import logging

from helenus.bot import HelenusBot
from helenus.config import DISCORD_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("HELENUS_DISCORD_TOKEN is not set (see .env.example)")
    bot = HelenusBot()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
