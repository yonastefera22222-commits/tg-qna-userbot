# Running the bot off Render (no credit card)

Render's free instances run out. This is what actually works when you **cannot or
will not add a card**, ranked by how much of the problem each one solves.

Written against the current code: the image builds from `Dockerfile`, the bot
listens for health checks on `$PORT` (optional), and everything that must survive
a restart lives in `$DATA_DIR`. Nothing here needs a code change.

---

## What this bot needs, and why most "free hosting" lists are useless

This is a **long-running userbot**, not a web app:

| Requirement | Why |
|---|---|
| **A process that keeps running** | Telethon holds a persistent MTProto connection. No connection = no answering. |
| **No idle sleep** | Telegram pushes updates down a live socket. A host that sleeps after N minutes of *inbound HTTP* traffic kills every answer overnight, because nothing is calling `/health`. |
| **Persistent disk, or an offline state backup** | `data/` holds the SQLite DB and the Telegram session. Lose it and the bot logs out and needs re-pairing. |
| **Unrestricted outbound HTTPS** | Telegram, your AI provider, the Exness API. |
| **~256 MB RAM, trivial CPU** | The bot is small. |

So **rule out the whole serverless family**: Vercel, Netlify, Cloudflare Workers,
AWS Lambda, Deno Deploy, Google Cloud Run. They run code in response to a request
and then stop. A bot with no inbound traffic gets no invocations and is silent.
They are good products; they are the wrong shape.

> ⚠️ **One thing to know before hosting anywhere:** a Telegram *userbot* runs on
> your account's session string, which is full access to that account. Only put it
> on a host you'd trust with your phone. That rules out the "free VPS, no card"
> sites that advertise 512 MB forever — they exist, they are usually short-lived or
> worse, and your session is the payment. Nothing below asks you to trust an
> unknown operator with it.

---

## The short answer, given no card

| Rank | Option | Card | Always on? | Verdict |
|---|---|---|---|---|
| **1** | **Northflank Sandbox** | **No card needed** (their pricing page) | **Yes — "always on, no sleeping"** | The only managed host that is both card-free *and* doesn't sleep. Try this first. |
| **2** | **Your own device** (Android phone via Termux, or an old laptop / Raspberry Pi) | No | Yes | Slowest to set up, by far the most reliable, and the session never leaves your control. |
| **3** | **Wait for Render to reset** | No | No (sleeps) | Free instances are per workspace per month — they come back. Fine as a stopgap. |
| — | Oracle Cloud Always Free | **Card required** | Yes | The best free host overall, but out of scope here. Appendix at the end if you ever get a card. |
| — | Koyeb | Card + $29 hold | No (sleeps after 1 h) | Out. |
| — | Back4App, SnapDeploy | No card | **No** — 60 min per deploy / sleeps after 15 min | Out for a live bot; okay for a demo. |
| — | Hugging Face Spaces | Docker Spaces now need a paid plan | — | Out. |
| — | Fly.io, Railway, Heroku | No usable free tier for new accounts | — | Out. |

---

## 1. Northflank Sandbox — the best card-free option

Their pricing page states, for the free **Sandbox** tier: 2 services, 2 jobs and
1 database, up to **$20/month of resources**, and **"Always on, no sleeping"**, with
**"No card needed"**.

The arithmetic works out for this bot on the smallest compute plan:

| Resource | Rate | For this bot |
|---|---|---|
| `nf-compute-50` (0.5 vCPU, 1 GB) | $12.00/month | $12.00 |
| Persistent disk, 1 GiB | $0.11657/GiB/month | $0.12 |
| **Total** | | **≈$12.12**, inside the $20 allowance |

So one always-on service plus a small volume fits with room to spare. You could run
a **second** bot free — two accounts, two services, still inside the allowance.

Their billing docs elsewhere still say a payment method is required to create
resources. If signup asks for a card, you have hit that contradiction and this route
is closed to you; go to option 2. It costs nothing to try.

### Deploy steps

1. Sign up at `app.northflank.com/signup` (GitHub login, no card if it holds true).
2. **Create → Service → Build from repo**, point at your fork of this repo.
3. Build type: **Dockerfile**. The repo has one; no build command needed.
4. Port **8080**, health check path **`/health`**, protocol HTTP.
5. Compute: **nf-compute-50** (0.5 vCPU / 1 GB). Set the service to run
   **1 instance, always**.
6. **Volumes → Add volume**, mount path **`/app/data`**, 1 GiB. This is the part
   that stops the bot logging out on every redeploy — the image already declares
   `VOLUME ["/app/data"]` and the app writes there via `DATA_DIR`.
7. Environment / secret group — the same variables as Render:

```env
BOT_TOKEN=123456:ABC...
OWNER_ID=your_telegram_numeric_id
ENCRYPTION_KEY=<44-char Fernet key>
DATA_DIR=/app/data
PORT=8080
LOG_LEVEL=INFO
PUBLIC_URL=https://<your-service>.northflank.app
```

8. Deploy, open the logs. With `OWNER_ID` set, `/start` goes straight to the wizard.

Generate the encryption key anywhere Python runs:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`ENCRYPTION_KEY` **must be the same value** across restarts and must not be
regenerated later — it decrypts the stored Telegram session and API keys. A new key
means the database is unreadable and the bot needs re-pairing.

### If it does sleep despite the claim

Check with a trivial test before trusting it: deploy, leave it 30 minutes untouched,
then `curl https://<service>/health`. If it cold-starts, the tier sleeps and the
userbot will be dead overnight. In that case use option 2.

---

## 2. Your own device — free forever, no card, no third party

Underrated: this bot needs 256 MB of RAM and a network connection. A phone in a
drawer does that.

### Android phone or tablet, via Termux

1. Install **Termux** from **F-Droid** (not the Play Store — that build is
   outdated and breaks package installs).
2. Keep the process alive when the screen is off:

```bash
termux-wake-lock
```

3. Install the toolchain. Termux has no prebuilt wheels for everything, so a few
   packages need a compiler:

```bash
pkg update && pkg upgrade -y
pkg install -y python git clang openssl libffi rust binutils
termux-wake-lock
```

4. Clone and install:

```bash
git clone https://github.com/yonastefera22222-commits/tg-qna-userbot.git
cd tg-qna-userbot
python -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install cryptg || true
```

> If `aiohttp` fails to build, install the system copy first and let pip reuse it:
> `pkg install python-aiohttp`, then re-run the pip install.

5. Configure and run:

```bash
touch .env && nano .env     # BOT_TOKEN, OWNER_ID, ENCRYPTION_KEY (no PORT needed)
.venv/bin/python -m qna_bot
```

Without `PORT` the bot skips the HTTP server and just runs the userbot — which is
all a phone needs.

6. Survive reboots and battery optimisation:
   - Android settings → Apps → Termux → Battery → **Unrestricted**;
   - enable Termux's **Acquire wakelock** notification action;
   - to restart on boot, Termux:Boot (same F-Droid source) with a `~/.termux/boot/`
     script that calls `termux-wake-lock` and starts the bot under `tmux`.

`tmux` is worth the extra install: `pkg install tmux`, then run the bot inside a
session so a dropped SSH or a closed terminal doesn't kill it.

### Old laptop, Raspberry Pi, or a spare desktop

The path most people should take if they have any spare hardware:

```bash
sudo apt-get install -y python3-venv git
git clone https://github.com/yonastefera22222-commits/tg-qna-userbot.git /opt/tg-qna-userbot
cd /opt/tg-qna-userbot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p data
sudo cp deploy/tg-qna-userbot.service /etc/systemd/system/
```

Edit the unit's `User=` and `WorkingDirectory=` to match, write `.env` as above,
then:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tg-qna-userbot
journalctl -u tg-qna-userbot -f
```

Or with Docker, from the repo root: `docker compose up -d --build` — the compose
file already mounts `./data`, sets `restart: unless-stopped` and reads `.env`.

A Raspberry Pi costs ~$5 of electricity a year at this load and never asks for a
card. It is the most dependable free host there is, because nobody can take it away.

---

## 3. Waiting for Render

The free instance limit is **per workspace per month**, so it resets. If you are
only days away, the least work is to wait — and this is a good moment to make the
move survivable, because Render Free **wipes its disk on every restart**:

1. In the dashboard, take the **state backup** (`BOT_STATE_B64`) *before* you lose
   access, and store it somewhere safe.
2. Keep `ENCRYPTION_KEY` too — the backup is useless without it.

With both, a move to any other host is a copy-paste and needs no new Telegram
login.

---

## Moving hosts without re-pairing

Telegram limits how many times one number can log in per day, and repeated logins
from new IPs look like account takeover. Do this instead:

1. On the **old** host, take the state backup → `BOT_STATE_B64`.
2. Set `BOT_STATE_B64` **and the same `ENCRYPTION_KEY`** on the new host.
3. Start the new host, confirm `/diag` shows groups enabled and a test question is
   answered.
4. **Only then** stop/suspend the old one.

Two instances polling one Telegram session is the one failure that looks like
random breakage: both connect, Telegram splits the updates between them, answers
fire unpredictably. Move, verify, switch off — never both at once. A *second*
deployment needs its own `BOT_TOKEN` from @BotFather for the same reason.

---

## Check after any move

- `curl http://<host>:8080/health` → JSON with `active_groups ≥ 1`
- `/diag` in Telegram: no `skip:not_admin`, no `skip:group_not_enabled`
- one real question in a test group gets a bare answer with no timestamp
- **restart the host** and confirm you are still logged in — that is the
  persistence test, and the one free tiers usually fail
- nothing is billing you. On Northflank, watch the usage figure stays under $20/mo;
  on Oracle (appendix), a single instance over 4 OCPU or 24 GB silently becomes
  chargeable.

---

## Appendix: Oracle Cloud Always Free, if you ever get a card

The strongest free option overall — a real always-on VM with 4 ARM cores and
24 GB RAM, free indefinitely — but it requires a credit or debit card at signup
for identity verification (prepaid and virtual cards are rejected). Kept here for
completeness:

1. **Create instance** → Ubuntu 24.04 → **Change shape → Ampere →
   `VM.Standard.A1.Flex`**, 4 OCPU / 24 GB (console shows *Always Free-eligible*),
   boot volume 50 GB, add your SSH key.
2. Open port 8080 in the VCN security list **and** in the instance firewall —
   Oracle's Ubuntu images block everything but SSH, which is the classic "the app
   runs but the URL times out":
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. Install exactly as in [option 2](#old-laptop-raspberry-pi-or-a-spare-desktop).
4. Caveats: ARM capacity is frequently "out of capacity" on free accounts (a
   Pay-As-You-Go upgrade keeps Always Free resources free and provisions in
   minutes), and idle VMs can be reclaimed — a weekly restart cron is cheap
   insurance.
