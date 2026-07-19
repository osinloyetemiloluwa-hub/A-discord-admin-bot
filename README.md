---
title: TECO - AI Co-Admin Discord Bot
emoji: 🤖
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# TECO - Advanced AI Co-Admin Bot

An advanced Discord server-management bot combining traditional moderation tools with
an AI agent powered by **Groq AI** (via free Groq API - llama-3.3-70b, mixtral-8x7b).
Built 100% in Python with `discord.py`, source-controlled on **GitHub**, and hosted on a
**Hugging Face Docker Space**.

> Note on the YAML block above: it's required so Hugging Face knows how to build this
> repo as a Space (Docker SDK, port 7860). GitHub just renders it as plain text at the
> top of this file — harmless, and lets one repo serve both purposes.

## How the three pieces fit together

```
GitHub repo  ──push──▶  GitHub Actions  ──force-push──▶  HF Space (Docker, rebuilds)
                                                              │
                                                              ▼
                                                     Discord bot process
                                                     + tiny keep-alive web server
                                                              │
                                                              ▼
                                                  Groq AI API (the "brain")
                                                  llama-3.3-70b / mixtral-8x7b
```

- **GitHub** is where you write and version your code.
- A **GitHub Action** (included) force-pushes your `main` branch to your Hugging Face
  Space's own git repo on every commit, so the Space always rebuilds from GitHub —
  you never have to push to HF manually.
- The **HF Docker Space** builds the `Dockerfile` and runs `main.py`, which starts the
  Discord bot *and* a tiny `aiohttp` web server on port 7860 side-by-side. HF Spaces
  require something bound to a port to mark the Space "Running" — a bot with no HTTP
  server gets stuck in "Starting" forever, so this is required, not optional.
- **Groq AI** provides the API key that powers every AI command (`/teco`, `/ask`,
  `@mention` chat, `/toxcheck`, `/summarize`, `/moderate`, `/audit`).

## Features

**Advanced AI Commands (TECO):**
- `/teco` - Natural language command processing (e.g., "delete last 5 messages with nsfw", "show me recent messages")
- `/audit` - AI-powered server audit with health scores and recommendations
- `/scan` - Scan members for issues (new accounts, no avatar, incomplete profiles)
- `/toxicityscan` - AI scan entire channels for toxic content
- `/purgecontent` - Delete messages by keyword or AI-detected violations
- `/search` - AI-assisted message search

**Moderation:** kick, ban, tempban, unban, mute/unmute (timeout), warn system with
auto-escalation (3 warns = mute, 5 = kick, 7+ = ban), purge, slowmode, channel
lock/unlock, role management, nicknames, user/server info, moderator notes.

**AI Agent (Groq):** `/ask` for direct AI Q&A with rolling memory, `@mention`
conversational chat, `/toxcheck` AI-based toxicity classification, `/summarize`
channel activity summarization, `/moderate` AI-assisted rule-violation review.

**Auto-Mod:** anti-spam (message rate + mention spam), link filtering, excessive caps
filtering, custom banned word list, anti-raid join-rate detection with auto verification
lockdown, new-account flagging.

**Logging:** message edit/delete, member join/leave, role/nickname changes, channel
create/delete, voice channel activity — all to a configurable log channel.

**Welcome System:** customizable welcome/farewell messages with placeholders
(`{mention}`, `{name}`, `{server}`, `{count}`), auto-role assignment on join.

## Project Structure

```
discord-co-admin-bot/
├── main.py                       # Entry point: starts bot + keep-alive server together
├── Dockerfile                    # HF Docker Space build
├── requirements.txt
├── runtime.txt
├── .env.example
├── .gitignore
├── .github/workflows/sync-to-hf.yml  # Auto-push GitHub -> HF Space
├── cogs/
│   ├── moderation.py              # Kick/ban/mute/warn/purge/lock/role/info
│   ├── ai_agent.py                 # Groq-powered AI commands (TECO)
│   ├── automod.py                  # Spam/link/caps/badword/raid protection
│   ├── logging_cog.py              # Event logging
│   └── welcome.py                   # Welcome/farewell/autorole
├── utils/
│   ├── database.py                 # PostgreSQL/asyncpg layer
│   ├── groq_client.py              # Groq AI API wrapper
│   └── keepalive.py                # aiohttp server so HF Space shows "Running"
└── data/                          # Database storage (gitignored)
```

## Setup

### 1. Discord Bot
1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. Bot tab → Reset Token → copy it for `DISCORD_TOKEN`
3. Enable **all three Privileged Gateway Intents** (Presence, Server Members, Message Content)
4. OAuth2 → URL Generator → scopes: `bot`, `applications.commands` → permissions: `Administrator`
   (or curated: Kick/Ban Members, Manage Roles, Manage Channels, Manage Messages, Moderate Members, View Audit Log)
5. Use the generated URL to invite the bot to your server

### 2. Groq AI (FREE)
1. Go to [console.groq.com/keys](https://console.groq.com/keys)
2. Create a free API key → this is your `GROQ_API_KEY`
3. Groq offers free tier with llama-3.3-70b-versatile (fast inference), mixtral-8x7b-32768, and more
4. Default model: `llama-3.3-70b-versatile` - fast and capable

### 3. GitHub Repo
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/your-username/your-repo.git
git push -u origin main
```

### 4. Hugging Face Docker Space
1. [huggingface.co/new-space](https://huggingface.co/new-space) → SDK: **Docker** → blank template
2. Note the Space URL, e.g. `https://huggingface.co/spaces/your-username/your-space-name`
3. Space → **Settings** → **Variables and secrets** → add as **secrets**:
   - `DISCORD_TOKEN`
   - `GROQ_API_KEY`
   - `OWNER_IDS`
   - `DATABASE_URL` (optional, for PostgreSQL)
4. Don't manually upload files yet — step 5 wires up auto-deploy from GitHub instead.

### 5. Connect GitHub → HF Space (auto-deploy)
1. In your **HF account** → Settings → Access Tokens → create a token with **Write** access
2. In your **GitHub repo** → Settings → Secrets and variables → Actions → New repository
   secret named `HF_TOKEN` → paste the HF write token
3. Edit `.github/workflows/sync-to-hf.yml` and set `HF_SPACE` to `your-username/your-space-name`
4. Commit and push to `main` — the Action force-pushes your code to the Space, which
   then builds the `Dockerfile` and starts the bot. Check the Space's "Logs" tab to
   confirm it reaches `✅ Logged in as ...`.

### 6. Local Development (optional, recommended before deploying)
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in your tokens
python main.py
```

## TECO - Natural Language Commands

TECO is the advanced AI command processor that understands natural language:

```
/teco what are the last messages on my discord server
/teco delete last 10 messages containing nsfw
/teco show me messages from user123
/teco who are the new members today
/teco summarize what we've been discussing
/teco audit this server for issues
```

## Important Operational Notes

**Free Space sleep behavior:** a free `cpu-basic` HF Space automatically pauses after
48 hours of inactivity, and you can't configure a custom sleep time on that tier. To
keep a Discord bot online 24/7, either:
- Upgrade the Space to a paid persistent hardware tier (never sleeps), **or**
- Set up a free external pinger (e.g. [UptimeRobot](https://uptimerobot.com) or a
  scheduled GitHub Action) to hit `https://your-space-url.hf.space/health` every
  20–30 minutes — this counts as activity and prevents the 48h sleep timer.

**Database persistence:** the Space's filesystem resets on every rebuild. For persistent
warnings/config across redeploys, either attach a [Hugging Face persistent storage](https://huggingface.co/docs/hub/spaces-storage)
volume to the Space, or use the provided PostgreSQL configuration with Neon.

**Groq model selection:** `.env` defaults to `GROQ_MODEL=llama-3.3-70b-versatile`, which
offers the best balance of speed and capability. You can also use `mixtral-8x7b-32768`
for longer context windows.

## Commands Quick Reference

All commands are slash commands (`/`). Run `/help` in Discord at any time for the full
in-app list grouped by category.
