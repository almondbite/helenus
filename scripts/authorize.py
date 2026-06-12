"""
One-time interactive Schwab OAuth flow. Run this manually before first launch;
it opens a browser, completes the login, and writes the token file the bot
loads non-interactively via client_from_token_file.
"""

from schwab.auth import client_from_login_flow

from helenus.config import (
    SCHWAB_API_KEY,
    SCHWAB_APP_SECRET,
    SCHWAB_CALLBACK_URL,
    SCHWAB_TOKEN_PATH,
)

if __name__ == "__main__":
    client_from_login_flow(
        api_key=SCHWAB_API_KEY,
        app_secret=SCHWAB_APP_SECRET,
        callback_url=SCHWAB_CALLBACK_URL,
        token_path=SCHWAB_TOKEN_PATH,
    )
    print(f"Token written to {SCHWAB_TOKEN_PATH}")
