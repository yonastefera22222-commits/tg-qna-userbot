# ⚡ Telegram Q&A Giveaway Userbot + Dashboard Controller (`tg-qna-bot`)

Turn your Telegram account into an ultra-fast, free AI Q&A userbot designed for **Telegram Q&A giveaways**, controlled entirely through a private **Dashboard Controller Bot** with an interactive `/start` setup wizard — **ready for Render Free Tier** (and Docker/VPS).

---

## ✨ Key Features

- **🧭 Full In-Bot Setup Wizard (`/start`)**: Walks you step-by-step inside Telegram through API credentials, logging in your Telegram account, adding an AI key, and picking which group chats to monitor.
- **📷 QR-Code Login (no code delivery at all)**: Scan a QR code sent to you in the dashboard chat with **Settings → Devices → Link Desktop Device**. No SMS, no service message, no email, no flood wait — the most reliable way in when codes simply will not arrive. Telegram's QR token only lives ~30 seconds, so the bot **auto-renews it on the same connection and keeps going**: the code stays scannable while the screen is open (tap ♻️ Refresh QR Image to swap in the newest one).
- **📧 Automatic Login-Email Unblock**: If Telegram answers `SentCodeTypeSetUpEmailRequired` it will *never* send a code, no matter how often you retry. The wizard now detects this, asks for your email, emails you a verification code, verifies it, and then requests the real login code — all inside the chat.
- **🩺 `/diag` + `/testanswer`**: `/diag` reports every gate between an incoming message and a reply — account connected, responder paused, enabled groups, per-group who-triggers/detection, and the last 8 messages with the exact reason each was answered or skipped. `/testanswer` runs the real filter + AI call on a sample question so you can prove the pipeline works without waiting for a giveaway.
- **🛡 Admin detection fails open**: admin lists need a supergroup and permission to list members; on basic or restricted groups Telegram refuses. That used to return an empty admin set and silently drop every message — it now logs a warning and answers anyway.
- **🩺 `/logindiagnose` Command**: Prints the raw Telegram response type, the real delivery channel, the code length, and whether the server can even reach Telegram — so "no code" becomes a definitive answer instead of a guess.
- **💾 Persistent Wizard State**: Conversation state is stored in SQLite, so a Render restart (free tier sleeps after 15 min) or a duplicate instance no longer silently swallows the step you are on.
- **🔢 Anti-Invalidation Inline OTP Keypad**: Enter your Telegram login code by tapping inline keypad buttons (so Telegram never invalidates your login code). Supports 2FA passwords and `StringSession` paste.
- **📨 Tells You Exactly Where the Code Went**: Telegram rarely sends login codes by SMS — for most accounts the code arrives as a **service message inside the Telegram app**, or by email/call/confirmation prompt. The wizard now reports the real delivery channel (and code length) Telegram chose, instead of always claiming "sent via Telegram".
- **🩺 Login Troubleshooting Built In**: A **❓ No code received?** button gives a step-by-step checklist (service chat, SMS/email, country code format, alternate channel, flood limits). Common failures are translated into plain instructions: `FloodWait` shows the exact seconds, a bad number tells you the correct `+251...` format, a rejected API Hash links back to `my.telegram.org`, and a dead login attempt offers a one-tap retry instead of an opaque error.
- **☁️ Render Free Tier Ready**:
  - Built-in async HTTP `/health` endpoint binding `0.0.0.0:$PORT` (required by Render Free Web Services).
  - Includes **`render.yaml`** for 1-click Blueprint deployment.
  - **1-Tap `☁️ Export Render Config (`BOT_STATE_B64`)`** button in the Dashboard Bot so your Telegram login, AI keys, and selected groups survive Render Free's ephemeral disk restarts automatically!
- **👥 Interactive Group Selector & Per-Group Giveaway Rules**:
  - Paginated `✅ / ⬜` checklist synced straight from your Telegram account + instant group search.
  - Per-group toggles:
    - **Who triggers**: `👑 Admins Only` (safest for giveaways, supports anonymous group admins!) / `👥 Humans Only` / `🌐 Everyone (+Bots)`.
    - **Answer Style**: `⚡ Giveaway (Ultra-Short bare answer)` / `💬 Short (1 sentence)` / `📖 Detailed`.
    - **Detection Mode**: `🔥 Giveaway Aggressive` / `⚖️ Balanced` / `🔒 Strict (? required)`.
    - **↩️ Reply to Question**: Replies directly to the question message (`event.reply`).
    - **🧮 Instant Local Math Solver**: Solves arithmetic giveaways (`47 * 83`, `15% of 240`, `sqrt(144)`, `(12 + 8) × 5`) in **<1ms** locally with zero API latency!
    - **📷 Answer Photo Questions**: Answers questions sent as a **picture** — a screenshot of a quiz, a photographed handwritten problem, or an image with a caption. Works even when the photo has **no text at all**. Photos up to **4 MB** are downloaded and passed to a vision model.
    - **⏱ Cooldown**, **⏳ Optional Human Delay**, **🏷 Custom Trigger Keywords**, and **🧠 Context Messages**.
- **🤖 Multi-Provider Fast & Free AI + Automatic Fallback**:
  - **⚡ Groq** (`openai/gpt-oss-120b` / `openai/gpt-oss-20b`, ~500–1000 tok/s — ideal primary for speed giveaways). Retired model IDs are detected automatically and swapped for the live replacement, so a stale model never looks like a bad API key.
  - **🧠 Google Gemini** (high accuracy free tier — Flash-Lite / Flash).
  - **🌐 OpenRouter** (`openrouter/free` auto-router + free model catalog).
  - Single-shot classification + answering over persistent HTTP/2 connection pooling + automatic failover on HTTP 429/errors.
- **📷 Photo Questions (Vision)**: Screenshots and photos are answered like text questions. If your configured model cannot read images, the bot **automatically switches to a vision-capable model just for that message** and switches back afterwards — Groq `gpt-oss` → `qwen/qwen3.8-27b`, and every Gemini Flash model reads images natively. The instant local math solver is bypassed for photos (the arithmetic is *inside* the image).
- **🧪 Live Q&A Simulator & Speed Benchmark**: Test sample giveaway questions and benchmark your providers right inside the dashboard bot.

---

## ☁️ Deploying to Render Free Tier (Step-by-Step)

### Step 1 — Push this repo to your GitHub (`tg-qna-bot`)

> **⚠️ Security reminder:** Never commit `.env` or your `BOT_TOKEN` to GitHub! Only set `BOT_TOKEN` inside Render's **Environment Variables** dashboard.

```bash
cd tg-qna-bot
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/tg-qna-bot.git
git push -u origin main
```

### Step 2 — Deploy on Render (Free Tier)

1. Go to [dashboard.render.com](https://dashboard.render.com) → click **New +** → **Web Service** *(Render Free Tier only supports Web Services — this bot includes a built-in `/health` HTTP server specifically for it!)*.
2. Connect your GitHub account and select your **`tg-qna-bot`** repository.
3. Verify these settings *(auto-filled if you use **New + → Blueprint** with `render.yaml`)*:
   - **Runtime**: `Python 3`
   - **Instance Type**: **Free**
   - **Build Command**: `pip install --upgrade pip && pip install -r requirements.txt`
   - **Start Command**: `PYTHONPATH=src python -m qna_bot`
   - **Health Check Path**: `/health`
4. Scroll to **Environment Variables** and add:
   - `BOT_TOKEN` = *(your BotFather token)*
   - `OWNER_ID` = *(your numeric Telegram User ID from [@userinfobot](https://t.me/userinfobot) — strongly recommended so nobody else can access your bot!)*
   - `PYTHON_VERSION` = `3.12.7`
5. Click **Create Web Service** and wait ~2 minutes for Render to show **Live**.

### Step 3 — Walk Through the In-Bot Setup Wizard

1. Open your Dashboard Bot in Telegram and send **`/start`**.
   - *(If you skipped `OWNER_ID`, check Render's **Logs** tab for the 6-character `PAIRING CODE` and send it to the bot first.)*
2. Follow the 4 steps right inside Telegram:
   - **Step 1**: Enter your Telegram `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) → *API development tools*.
   - **Step 2**: If codes never arrive, use **📷 Login with QR Code** (no SMS needed). Otherwise send your phone number in **international format** (e.g. `+251912345678` — country code, no leading `0`). The bot then tells you **where Telegram sent the code** (usually a service message *inside* the Telegram app, not an SMS), then tap the digits on the **inline numeric keypad** (+ 2FA password if enabled). No code? Tap **❓ No code received?**, then **🔄 Resend Code** — Telegram switches channel (app message → SMS → call).
   - **Step 3**: Pick a free AI provider (recommended: **⚡ Groq** from [console.groq.com/keys](https://console.groq.com/keys)), paste your API key (tested live with ms speed!), and choose a model.
   - **Step 4**: Toggle `✅` on the giveaway groups you want to monitor, tap `⚙️` on any group to customize rules, and tap **✅ Finish Setup & Open Dashboard**.

### Step 4 — Make Your Setup Survive Render Free Restarts (Important!)

Because Render Free Tier wipes local container disk whenever the service restarts or redeploys:

1. In your Dashboard Bot, tap **⚙️ Account & Setup** → **☁️ Export Render Config (`BOT_STATE_B64`)**.
2. Copy the generated code string.
3. In Render → your service → **Environment** → add:
   - Key: `BOT_STATE_B64`
   - Value: *(paste the exported string)*
4. Click **Save Changes** (Render redeploys and automatically restores your login, AI keys, and selected groups!).
5. Tap **🗑 Delete Secret Message** in Telegram to keep your chat clean.

*(Whenever you later enable new groups or add a fallback AI key, export `BOT_STATE_B64` again and update it in Render so changes persist across restarts.)*

### Step 5 — Keep Render Free Awake 24/7 (Optional but Recommended)

Render Free Web Services sleep after **15 minutes** without inbound HTTP traffic. To keep your userbot awake during giveaways:
1. Create a free account at [uptimerobot.com](https://uptimerobot.com) or [cron-job.org](https://cron-job.org).
2. Add an HTTP monitor pointing to:
   `https://<your-render-service-name>.onrender.com/health`
3. Set the check interval to **5 or 10 minutes**.

---

## 🐳 Alternative: Docker Compose / VPS Deployment

```bash
cp .env.example .env
nano .env   # set BOT_TOKEN and OWNER_ID
docker compose up -d --build
docker compose logs -f
```

---

## 🏆 Recommended Giveaway Settings

| Setting | Recommended Value | Why |
|---|---|---|
| **Primary AI** | Groq · `openai/gpt-oss-120b` (or `openai/gpt-oss-20b`) | Groq's live free models — Llama was retired from the free tier on 2026-08-16 |
| **Fallback AI** | Google Gemini · `gemini-3.5-flash-lite` | Auto-failover if Groq hits free RPM limits |
| **Who Triggers** | `👑 Admins Only` *(or `🌐 Everyone (+Bots)` if a bot asks)* | Stops other participants from baiting your account |
| **Answer Style** | `⚡ Giveaway (Ultra-Short)` | Bare exact answers (`Paris`, `42`) with zero fluff |
| **Detection Mode** | `🔥 Giveaway Aggressive` | Catches prompts without `?` like `Capital of Kenya` |
| **Instant Local Math** | `ON` | Solves arithmetic giveaways in `<1ms` locally |
| **Reply to Question** | `ON` | Replies directly to the host's question message |
| **📷 Answer Photo Questions** | `ON` | Answers screenshot/photo quizzes — turn **off** to save paid vision credits |

---

## 🧪 Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```
