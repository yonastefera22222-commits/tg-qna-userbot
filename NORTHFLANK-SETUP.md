# Setting up on Northflank, step by step

Northflank's free **Sandbox** tier gives 2 services, 2 jobs, 1 database, up to
**$20/month of resources**, and — the part that matters for a userbot — **"always
on, no sleeping"**. This bot on the smallest compute plan plus a 1 GiB volume costs
about **$12.12/month**, so you can run **two bots** inside the allowance.

> The pricing page says **"No card needed"**. Their billing docs elsewhere still say
> a payment method is required to create resources. If signup asks for a card, that
> is the contradiction and this route is closed — use
> [DEPLOY-ALTERNATIVES.md](DEPLOY-ALTERNATIVES.md) instead.

Each userbot needs **its own @BotFather bot token**. Two deployments sharing one
token fight over Telegram's update stream and both look broken.

---

## Before you start: three things that will bite you

Read these now, they cost people hours.

**1. Repository visibility.** Northflank deploys from GitHub. Your fork must be
**public** (or you must install the Northflank GitHub App on the private repo with
access). This repo contains no secrets — `.env` is gitignored — so public is fine.

**2. The port is not the bot.** This is a *userbot*: it answers through Telethon,
not through HTTP. The HTTP server on `PORT` exists only for the platform's health
check and the browser login page. If the health check fails, Northflank will
restart the container in a loop and the bot will never stay up. The image now
handles that correctly, but it is why **`PORT=8080` is mandatory**, not optional.

**3. The volume is the whole game.** `data/` holds the SQLite database *and* your
Telegram session. Without a persistent volume, every redeploy wipes it and the bot
logs out. The image declares `VOLUME ["/app/data"]` and the entrypoint makes a
root-owned volume usable — but **you must still mount one**.

---

## Step 1 — Create the account and project

1. Go to `app.northflank.com/signup`, sign in with GitHub.
2. **Create project** → name it `telegram-bots`. One project can hold both bots.

## Step 2 — Link your repository

1. In the project: **Repositories → Add repository** (or start a service and let it
   prompt you).
2. Authorise Northflank against GitHub and pick **your fork** of
   `tg-qna-userbot`.

If the repo does not appear, the GitHub App was not granted access to it — fix that
in GitHub → Settings → Applications → Northflank.

## Step 3 — Create the service

**Create service → Combined service** (fill in:)

| Field | Value |
|---|---|
| Name | `tg-qna-bot` (or `bot-2`) |
| Repository | your fork, branch `main` |
| Build type | **Dockerfile** |
| Dockerfile path | `/Dockerfile` |
| Build context | `/` |

Do **not** choose a buildpack. The repo's Dockerfile is what pulls in `setpriv` and
the entrypoint that makes the volume writable.

## Step 4 — Networking (this is the make-or-break step)

**Networking → Add port**

| Field | Value |
|---|---|
| Name | `http` |
| Internal port | **8080** |
| Protocol | **HTTP** |
| Public | **on** |

**Health checks → Add health check**

| Field | Value |
|---|---|
| Type | Liveness probe |
| Protocol | HTTP |
| Path | **`/health`** |
| Port | **8080** |
| Initial delay | **30** seconds |
| Period | 30 seconds |
| Timeout | 5 seconds |
| Failure threshold | 3 |

The initial delay matters: the bot opens a Telegram connection before the HTTP
server is ready, and a probe that starts at 5 s will kill a perfectly healthy
container on a cold start.

## Step 5 — Resources

**Compute:** `nf-compute-50` (0.5 vCPU / **1 GB** RAM) — $12/month, inside the
$20 allowance.

**Instances: 1.** Never scale above one. Two replicas of the same userbot share one
Telegram session, both connect, and answers fire unpredictably — this looks like a
bug in the bot and is not.

## Step 6 — Volume (do not skip)

**Volumes → Add volume**

| Field | Value |
|---|---|
| Name | `bot-data` |
| Size | **1 GiB** |
| Mount path | **`/app/data`** |

Mount path must be exactly `/app/data` — that is `DATA_DIR`, and it is where the
database, the encryption key file, and the Telegram session live.

The container starts as root, `chown`s this volume to `botuser`, then drops to
`botuser` with `setpriv`. That is why the Dockerfile no longer ends with
`USER botuser`: a container already running unprivileged cannot adopt a root-owned
volume, and it used to die on the first `mkdir` with a `PermissionError` that never
appeared in the logs.

## Step 7 — Environment variables

**Environment → Secret group → Add variables.** Use a secret group rather than
plain variables, so the values are masked in the UI.

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | **yes** | From @BotFather — one per bot |
| `OWNER_ID` | **yes** | Your numeric Telegram id (@userinfobot). Without it the bot logs a 6-character pairing code and the first person to send it owns the bot |
| `ENCRYPTION_KEY` | **yes** | 44-char Fernet key. Generate once, below |
| `PORT` | **yes** | `8080` — must match the port above |
| `DATA_DIR` | **yes** | `/app/data` — must match the volume |
| `PUBLIC_URL` | recommended | `https://<service>-<project>.northflank.app`, for the browser login page |
| `LOG_LEVEL` | optional | `INFO` |
| `BOT_STATE_B64` | only when migrating | Restores an existing login — see Step 10 |
| `EXNESS_API_KEY` / `EXNESS_PRIVATE_KEY` / `EXNESS_ACCOUNT_ID` / `EXNESS_GATEWAY_URL` | optional | All four, if you want live Forex prices. Can also be entered in the dashboard |

Generate the encryption key locally (or in the Northflank console):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **This key must never change.** It encrypts the stored Telegram session and API
> keys. Regenerate it later and the database is unreadable — the bot has to be
> re-paired from scratch.

## Step 8 — Deploy and watch the logs

Hit **Deploy**. The first build takes a few minutes (Python wheels).

In **Logs**, a healthy start looks like:

```text
INFO  qna_bot.main  Starting Telegram QnA userbot
INFO  qna_bot.health  Health server listening on 0.0.0.0:8080
```

Then send `/start` to your **dashboard bot** from the owner account. With `OWNER_ID`
set you go straight into the wizard — no pairing code.

### Reading a failed deploy

| What you see | Cause | Fix |
|---|---|---|
| `PermissionError: [Errno 13] /app/data` | Volume not mounted, or mounted at the wrong path | Mount at exactly `/app/data` |
| `entrypoint: ... not writable by uid` | The service runs as an overridden non-root user | Remove the run-as-user override so the entrypoint can chown |
| Health check failing, container restarting | `PORT` not set, or the port config missing | Add port 8080 HTTP and `PORT=8080` |
| `BOT_TOKEN is required` | Variable not attached to this service | Attach the secret group |
| Runs, then dies after ~15 min | That is Render behaviour, not Northflank | You are probably still on Render |
| `PAIRING CODE: XXXXXX` | `OWNER_ID` missing | Add it, redeploy; or claim with the code |

## Step 9 — Finish inside Telegram

1. `/start` → **Select & Configure Groups** → enable at least one group.
2. **AI Providers & Models** → add a Groq / Gemini / OpenRouter key.
3. `/diag` — it should show groups enabled and no `skip:not_admin`.
4. Ask a test question in a group and confirm a bare answer with no timestamp.

## Step 10 — A second bot

Same steps, different values:

| | Bot 1 | Bot 2 |
|---|---|---|
| Service name | `tg-qna-bot` | `bot-2` |
| `BOT_TOKEN` | its own | **its own — never share** |
| `OWNER_ID` | yours | the second owner's |
| `ENCRYPTION_KEY` | its own | its own |
| Volume | `bot-data` | `bot-data-2` → `/app/data` |

Both fit in the Sandbox allowance (≈$12.12 each, $20 of resources). Check
**Billing → Usage** after a day to confirm.

---

## Migrating an existing bot from Render

Do this instead of logging in again — Telegram rate-limits repeated logins and
treats new-IP logins as suspicious.

1. On the **Render** deployment, open the dashboard → **☁️ Backup This Deployment**
   and copy the `BOT_STATE_B64` string.
2. On Northflank, set `BOT_STATE_B64` **and the same `ENCRYPTION_KEY`**.
3. Deploy and confirm `/diag` shows the groups and answers flow.
4. **Then** suspend Render.

Never run both at once: two instances on one session split the Telegram updates and
answers fire at random. Move, verify, switch off.

## Keeping it alive

- **Logs**: if the service stops, the last 100 lines are in the Logs tab. A
  `FloodWaitError` means Telegram is throttling replies — raise the group cooldown.
- **Backups**: the dashboard's **Backup This Deployment** button produces the same
  `BOT_STATE_B64` string. Take one after setup, and again whenever you add groups.
  Keep it with `ENCRYPTION_KEY`; neither is usable alone.
- **Updating**: push to your fork's `main`; Northflank can redeploy on source change
  (enable it in the service's build settings). The volume survives, so you stay
  logged in.
- **Cost**: **Billing → Usage**. Two services at ~$12.12 against a $20 allowance is
  fine; a third would not be. If you see $20+, you have something oversized.
