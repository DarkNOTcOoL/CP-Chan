# Codeforces Discord CP Bot 🚀

A full-featured Discord bot built with `discord.py` v2.0+ and SQLite that brings Codeforces competitive programming tracking, automatic problem announcements, leaderboards, and a dynamic rank role system directly into your Discord server (#coding channel).

---

## ✨ Features

- **Slash Commands**:
  - `/solving <problem_id>` — Announce that you are solving a Codeforces problem (e.g. `/solving 213A`, `/solving 1900B`) with problem details, rating, and tags.
  - `/link <cf_handle>` — Link your Discord account to your Codeforces handle. Automatically backfills your past accepted submissions to jump-start your points and assigns your rank role.
  - `/leaderboard` — Display the server's top coders sorted by total points with rank badges, solve counts, and Codeforces handles.
  - `/stats [user]` — View detailed user statistics, live Codeforces rating/rank, tier progress bar, and problem rating distribution breakdown.

- **Automated Tracking & Auto-Announcements**:
  - Background polling task runs every 3 minutes.
  - Detects new accepted (`verdict == "OK"`) Codeforces submissions.
  - Awards custom weighted points according to problem difficulty.
  - Sends congratulatory embeds directly to the `#coding` channel.

- **Dynamic Discord Rank Roles**:
  - Automatically creates and manages tiered roles on the server:
    - `S1`: 100+ points
    - `S2`: 500+ points
    - `S3`: 1,000+ points
    - `C1`: 2,000+ points
    - `C2`: 5,000+ points
    - `C3`: 10,000+ points
  - Automatically promotes users, swaps roles, and announces promotions in chat.

---

## 🛠️ Project Structure

```
dccpbot/
├── cogs/
│   ├── leaderboard.py    # /leaderboard command
│   ├── link.py           # /link command + history backfill
│   ├── solving.py        # /solving command
│   ├── stats.py          # /stats command + progress bar
│   └── tracker.py        # Background CF submission tracker & announcements
├── cf_api.py             # Asynchronous Codeforces API client (aiohttp)
├── db.py                 # Async SQLite database layer (aiosqlite)
├── points.py             # Custom point weighting rules
├── roles.py              # Rank tier definitions & Discord role management
├── main.py               # Bot entry point & lifecycle management
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variables template
└── .gitignore
```

---

## 🚀 Quick Start (Local Setup)

### 1. Clone the Repository
```bash
git clone https://github.com/<your-username>/discord-cp-bot.git
cd discord-cp-bot
```

### 2. Create Virtual Environment & Install Dependencies
```bash
python -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your credentials:
```ini
DISCORD_BOT_TOKEN=your_bot_token_here
CODING_CHANNEL_ID=your_channel_id_here
```

### 4. Run the Bot
```bash
python main.py
```

---

## 🛡️ Discord Bot Setup & Permissions

1. Visit [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. In **OAuth2 → URL Generator**, select:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**:
     - `Send Messages`
     - `Embed Links`
     - `Use Application Commands`
     - `Read Message History`
     - `Manage Roles` *(Required for rank assignments)*
3. **Important**: In your Discord server's **Server Settings → Roles**, drag the **Bot's own role above all rank roles** (`S1`, `S2`, `S3`, `C1`, `C2`, `C3`) so it has permission to assign and remove them.

---

## 📜 License
This project is open source and available under the [MIT License](LICENSE).

