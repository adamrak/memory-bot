# Discord Memory Bot

Personal assistant on Discord that answers from (and saves to) markdown notes in a GitHub repo, using Gemini.

## Usage
- Write a message in the bot channel → the bot opens a **thread** (one thread = one conversation) and answers from your notes, citing files.
- Inside a thread, follow-up questions keep context.
- `zapíš si sumár z konverzácie` / `zapíš si to do sekcie auto/stk` / `save: …` → the bot shows a preview with
  **✅ Save / 📝 Change path / ❌ Cancel**. Nothing is committed until you click Save. New text is appended to the file.
- `!sync` → pull the latest notes from GitHub now (it also syncs automatically every 5 minutes).

## Setup
### Discord
1. Developer Portal → Bot → enable **Message Content Intent**.
2. Invite with permissions: View Channels, Send Messages, Read Message History,
   Create Public Threads, Send Messages in Threads.
3. Enable Developer Mode in Discord, right-click yourself → Copy User ID → `OWNER_ID`.

### GitHub
Fine-grained personal access token, **only the notes repo**, permission **Contents: Read and write**.

### Render (free Web Service)
- Build: `pip install -r requirements.txt`
- Start: `python main.py`
- The bot serves `GET /health` on `$PORT`. Free web services sleep after 15 min without HTTP traffic,
  so create an **UptimeRobot** (or cron-job.org) HTTP monitor on `https://<app>.onrender.com/health`, every 5 min.

### Environment variables
| Name | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token |
| `OWNER_ID` | ✅ | Your Discord user ID. The bot ignores everyone else |
| `GITHUB_TOKEN` | ✅ | Fine-grained PAT |
| `GITHUB_REPO` | ✅ | `owner/repo` |
| `GEMINI_API_KEY` | ✅ | From Google AI Studio |
| `GEMINI_MODEL` | | Default `gemini-3.8-flash` |
| `GITHUB_BRANCH` | | Default: the repo's default branch |
| `CHANNEL_ID` | | Restrict the bot to one channel (+ its threads) |
| `SYNC_MINUTES` | | Default `5` |
| `HISTORY_LIMIT` / `HISTORY_MAX_CHARS` | | Thread context cap, default `50` / `40000` |

> ⚠️ On the Gemini **free tier**, Google may use prompts for training. Your whole notes repo is sent with each question.
