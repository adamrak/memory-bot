# Discord Memory Bot

Personal assistant on Discord that answers from (and saves to) markdown notes in a GitHub repo, using Gemini.

## Usage
- Write a message in the bot channel → the bot opens a **thread** (one thread = one conversation) and answers from your notes, citing files.
- Inside a thread, follow-up questions keep context. Photos you send are shown to Gemini; PDFs only when you ask (`prečítaj`, `analyzuj`, `read`, …).
- `zapíš si sumár z konverzácie` / `zapíš si to do sekcie auto/stk` / `save: …` → preview with
  **✅ Save / 📝 Change path / ❌ Cancel**. Nothing is committed until you click Save. New text is appended to the note.
  Photos/PDFs from the thread can be saved too (pick them in the preview); they are stored **next to the note**.
- `oprav …` / `zmaž …` / `presuň …` / `edit` / `delete` → Gemini proposes changes, you get a diff with **✅ Apply / ❌ Cancel**.
  One commit per change. Attachments follow their note: when a note moves, its files move next to it
  (unless another note links them too); attachments no longer linked from any note are deleted.
- `vráť` / `undo` / `revert` (or `!undo`) → preview of the last commit with **↩️ Revert / ❌ Cancel**.
  Reverting creates a new commit restoring every touched file (notes and attachments) to its previous version.
  Running it again right after undoes the revert.
- Ask for a stored file ("ukáž mi návod k akaso") → the bot sends it (or a GitHub link if > 10 MB).
- `!sync` → pull the latest notes from GitHub now (it also syncs automatically every 5 minutes).
- `!ping` → test Gemini latency of each configured model.

## Repo layout
Notes live under `memory/` (see `MEMORY_DIR`), nested as deep as useful; Gemini picks the folders:
```
memory/
  auto/stk.md
  elektronika/akaso-brave/
    akaso-brave.md
    2026-10-30-navod.pdf
```

## Setup
### Discord
1. Developer Portal → Bot → enable **Message Content Intent**.
2. Invite with permissions: View Channels, Send Messages, Read Message History, Attach Files,
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
| `OWNER_ID` | ✅ | Your numeric Discord user ID. The bot ignores everyone else |
| `GITHUB_TOKEN` | ✅ | Fine-grained PAT |
| `GITHUB_REPO` | ✅ | `owner/repo` |
| `GEMINI_API_KEY` | ✅ | From Google AI Studio |
| `GEMINI_MODEL` | | Comma-separated fallback list, default `gemini-3.5-flash` |
| `GEMINI_THINKING` | | `low` (default) / `high` / empty = model default |
| `GEMINI_TIMEOUT_S` / `GEMINI_MEDIA_TIMEOUT_S` | | Default `45` / `120` (requests with images/PDFs) |
| `GITHUB_BRANCH` | | Default: the repo's default branch |
| `MEMORY_DIR` | | Folder with notes, default `memory` |
| `CHANNEL_ID` | | Restrict the bot to one channel (+ its threads) |
| `SYNC_MINUTES` | | Default `5` |
| `HISTORY_LIMIT` / `HISTORY_MAX_CHARS` | | Thread context cap, default `50` / `40000` |
| `MAX_MEDIA` | | Newest N thread images/PDFs sent to Gemini, default `5` |
| `MAX_ATTACHMENT_MB` | | Max size of a saved attachment, default `25` |

> ⚠️ On the Gemini **free tier**, Google may use prompts for training. Your whole notes repo is sent with each question.
