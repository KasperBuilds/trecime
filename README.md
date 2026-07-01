# 🎫 Ticket Monitors

Cloud-based ticket availability watchers that run 24/7 on Railway and alert you via Telegram the instant tickets become available.

## Monitors

### 🏔️ Auronzo / Tre Cime di Lavaredo Parking
Watches [pass.auronzo.info](https://pass.auronzo.info) for parking permit availability at the Tre Cime di Lavaredo trailhead.

- Polls every **15 minutes**
- Alerts when a sold-out time slot becomes available or new dates open up
- Tracks per-slot state so you only get notified on actual changes

### ⚽ Real Madrid Tickets
Watches [realmadrid.com/es-ES/entradas](https://www.realmadrid.com/es-ES/entradas) for match ticket availability.

- Polls every **~2.5 minutes** (with random jitter)
- Alerts when a match flips from unavailable to on-sale
- Configurable watch list to filter by opponent name or date
- Sends a 💓 heartbeat summary to Telegram every 6 hours

## Architecture

Both monitors share a single **[Camofox browser server](https://github.com/jo-inc/camofox-browser)** — a headless Firefox with C++-level anti-detection that bypasses Cloudflare and Akamai bot protection. The Python scripts talk to it via REST API.

```
┌─────────────────────────────────────────────┐
│  Railway Container                          │
│                                             │
│  ┌───────────────────┐                      │
│  │ Camofox Browser   │◄── Akamai/Cloudflare │
│  │ Server (:PORT)    │    cookies set via    │
│  └──┬────────────┬───┘    real browser JS    │
│     │            │                           │
│  ┌──▼──────┐  ┌──▼──────────┐               │
│  │ Auronzo │  │ Real Madrid │               │
│  │ Monitor │  │ Monitor     │               │
│  └──┬──────┘  └──┬──────────┘               │
│     │            │                           │
│     └────┬───────┘                           │
│          ▼                                   │
│   Telegram Bot API                           │
└─────────────────────────────────────────────┘
```

## Setup

### 1. Prerequisites
- A [Telegram bot](https://core.telegram.org/bots#how-do-i-create-a-bot) — get the token from @BotFather
- Your Telegram chat ID — message @userinfobot to get it

### 2. Deploy to Railway

1. Fork this repo or connect it to [Railway](https://railway.app)
2. Add these environment variables in the Railway dashboard:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `CAMOFOX_STORAGE_STATE` | Auronzo session cookies (see below) |

3. Railway auto-deploys on every push to `main`

### 3. Auronzo Login (Cookie Refresh)

The Auronzo monitor requires a logged-in session. Cookies expire every **30 days**.

```bash
# Install Playwright
pip install playwright && playwright install firefox

# Run the login helper — a browser window opens, log in manually
python3 login.py

# Copy the contents of new-storage-state.json into
# the CAMOFOX_STORAGE_STATE variable in Railway
```

### 4. Real Madrid Configuration (Optional)

Edit the `WATCH_LIST` at the top of `rm_monitor.py` to filter specific matches:

```python
# Watch all matches (default)
WATCH_LIST = []

# Watch only El Clásico
WATCH_LIST = [{"opponent": "Barcelona"}]

# Watch all September matches
WATCH_LIST = [{"date": "2026-09"}]

# Watch multiple
WATCH_LIST = [
    {"opponent": "Barcelona"},
    {"opponent": "Atlético"},
    {"date": "2026-12-25"},
]
```

## Files

| File | Purpose |
|---|---|
| `auronzo_monitor.py` | Tre Cime parking monitor |
| `rm_monitor.py` | Real Madrid ticket monitor |
| `login.py` | Helper to refresh Auronzo session cookies |
| `start.sh` | Entrypoint — starts Camofox + both monitors |
| `Dockerfile` | Container config for Railway |
| `camofox-browser/` | Anti-detection browser server |

## Running Locally

```bash
# Terminal 1 — start the browser server
cd camofox-browser && npm install && npm start

# Terminal 2 — run a monitor
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
python3 rm_monitor.py
```

## License

Personal use. Not affiliated with Real Madrid CF or Comune di Auronzo di Cadore.
