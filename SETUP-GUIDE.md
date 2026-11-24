# Setting Up the Bot for Another Account — Step by Step

This is the complete path from an empty Render account to a working bot answering giveaways, **for a second account of your own**. It is written against the current code, so every button name and env var below is exactly what you will see.

Two things are decided per account and cannot be reused:

| Per account | Why |
|---|---|
| **`BOT_TOKEN`** | Each bot needs its *own* @BotFather bot. Two deployments sharing one token fight over the same Telegram update stream — both look flaky. |
| **Telegram login** (`api_id`/`api_hash` + a logged-in account) | The bot answers *as whoever is logged in*. That is the whole point of a userbot. |

Everything else (which groups, detection mode, cooldowns, keywords) can be carried over with the **Share** button.

---

## Step 1 — Get a separate bot token

1. In Telegram, open **@BotFather**.
2. Send `/newbot`, choose a name and a username ending in `bot`.
3. Copy the **token** it returns (`123456789:AAE...`).

> **Do not reuse your existing bot's token.** If both deployments poll the same token, Telegram delivers each update to only one of them, and answers appear to fire at random.

---

## Step 2 — Get the new account's numeric Telegram ID

1. In Telegram, open **@userinfobot** and send `/start`.
2. It replies with an **`Id:`** number, e.g. `987654321`.

This is the `OWNER_ID`. It locks the dashboard to that person — without it, anyone who finds your bot can take it over. Save this number.

---

## Step 3 — Fork the repository

Your repo: `github.com/yonastefera22222-commits/tg-qna-userbot` → click **Fork** → create it under the target account (or your own, it does not matter who) — you just need your own copy so Render can read it.

**Do not fork if you can avoid it?** You can also point a new Render service at your existing repo. Render allows multiple services from one repo, but keep them in **separate services**, never two instances of the same service.

---

## Step 4 — Create the Render service

### Option A — Blueprint (recommended, uses `render.yaml`)

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New +** → **Blueprint**.
2. Connect GitHub and select your forked repo.
3. Render reads `render.yaml` and shows the service `tg-qna-bot` on the **Free** plan.
4. It asks for the `sync: false` variables — enter **`BOT_TOKEN`** and **`OWNER_ID`** now (from Steps 1–2).
5. Click **Apply** / **Create**.

**Two things that make a blueprint deploy look dead when it isn't:**

> **`OWNER_ID` and `ENCRYPTION_KEY` are generated once you type a value.** If you
> skip `ENCRYPTION_KEY`, the bot generates one into the container disk — and Render
> Free wipes that disk on every restart, which throws away the encrypted Telegram
> session with it. Generate a key first and paste it in so the value is stable:
>
> ```bash
> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```
>
> **It must be 32 url-safe base64 bytes** (44 characters ending in `=`). A random
> string from Render's "Generate" button will not work — if you paste one, the
> bot now logs a clear `ENCRYPTION_KEY is not a valid Fernet key` warning and keeps
> running, but every secret stored under the old key becomes unreadable.

> **Nothing is enabled yet, so silence is expected.** A fresh deployment has no
> groups in it. While the logs show `No saved Telethon session yet`, the bot is
> waiting for `/setup`, not broken. After setup, open
> **/dashboard → 👥 Select & Configure Groups** — that first open imports your
> joined groups — then tap each group you want answered.

### Option B — Web Service (manual)

**New +** → **Web Service** → pick the repo, then set exactly:

| Setting | Value |
|---|---|
| Runtime | `Python 3` |
| Instance Type | **Free** |
| Build Command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start Command | `PYTHONPATH=src python -m qna_bot` |
| Health Check Path | `/health` |

Then under **Environment**, add:

| Key | Value |
|---|---|
| `PYTHON_VERSION` | `3.12.7` |
| `BOT_TOKEN` | the token from Step 1 |
| `OWNER_ID` | the id from Step 2 |
| `LOG_LEVEL` | `INFO` |

Click **Create Web Service** and wait for the log to reach `Health HTTP server listening` and **Live**.

> **Leave `BOT_STATE_B64` empty for now.** It is only for *restoring* a deployment. Filling it with an old snapshot is the most common way people accidentally import someone else's session. You will add it in Step 8.

---

## Step 5 — Open the dashboard bot

1. In Telegram, open **your new bot** (the one from @BotFather) and send `/start`.
2. Because `OWNER_ID` is set, it should greet **only** that account.

If it instead asks for a **pairing code**, `OWNER_ID` did not take — open Render → **Logs** and look for:

```
PAIRING CODE:  A1B2C3
```

Send that code to the bot to claim ownership, then fix `OWNER_ID` in Render so it cannot happen again.

---

## Step 6 — Run the setup wizard (Steps 1–4 inside Telegram)

On a fresh deployment `/start` opens the wizard **directly** — there is no "Run Setup Wizard" button until setup is finished. To restart it later, use `/setup` or **⚙️ Account & Setup → 🧭 Re-run Full Setup Wizard**.

### Wizard Step 1 / 4 — Telegram API credentials

The bot asks for an **API ID** and **API Hash**. These belong to the *new* account's Telegram developer app:

1. Open [my.telegram.org](https://my.telegram.org) and log in **as the target account**.
2. **API development tools** → fill in any app name/short name → **Create**.
3. Copy **App api_id** (a number) and **App api_hash** (32 hex characters).
4. Send the number first, then the hash, to the bot.

> You *may* reuse the same `api_id`/`api_hash` for multiple accounts — they identify an *app*, not a user. Using a separate one per account is tidier and avoids rate-limit coupling.

### Wizard Step 2 / 4 — Log in the Telegram account

This is the account that will actually **type the answers in groups** — a normal user account, not a bot.

You are offered four routes, in order of how likely each one is to work **with one phone**:

| Route | When to use it |
|---|---|
| **📧 Login With Email Code** | **Start here.** Telegram emails the login code instead of sending it to the Telegram app or by SMS. This is the one channel it still delivers to third-party apps, so it does not fail silently. |
| **Phone + OTP keypad** | If you know Telegram delivers codes to this account. Send the number in international format, e.g. `+251912345678` (country code, no leading `0`). |
| **QR code (second device)** | Tap **📷 Make a QR Code**, then on another device: **Settings → Devices → Link Desktop Device**. The code auto-renews, so it stays scannable. **This route needs a camera and a second screen — see below.** |
| **Paste StringSession** | Only if you already have a `Telethon` session string for that account. |

If a code is required, the bot tells you **where** Telegram sent it (usually a service message *inside* the Telegram app, not SMS), then gives you an inline keypad. Tap **🔐** and enter **2FA** if the account has it.

> **Codes that never arrive are not your fault.** Telegram stopped sending login codes by SMS to third-party clients in Feb 2023, and since early 2026 it also silently withholds the in-app code for many accounts: the request succeeds and nothing is delivered. Tap **📧 No code coming? Get it by email** instead of retrying — retries only add flood waits.

#### Email login, step by step

1. Tap **📧 Login With Email Code**.
2. Send the email address to use.
3. Telegram emails a short verification code — send it to the bot.
4. The bot verifies it with Telegram, which then sends the **login code to that same inbox** — send that too.
5. If the account has 2FA, send the cloud password.

> The address becomes this account's **Telegram login email**. That is a real account change Telegram makes, and it is what makes future logins reliable.

#### Why the QR code cannot be done on one phone

Telegram **refuses** to let an app approve a QR login from a link. Its own TDLib documentation is explicit about `internalLinkTypeQrCodeAuthentication`: the token *"must be scanned from QR-code using in-app camera"*. Tapping such a link inside Telegram only shows:

> *"This code can be used to allow someone to log in to your Telegram account. To confirm Telegram login, please go to Settings > Devices > Scan QR and scan the code."*

This is deliberate anti-phishing design — otherwise a link in any chat could hand your account to a stranger — so **no workaround exists**. A QR login always needs the camera on a second screen. If you only have one phone, use the **email code**.

#### If you do want the QR route

The bot's HTTP server can also show the login token as a web page (`/login/<id>`) for the case where your browser *does* hand the link to Telegram. It needs a public address: on Render this is automatic (`RENDER_EXTERNAL_URL`); elsewhere set `PUBLIC_URL=https://your-domain`. Without either, the bot silently falls back to the QR image only.

### Wizard Step 3 / 4 — AI provider and key

1. Pick a provider. **⚡ Groq** is the fastest and has a free tier: [console.groq.com/keys](https://console.groq.com/keys).
2. Paste **that account's own** API key. The bot tests it live and deletes your message.
3. Choose a model — `openai/gpt-oss-120b` is the default and the right choice for speed.

> **Use a separate AI key per account.** Sharing one key means one account's rate limits silently throttle the other, and you cannot tell whose usage is whose.

### Wizard Step 4 / 4 — Pick groups

The bot syncs the groups the logged-in account is in. Toggle **✅** on the giveaway groups to monitor. Tap **⚙️** on any group to set:

- **Who triggers** — `👑 Admins Only` is safest (stops other members baiting the account)
- **Detection mode** — `🔥 Giveaway Aggressive` for giveaways that skip the `?`
- **Answer style**, **cooldown**, **reply to question**, **local math**, **📷 photo answers**

Then tap **✅ Finish Setup & Open Dashboard**.

---

## Step 7 — Verify it works

If a login code never arrives, send **`/logindiagnose`** — it prints where Telegram actually delivered the code and what to try next.

1. Send a real question in one of the selected groups, or use the dashboard's **🧪 Live Q&A Simulator** to test without waiting for a giveaway.
2. If nothing happens, send **`/diag`** to the bot. It prints, per group, exactly why each message was or was not answered, plus the last 8 decisions.

> **`/diag` is the first thing to read, always.** It states plainly whether the group list is empty, whether no group is enabled, whether the responder is paused, whether the AI key is missing, and whether the only messages arriving came from the bot's own account. Every one of those is a different fix, and none of them throw an error anywhere else.
3. **`/testanswer`** runs one question through the local filter *and* the AI stage and shows both results.

---

## Step 8 — Make it survive Render restarts (optional but important)

Render Free wipes the local disk on every restart, which logs the userbot out. Fix:

1. In the dashboard: **⚙️ Account & Setup** → **☁️ Backup This Deployment**.
2. Copy the string.
3. Render → your service → **Environment** → add `BOT_STATE_B64` = that string → **Save Changes**.

> ⚠️ **That backup contains the account's Telegram session and AI keys.** Anyone holding it can log in as that account. Keep it in that service's own environment and never send it to anyone.

To carry **group settings** to a *third* account later, use **🎁 Share Setup With Someone** instead — it contains no credentials at all.

---

## Step 9 — Keep it awake

Render Free sleeps after **15 minutes** with no inbound HTTP traffic — the userbot stops answering while asleep.

1. Sign up at [cron-job.org](https://cron-job.org) or [uptimerobot.com](https://uptimerobot.com) (free).
2. Add an HTTP monitor for `https://<your-service-name>.onrender.com/health`.
3. Set the interval to **5 minutes**.

---

## Running two accounts side by side

| Do | Don't |
|---|---|
| Separate Render service per account | Two instances of the same service |
| Separate `BOT_TOKEN` | One @BotFather bot shared by both |
| Separate `OWNER_ID` | The same `OWNER_ID` on both |
| Separate AI key | One key shared across both |
| Separate `BOT_STATE_B64` | Copying a backup between deployments |

The bot enforces the last one: importing a snapshot into a deployment that **already has an owner** silently refuses every credential and logs a warning, because importing a session would otherwise hijack that deployment.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Service shows a **frowning face / "deploy failed"** and never goes Live | Almost always a build or boot crash, not Telegram | Read **Logs** — the last line names the cause. A bad `ENCRYPTION_KEY` is now logged as a warning and no longer stops the boot |
| Bot asks for a **pairing code** | `OWNER_ID` missing or wrong | Check Render env; read the code from Logs |
| Dashboard says **⛔ Unauthorized** | You are messaging from a different Telegram account than `OWNER_ID` | Use the right account, or correct `OWNER_ID` |
| Bot is **Live** but silent, `/diag` says **No groups synced yet** | Fresh deploy: the group list is empty | `/dashboard` → 👥 Select & Configure Groups (first open imports your groups), then enable the ones to answer in |
| Logs show `No saved Telethon session yet` | Setup wizard has not been run | Send `/start` to the bot and finish the wizard |
| Logs show `ENCRYPTION_KEY is not a valid Fernet key` | Something other than a Fernet key was pasted | Generate one with `Fernet.generate_key()` and set it; secrets stored under the old key cannot be read |
| Bot is **Live** but silent | Userbot not logged in, or paused | `/diag` → follow the verdict line |
| Answers **never appear** in groups | Logged-in account is not an admin there, or group not enabled | `/diag` shows the per-group skip reason |
| Logged out after every deploy | No `BOT_STATE_B64` | Step 8 |
| Both deployments answer the same message | The **same Telegram user account** is logged into both (one account, two bots) | Log out one session in Telegram → Settings → Devices. Two *different* accounts can safely watch the same group — whoever is faster wins, which is not useful but is not broken. |
| `model_not_found` | A retired model ID is saved | Pick a model again in the AI menu; the bot also auto-swaps known retired IDs |

---

## Quick reference — every env var

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | @BotFather token for *this* bot |
| `OWNER_ID` | ✅ strongly | Numeric Telegram id allowed to use the dashboard |
| `PYTHON_VERSION` | recommended | `3.12.7` |
| `LOG_LEVEL` | optional | `INFO` (default) |
| `BOT_STATE_B64` | optional | Restores login + settings after a restart |
| `TG_API_ID` / `TG_API_HASH` | optional | Skips wizard Step 1 |
| `TG_STRING_SESSION` | optional | Skips wizard Step 2 (advanced) |
| `PUBLIC_URL` | optional | Base URL for the browser login page (`/login/<id>`). Render sets `RENDER_EXTERNAL_URL` automatically; without either, the bot falls back to the QR image |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | optional | Skips wizard Step 3 |
| `EXNESS_API_KEY` / `EXNESS_PRIVATE_KEY` / `EXNESS_ACCOUNT_ID` / `EXNESS_GATEWAY_URL` | optional | Skips the Exness step of the AI menu. All four are needed together: every Forex price is read from your own Exness trading account (read-only key), and without them Forex groups answer `No live data` |
| `ENCRYPTION_KEY` | recommended | Encryption key for secrets at rest in SQLite. **Must be 32 url-safe base64 bytes** — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. If unset, one is generated into `data/.secret.key`, which Render Free **wipes on every restart**, taking the encrypted account session with it. An invalid value is logged as an error and ignored rather than stopping the boot. Not needed for `BOT_STATE_B64` restores (those carry plaintext through the export). |
| `DATA_DIR` | optional | SQLite location (default `./data`) |
| `PORT` | auto on Render | Enables the `/health` server |
