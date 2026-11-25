# ⚡ Telegram Q&A Giveaway Userbot + Dashboard Controller (`tg-qna-bot`)

Turn your Telegram account into an ultra-fast, free AI Q&A userbot designed for **Telegram Q&A giveaways**, controlled entirely through a private **Dashboard Controller Bot** with an interactive `/start` setup wizard — **ready for Render Free Tier** (and Docker/VPS).

---

## ✨ Key Features

- **🧭 Full In-Bot Setup Wizard (`/start`)**: Walks you step-by-step inside Telegram through API credentials, logging in your Telegram account, adding an AI key, and picking which group chats to monitor.
- **📧 Login With Email Code — the channel that actually delivers**: Telegram stopped sending login codes by SMS to third-party clients in Feb 2023, and since early 2026 it silently withholds the in-app code for many accounts — the request succeeds, the hash is valid, and **nothing arrives** ([Telethon #4730](https://github.com/LonamiWebs/Telethon/issues/4730)). Email is different: Telegram's authorization docs list it as a first-class delivery method for third-party apps, and Telegram's own mail servers send it. The wizard now offers *"Get the code by email"* on the login screen, on the OTP keypad when nothing turns up, and on the *No code received?* page. It works mid-login, so you never restart.
- **🚪 No More OTP Dead Ends**: the code-keypad screen carries a **“No code coming? Get it by email”** escape hatch, and the *No code received?* help page leads with it instead of only listing ways to wait. Honest about the causes: no SMS for third-party apps, delivery silently withheld, and flood limits that retrying only makes worse.
- **📱 Honest Client Identity**: the login connection no longer announces itself as `Telethon on Python` — the fingerprint Telegram's anti-spam heuristics suppress codes for. It presents a normal mobile device profile.
- **📷 QR-Code Login — second device only, by design**: tap **📷 Make a QR Code** and scan it from **Settings → Devices → Link Desktop Device** on a device that is already signed in. The token auto-renews, so the code stays scannable.
  > **Why can't one phone approve a QR login?** Because Telegram refuses it. Its TDLib documents the restriction on `internalLinkTypeQrCodeAuthentication`: the link *"must be scanned from QR-code using in-app camera"*, and tapping it inside Telegram only answers *"go to Settings → Devices → Scan QR"*. That is deliberate anti-phishing behaviour — an app must never be able to approve a login from a link — so no workaround exists. **Use the email code instead when you only have one phone.**
  > *(An earlier version of this bot offered the `tg://login` token as a web link that "one-taps" approval. Telegram rejects that path, which is why it always showed the Scan QR dialog. The web page is still served for the case where your browser does hand the link to Telegram, but it no longer promises a one-phone approval.)*
- **📧 Automatic Login-Email Unblock**: If Telegram answers `SentCodeTypeSetUpEmailRequired` it will *never* send a code, no matter how often you retry. The wizard now detects this, asks for your email, emails you a verification code, verifies it, and then requests the real login code — all inside the chat.
- **🧪 Testing From A Second Account**: a userbot cannot answer itself, so messages sent from the *same* account the bot runs as are counted and skipped (`↩️ Own messages skipped`). Test from a different account. `/diag` now distinguishes all four silences: nothing arriving, only your own messages arriving, messages arriving but rejected by the sender filter, and messages arriving but rejected by the question filter.
- **📡 Channel Posts & Forwards**: a question posted on behalf of a channel — a forward from a news/tips channel, or an anonymous channel admin — carries the *channel's* sender id, which can never be an admin of the group. With **Who Triggers = 👑 Admins Only**, those were dropped permanently. A forward is now judged by the person who forwarded it, so an admin's forward is answered; a bare channel post needs **🌐 Everyone**. The skip reason in `/diag` says exactly which case it hit.
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
  - **1-tap `☁️ Backup This Deployment`** button in the Dashboard Bot so your Telegram login, AI keys, and selected groups survive Render Free's ephemeral disk restarts automatically — plus **`🎁 Share Setup With Someone`**, which exports groups and settings only and never your session or API keys.
- **👥 Interactive Group Selector & Per-Group Giveaway Rules**:
  - Paginated `✅ / ⬜` checklist synced straight from your Telegram account + instant group search.
  - Per-group toggles:
    - **Who triggers**: `👑 Admins Only` (safest for giveaways, supports anonymous group admins!) / `👥 Humans Only` / `🌐 Everyone (+Bots)`.
    - **Answer Style**: `⚡ Giveaway (Ultra-Short bare answer)` / `💬 Short (1 sentence)` / `📖 Detailed`.
    - **Detection Mode**: `🔥 Giveaway Aggressive` / `⚖️ Balanced` / `🔒 Strict (? required)`.
    - **↩️ Reply to Question**: Replies directly to the question message (`event.reply`).
    - **🧮 Instant Local Math Solver**: Solves arithmetic giveaways (`47 * 83`, `15% of 240`, `sqrt(144)`, `(12 + 8) × 5`) in **<1ms** locally with zero API latency!
    - **🏁 First Comment Race**: When the giveaway is "first comment wins", the reply is a latency contest you cannot lose to an AI round-trip. This mode recognises the signal in **~2 µs** and replies **`I win`** with **no model call at all** — no prompt, no API key, no chance of the model deciding it "isn't a question" and replying nothing. It matches every variation hosts actually type: `first comment`, `1st comment`, `first cmt`, `first com`, `firstcomment`, `comment first`, `fc`, `f.c.`, `f/c`, common typos (`frst`, `firts`), emoji decoration, and Amharic (`ፊርስት ኮመንት`). Prose that merely *mentions* a first comment ("the first comment on yesterday's post was rude") is ignored, and so are lookalikes like `FC Barcelona` and `FC 2-1`. **Off by default** — it posts on its own, so enable it only in a group that asks for it.
    - **📷 Answer Photo Questions**: Answers questions sent as a **picture** — a screenshot of a quiz, a photographed handwritten problem, or an image with a caption. Works even when the photo has **no text at all**. Photos up to **4 MB** are downloaded and passed to a vision model.
    - **💱 Forex Trading Mode**: Turns the group into a Forex specialist. Bare trader shorthand (`EURUSD?`, `GU bias`, `XAU support`, `cable`) is recognised instead of dropped as chatter, and answers use FX conventions — **pips** (0.0001, and 0.01 on JPY pairs), a **standard lot = 100,000 units**, pairs written `EURUSD`/`GBPJPY`/`XAUUSD`, and the real drivers of a currency (rate differentials, central-bank policy, inflation prints, risk sentiment, commodity terms of trade). It **refuses to invent live data**: asked for today's price, a level, a forecast or the news, it answers **`No live data`** rather than reciting a stale price as if it were current.
    - **🎓 Forex Question Coverage (with the Forex mode)**: built for what a Forex giveaway actually asks. **Definitions** (pip, pipette, margin, swap, rollover, spread, slippage, lot sizes, cross/exotic/major pairs, base and quote currency), **technicals** (support/resistance, breakout, retest, pullback, doji, engulfing, pin bar, bull flag, wedge, pennant, golden/death cross, double top/bottom, pivot points, channels, trendlines), **indicators** (RSI, MACD, moving averages, Bollinger, ATR, Ichimoku, divergence, overbought/oversold), **macro** (NFP, CPI, FOMC, hawkish/dovish, QE, tapering, dot plot, basis points, real yields, risk-on/risk-off, carry trade, current account, GDP), **sessions**, and **risk** (trailing stops, position size, margin calls, stop hunts, whipsaws). The prompt reasons like a trader — pair direction is a *relative* story (hawkish ECB vs dovish Fed, both sides named), gold against real yields and the dollar, JPY/CHF as havens, AUD/NZD as risk-on, CAD against oil — rather than reciting trivia.
    - **🧮 Instant Forex Arithmetic (with the Forex mode)**: pip distance, pip value, lot size and risk % are **formulas, computed locally in <1 ms with no AI call** — `how many pips from 1.09850 to 1.09950` → `10`, `pip value on a standard lot` → `$10.00`, `2% of a 5000 account` → `100`, `1% risk with a 25 pip stop on 10000` → `0.40`. Anything needing a live cross-rate (e.g. `20 pips on GBPJPY`) deliberately falls through to the AI rather than guessing.
    - **📷 Chart Photos (with the Forex mode)**: a photographed chart, position screenshot or broker panel is read as the data source. The model is told to take price-scale, candle highs/lows, indicator values, symbol and timeframe **out of the picture** and to answer from them, while the live-data rules stay in force for questions the picture cannot answer. A **decorative poster** is recognised as decoration: the model is told not to read a price off the artwork.
    - **🖼️ Questions Printed On The Image**: a giveaway poster usually carries its question *inside the picture* with no caption. When a photo arrives with no text, the bot transcribes the words on it first, then answers through the same exact paths as typed text — so *"on the BTC chart, what was the highest price this week?"* is **computed from a real OHLC series** (`provider=local_market`, no second AI call) instead of being eyeballed off a chart. Attaching a photo never disables the local shortcuts.
    - **💬 Answers Under The Card**: the way a giveaway actually runs — the host posts a card, members reply *to it* with "What is the answer". Those replies are answered from the card in the thread, not taken literally. "What is the answer", "answer below", "Read the card" and "First to answer" are recognised as **pointers at the picture**, not as questions in their own right.
    - **🩹 A Refusal Is Retried, Not Posted**: if a model handed a photo answers "No live data" while the number is legible in the attachment, it gets one more look with the picture framed as the evidence. A refusal can never be the right answer when the answer is in the image.
    - **🧩 Every Poster Arrangement**: the question is found whether it is a single line, stacked across five lines of artwork, split mid-phrase, numbered among other questions, written as `BTCUSDT`, prefixed with a `$`, wrapped in hashtags, or missing its question mark. Captions are handled too: none at all, a stub like *"answer below"* or *"🔥🔥"*, an `@channel` credit, or a paragraph of promo copy with the question only on the card — each of those is read from the picture rather than answered literally.
    - **🇪🇹 Amharic Posters**: *"በExness BTC ገበታ ላይ ይህ ሳምንት ከፍተኛው ዋጋ ስንት ነው?"* is parsed and computed exactly like the English version — the period words (*ይህ ሳምንት* this week, *ያለፈው ሳምንት* last week, *ዛሬ* today, *ትናንት* yesterday) and the aggregations (*ከፍተኛው* highest, *ዝቅተኛው* lowest) are understood directly, and instruments can be written as `BTC` or *ቢቲሲ*.
    - **📊 Past-Period Market Questions (with the Forex mode)**: "on the Exness BTC chart, what was the highest price last week?" is **computed locally** from real daily OHLC data, not guessed by a model. Highest/lowest/open/close/change over **today, yesterday, this week, last week, this month and last month** work for **BTC, ETH, SOL, XRP, DOGE, BNB, LTC**, plus gold, silver and DXY. The answer is the bare figure (`66055.10`) and the model is never called — a model asked to eyeball a series of bars will eventually pick the wrong one, so the arithmetic happens in code. Crypto is quoted to 2 decimals, not FX's 5.
      - **A week is a calendar week, and it has five trading days.** "This week" is Monday-to-now; "last week" is the previous **Monday to Friday**, not the seven days before today. A trailing seven-day window lands on an arbitrary weekday, so a Thursday question about "last week" returns a high from two different weeks — close enough to look right, wrong often enough to lose a competition.
      - **The weekend is never a trading day.** Exness still serves a Saturday/Sunday bar for FX, metals and indices carrying Friday's close; those bars are dropped, so a five-day question really spans five sessions (and crypto, which does settle at weekends, is exempt).
      - **"The last 5 days" means five sessions.** Explicit day counts are counted in trading days, not calendar days — the weekend is skipped rather than silently eating two of the five.
      - **Daily bars are dated by their trading day.** The session opens at the broker's local midnight, which is 21:00/22:00 UTC the previous evening; that stamp is snapped to the trading date so a Monday bar can never be mistaken for Sunday and filtered out as a weekend.
      - **Today's still-forming candle** bounds "today" and "this week so far" but is excluded from "last week"/"last month", so a half-finished day can't become the weekly high. A window with no session in it (a weekend "this week", a Monday "yesterday") reports the most recent completed session rather than inventing a figure.
    - **📈 Live Market Prices from Exness (with the Forex mode)**: the refusal above stops being the answer once prices are switched on. The bot reads **real prices at answer time from your own Exness trading account** via the Exness Trader API — FX pairs, gold and silver, crypto, with the account's own symbol suffixes (`EURUSDm`, `XAUUSDc`) discovered automatically. There is **no fallback to another vendor**: if Exness is not connected or unreachable, the answer is `No live data` rather than a different venue's number. The model is handed the figures and told they are *the only prices it may state*, so "EURUSD now?" gets a real figure instead of a refusal. **Replies are just the number** — `EURUSD 1.09840` — with no timestamp, no "as of", and no mention of a feed, so an answer reads like a trader typing a price rather than a machine reporting one. When the market is closed the reply says `market closed` instead of presenting Friday's close as live. Costs one extra ~200–400 ms feed call, only in Forex groups that have it enabled, and it is cached for 30 s so a burst of giveaway questions pays for it once.
    - **⏱ Cooldown**, **⏳ Optional Human Delay**, **🏷 Custom Trigger Keywords**, and **🧠 Context Messages**.
- **🤖 Multi-Provider Fast & Free AI + Automatic Fallback**:
  - **⚡ Groq** (`openai/gpt-oss-120b` / `openai/gpt-oss-20b`, ~500–1000 tok/s — ideal primary for speed giveaways). Retired model IDs are detected automatically and swapped for the live replacement, so a stale model never looks like a bad API key.
  - **🧠 Google Gemini** (high accuracy free tier — Flash-Lite / Flash).
  - **🌐 OpenRouter** (`openrouter/free` auto-router + free model catalog).
  - Single-shot classification + answering over persistent HTTP/2 connection pooling + automatic failover on HTTP 429/errors.
- **🎯 Emoji-Proof Questions**: Giveaway hosts decorate heavily — `🎯 47 * 83 ✅🔥🔥🔥` — and emoji are multi-codepoint (flags, keycaps, ZWJ families, skin tones), so naive handling leaves invisible fragments that break parsing. Questions are normalised before anything else looks at them: emoji, invisible bidi marks, and no-break/ideographic spaces are removed while **keycap digits keep their digit** (`2️⃣ + 3️⃣` → `5`). Decorated arithmetic still takes the **<1 ms local solver**, emoji-only messages like `🔥🔥🔥` are correctly ignored instead of being sent to the AI, and answers keep a *natural* flag (`Nairobi 🇰🇪`) while losing decorative prefixes and emoji spam.
- **🔀 Mixed English + Amharic Questions**: these groups code-switch constantly — `FIFA ሲተነተን ምን ማለት ነው`, `explain VAR ማለት ምን ነው`. A mixed question is answered **in Amharic**, with any English name, acronym or technical term kept **exactly as written** (FIFA stays FIFA, never ፊፋ), because a host checking the answer will not recognise a transliteration. The detection threshold is deliberately low (25% Ethiopic letters) so an Amharic-shaped question with an English term inside is not misfiled as English — `explain VAR ማለት ምን ነው` sits at 41% and was previously answered in English. A question meaning "what does X mean" also gets the *meaning* of X, not a restatement. Amharic answers additionally get **extra token headroom** (Ethiopic costs ~1 token per character against ~4 chars for Latin), and a truncated answer is retried rather than posted as a fragment.
- **🇪🇹 Amharic Support**: Questions asked in Amharic (Ethiopic script) are understood. Amharic has no Latin question words and often no `?`, so `ማን ነው የኢትዮጵያ ዋና ከተማ` used to be skipped as chatter — it is now detected (`ማን` who, `ምን` what, `የት` where, `መቼ` when, `ስንት` how many…). Arithmetic wrapped in Amharic (`ስንት ነው 43 + 92`) still takes the **<1ms local solver**, and **Ethiopic numerals** work end to end: `፵፫ + ፶፪ ስንት ነው?` → `፺፭`, answered in the same numerals it was asked in.
- **🗣 Answers in the Most Appropriate Language**: the reply language follows the **answer**, not automatically the question. Ordinary facts and explanations come back in the language you asked in (Amharic question → Amharic answer), but names, acronyms, institutions and terms of art are given in the form they are actually written in: asked in Amharic what FIFA stands for, the answer is **`Fédération Internationale de Football Association`** — not a transliteration (`ፊፋ`) and not a translation. It also never answers a "what does X mean" question by restating the question, which is the classic AI giveaway failure.
- **🚀 Response Speed Control**: A per-account `⚡ Fastest / ⚖️ Balanced / 🧠 Smart` toggle. Reasoning models think *before* they answer, and both Groq's `gpt-oss` and Gemini 3.x default to **medium** reasoning — that hidden step is the usual cause of a laggy giveaway answer. **Fastest** (the default) turns it off or down to the minimum each model accepts, so the answer arrives in one shot. **Smart** hands the model its full reasoning budget for tricky questions.
- **📷 Photo Questions (Vision)**: Screenshots and photos are answered like text questions. If your configured model cannot read images, the bot **automatically switches to a vision-capable model just for that message** and switches back afterwards — Groq `gpt-oss` → `qwen/qwen3.8-27b`, and every Gemini Flash model reads images natively. An uncaptioned photo with a *printed question* is transcribed first so the question can be answered exactly; only a photo with no readable question goes to the model to answer (the arithmetic is *inside* the image).
- **🧪 Live Q&A Simulator & Speed Benchmark**: Test sample giveaway questions and benchmark your providers right inside the dashboard bot.

---

### 📈 Where the prices come from: Exness

Every Forex price the bot posts is read from **your own Exness trading account** through the
**Exness Trader API** — the same venue and the same feed the host scoring the giveaway is looking at.
That matters because a giveaway is graded against the Exness terminal: a tick from another liquidity
pool can sit pips away, and pips *are* the answer.

There is **one source and no fallback**. If Exness is unreachable, the answer says `No live data`
rather than quoting another broker or a central-bank reference rate under a Forex group's nose.

| What | Endpoint | Used for |
|---|---|---|
| Access Point discovery | `GET /v1/trading/access-point` on the Gateway host | resolve the account's host, cached for an hour |
| Account instruments | `GET /v1/configuration/accounts/{id}/instruments` | pick the account's own symbol (`EURUSD`, `EURUSDm`, …) |
| Live price | `GET /v1/market-data/accounts/{id}/candles` (`M1`, `bid`) | the newest tick, per question |
| Period history | same endpoint, `D1` | “highest price last week”, computed locally |

Requests are signed with **Ed25519** (`EXN-API-KEY`, `EXN-TIMESTAMP`, `EXN-DATA`, `EXN-SIGN`, …,
base64url without padding) exactly as Exness documents, and the account's Access Point is discovered
first — Exness has no fixed public quote host.

#### Connecting it (2 minutes, once)

Exness publishes **no anonymous quote feed** — quotes are account-scoped, and the API is for Exness
trading accounts only. So you need four values from your Exness Personal Area:

1. Sign in → **Key Management** → **Create API key**
2. Scope: **Trading — Read-only** (the bot never places an order), and select the account you trade
3. Copy the **API key** (`exnsk-…`) and the **private signing key** shown once at creation
4. Note your **trading account number** and the **Gateway host** from the **DNS widget**

Then in the dashboard bot: **`/dashboard` → 🤖 AI Providers & Models → 📈 Exness Prices**, and paste
each value when asked. Key and private key are deleted from the chat and stored encrypted; the menu
shows a ✅ per field. Finish with **🔌 Test Connection**, which resolves your host and reads a real
price — that is a live round trip, not a claim.

Prefer environment variables? Set `EXNESS_API_KEY`, `EXNESS_PRIVATE_KEY`, `EXNESS_ACCOUNT_ID` and
`EXNESS_GATEWAY_URL` on Render; env wins over anything saved in the dashboard.

**Check it on your own deploy:** send `/diag`. If Forex groups are enabled you get either
`🟢 Live prices: EURUSD 1.09840 (Exness EURUSDm, …UTC)` — a real probe from your server — or a
specific reason (`Exness not connected`, or the error code: `1000` bad key, `1001` signature,
`1002`/`403` account access, `4000` instrument not on that account). Price questions answer
`No live data` whenever that line is red, and `/diag` tells you which of the four values to fix.

> **Freshness without exposure.** FX trades 24/5, so on a weekend the newest candle is Friday's
> close. The bot marks that `market closed` and never presents it as the live price. The full
> timestamp and the exact Exness symbol stay out of group replies on purpose — they are shown on
> your own `/diag` screen, where they help, and nowhere the group can see.

## ☁️ Deploying to Render Free Tier (Step-by-Step)

> **Setting up a second account?** See **[SETUP-GUIDE.md](SETUP-GUIDE.md)** for the full walkthrough — separate bot token, separate `OWNER_ID`, what must not be shared between deployments, and a troubleshooting table.

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
     - Paste the key **on its own**. Trailing newlines, surrounding quotes, a `Bearer ` prefix and invisible characters copied from a browser are all stripped automatically, and a key with the wrong prefix (e.g. a Groq `gsk_…` key pasted into OpenRouter, which expects `sk-or-…`) is caught **before** any request is sent.
     - If a test fails, the message says *why*: a 401 saying **"Missing Authentication header"**, a **429** rate limit and a genuinely rejected key are three different problems, and only one of them means "get a new key".
     - **You can always leave the step.** `/cancel`, `/start`, `/setup`, `/dashboard` and every other command work from any step — a stuck prompt can never lock you out of the bot.
   - **Step 4**: Toggle `✅` on the giveaway groups you want to monitor, tap `⚙️` on any group to customize rules, and tap **✅ Finish Setup & Open Dashboard**.

### Step 4 — Make Your Setup Survive Render Free Restarts (Important!)

Because Render Free Tier wipes local container disk whenever the service restarts or redeploys:

1. In your Dashboard Bot, tap **⚙️ Account & Setup** → **☁️ Backup This Deployment (`BOT_STATE_B64`)**.
2. Copy the generated code string.
3. In Render → your service → **Environment** → add:
   - Key: `BOT_STATE_B64`
   - Value: *(paste the exported string)*
4. Click **Save Changes** (Render redeploys and automatically restores your login, AI keys, and selected groups!).
5. Tap **🗑 Delete Secret Message** in Telegram to keep your chat clean.

*(Whenever you later enable new groups or add a fallback AI key, export `BOT_STATE_B64` again and update it in Render so changes persist across restarts.)*

> **⚠️ Never send this backup to anyone.** It contains your Telegram **session string** (a complete account login) and your **AI API keys**. Use the Share option below instead.

### Step 4b — Letting Someone Else Run Their Own Copy

The code is fully per-deployment: every credential lives in that deployment's own Render environment or its own database. Nothing is baked into the repo, so anyone can run their own copy from the same source.

1. Tap **⚙️ Account & Setup** → **🎁 Share Setup With Someone (no credentials)**.
2. Send them the generated string — it contains your **groups and their settings only**. `tg_session`, `api_key_*`, `owner_id` and `tg_api_id` are excluded by design.
3. They fork this repo, create their own Render service, and set **their own** `BOT_TOKEN` and `OWNER_ID` (from [@userinfobot](https://t.me/userinfobot)), then paste the string into `BOT_STATE_B64`.
4. They deploy and log in **their own** Telegram account, and add **their own** AI key in the dashboard.

**Why the credentials do not transfer:** they must not. A Telethon session string *is* an account — importing one would post from the wrong account and bill the wrong person. The bot enforces this: importing a snapshot into a deployment that already has an owner silently refuses every credential and logs a warning, and `OWNER_ID` / `GROQ_API_KEY` / `TG_API_HASH` set in Render always override whatever a pasted snapshot contains.

**What does not travel with a share snapshot:** your Telegram login, your AI keys, your owner id, and any per-deployment secrets. Only group configuration (which groups, detection mode, answer style, cooldowns, keywords, toggles) moves across.

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
| **🏁 First Comment Race** | `OFF` *(on only when a host says "first comment wins")* | Replies `I win` in ~2 µs, zero AI latency — posts on its own, so leaving it on everywhere is noise |
| **Reply to Question** | `ON` | Replies directly to the host's question message |
| **📷 Answer Photo Questions** | `ON` | Answers screenshot/photo quizzes — turn **off** to save paid vision credits |
| **💱 Forex Trading Mode** | `ON` for FX groups | Accepts pair shorthand and uses pips/lot conventions; never fabricates a live price |
| **📈 Live Market Prices** | `ON` for FX groups | Real quotes for pairs, gold, DXY, US10Y; turn off for zero feed latency |
| **🚀 Response Speed** | `⚡ Fastest` | Turns off the model's hidden reasoning step — the #1 cause of slow answers |
| **🇪🇹 Amharic Groups** | `🔥 Aggressive` | Amharic questions often have no `?`, so aggressive mode catches them |

---

## 🧪 Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```
