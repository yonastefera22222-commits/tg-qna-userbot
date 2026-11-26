"""Get a durable Telegram session string that survives a free host's wipes.

Why you need this
-----------------
Render Free has no persistent disk (https://render.com/docs/free). Every deploy,
restart and 15-minute idle spin-down wipes the container's filesystem, including
`data/bot.db` - which is where the bot stores your Telegram login. So a login
completed inside the bot can be silently undone later, and you are asked for a
code again.

Environment variables are the one thing Render does *not* wipe. This script
produces a `TG_STRING_SESSION` value you paste into Render's Environment tab
once, and then the bot restores your login at every start with no code at all.

How to run it
-------------
On the machine where you can receive Telegram messages, in a terminal:

    pip install telethon
    python tools/make_session.py

It asks for your API ID, API Hash and phone number, then prompts for the login
code Telegram sends to your Telegram app. The final string printed is your
session.

    ⚠️ That string IS your Telegram account. Anyone holding it can read and send
    messages as you. Paste it only into your own host's Environment tab - never
    into a chat, and never share it.

Revoking it later: Telegram -> Settings -> Devices -> terminate the session named
"Q&A Userbot".
"""

from __future__ import annotations

import asyncio
import getpass
import sys

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover - only hit when deps are missing
    print("Telethon is not installed. Run:  pip install telethon")
    raise SystemExit(1) from None

#: Matches the login fingerprint in src/qna_bot/userbot/login.py, so the code
#: arrives and is accepted the same way it would in the deployed bot.
DEVICE_MODEL = "Pixel 6"
SYSTEM_VERSION = "SDK 33"
APP_VERSION = "10.2.0 (4300)"


async def main() -> int:
    print(__doc__.split("How to run it")[0].strip())
    print("\n" + "-" * 60)

    raw_id = input("API ID (numbers only, from my.telegram.org): ").strip()
    if not raw_id.isdigit():
        print("That is not a number. Run this again and paste the numeric App api_id.")
        return 1
    api_id = int(raw_id)

    api_hash = input("API Hash (32 characters): ").strip()
    if len(api_hash) < 16:
        print("That API Hash looks too short. Copy the full value from my.telegram.org.")
        return 1

    phone = input("Phone number in international format, e.g. +251912345678: ").strip()
    if not phone.startswith("+"):
        print("Include the country code, starting with '+'. Run this again.")
        return 1

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
    )

    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        described = type(getattr(sent, "type", sent)).__name__
        print(f"\nTelegram answered: {described}")
        print("Look in your Telegram app for the login code (the 'Telegram' service chat).")
        code = getpass.getpass("Login code: ").strip()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except Exception as exc:
            if type(exc).__name__ != "SessionPasswordNeededError":
                print(f"\nLogin failed: {exc}")
                print(
                    "\nIf the code never arrives: open Telegram on the phone that already "
                    "uses this number, and check the 'Telegram' service chat for a "
                    "'Confirm login' button instead of a code."
                )
                return 1
            password = getpass.getpass("Two-Step Verification password: ").strip()
            await client.sign_in(password=password)

        session_string = client.session.save()
    finally:
        await client.disconnect()

    print("\n" + "=" * 60)
    print("YOUR SESSION STRING (this is your account - keep it private):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print(
        "\nNext: Render -> your service -> Environment -> Add Environment Variable\n"
        "  Key:   TG_STRING_SESSION\n"
        "  Value: (paste the string above)\n"
        "Then Save Changes. The bot will start already logged in - no code, and\n"
        "nothing to lose when the disk is wiped.\n\n"
        "Also keep ENCRYPTION_KEY set to a fixed value, so stored secrets stay readable."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
