# daily-chess

A Slack bot that posts three Lichess puzzles—easy, medium, and hard—each day. Solve puzzles in Slack, contribute puzzles to the queue, and subscribe to reminders in each post's thread.

## Setup

Requires Linux or macOS and [uv](https://docs.astral.sh/uv/getting-started/installation/). The bot uses Python 3.11+; the launcher installs locked dependencies automatically.

1. From the repository root, create your configuration:

   ```sh
   cp .env.example .env
   ```

2. In [Slack app settings](https://api.slack.com/apps), create an app **From a manifest** using [assets/slack-manifest.json](assets/slack-manifest.json). The manifest includes the required bot permissions and enables Socket Mode, so no public request URL is needed.
3. Install the app to your workspace. Under **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN` in `.env`.
4. Under **Basic Information → App-Level Tokens**, generate a token with `connections:write`. Copy this `xapp-...` token into `SLACK_APP_TOKEN`.
5. Invite `daily-chess` to the destination channel. Copy its channel ID from the channel details into `SLACK_CHANNEL_ID`.
6. Set `POST_TIME` and `TIMEZONE` in `.env`, then start the bot:

   ```sh
   ./scripts/start.sh
   ```

Keep one instance running under your service manager. Preserve the SQLite database at `DATABASE_PATH` across restarts; it stores queues, subscriptions, rankings, and delivery progress. Restart after changing `.env` or the message template.

## Using the bot

Each post shows three static boards in a horizontally scrollable carousel, with ratings and the side to move. Use **View full board** to enlarge a board or **Submit answer** to enter a move.

Use these commands in the configured channel. Command replies and answer feedback are visible only to you.

| Command | Action |
| --- | --- |
| `/daily-chess submit 00008` | Queue a puzzle. A Lichess training URL also works. |
| `/daily-chess answer <id> <move>` | Submit the first move for a puzzle posted today. |
| `/daily-chess post` | Publish an extra set of three puzzles and notify subscribers. |
| `/daily-chess subscribe` | Get mentioned in each puzzle post's thread. |
| `/daily-chess unsubscribe` | Stop those mentions. |
| `/daily-chess help` | Show command help. |

Answers use algebraic notation, such as `Nf3`, `Rxe7`, `O-O`, or `e8=Q`. Only the first move is checked; you can retry an incorrect answer. The puzzle ID appears on its card, and "today" follows `TIMEZONE`.

Each user earns one daily place per difficulty across scheduled and manual posts. The first three places are announced with solver mentions in the puzzle thread and broadcast to the channel. Later solvers receive private confirmation.

Scheduled puzzles also have separate rankings: the next daily post lists the previous calendar day's first three solvers per difficulty by nickname, without mentions. This recap excludes manual puzzles and shows `No solvers` for unsolved difficulties.

## Puzzle selection and scheduling

Submitted puzzles are checked through Lichess and queued by rating: easy up to `EASY_MAX_RATING`, medium above that through `MEDIUM_MAX_RATING`, and hard above both. Each difficulty uses its oldest queued puzzle first. Empty queues are filled with random Lichess puzzles using its `easier`, `normal`, and `harder` bands, which can fall outside the submission rating limits.

Puzzles already queued or posted within `HISTORY_DAYS` cannot be added again. Scheduled and manual posts share these queues and duplicate checks.

The scheduler checks every 30 seconds and posts once per local day. After downtime, it catches up today's post and retries unfinished scheduled deliveries; it does not create posts for other missed days. Failed ranking announcements also retry automatically.

`/daily-chess post` adds an extra set without affecting the daily schedule. If it fails, run the command again to resume the oldest unfinished manual post, even after a restart. A timeout after Slack accepts a message can cause a duplicate on retry.

## Configuration

Set options in `.env`. [.env.example](.env.example) includes the required Slack values and these defaults:

| Option | Default | Purpose |
| --- | --- | --- |
| `POST_TIME` | `09:00` | Daily local time in 24-hour `HH:MM` format. |
| `TIMEZONE` | `Asia/Seoul` | IANA timezone for scheduling and rankings. |
| `HISTORY_DAYS` | `365` | Days before a posted puzzle can be reused. |
| `EASY_MAX_RATING` | `1400` | Inclusive upper rating for easy submissions. |
| `MEDIUM_MAX_RATING` | `1800` | Inclusive upper rating for medium submissions. |
| `DATABASE_PATH` | `data/daily-chess.sqlite3` | Persistent SQLite database. |
| `TEMPLATE_PATH` | `assets/daily_message.txt` | Post introduction template. |
| `FETCH_ATTEMPTS` | `20` | Maximum random puzzle attempts per empty queue. |

Rating limits must be integers with `0 <= EASY_MAX_RATING < MEDIUM_MAX_RATING`. `HISTORY_DAYS` and `FETCH_ATTEMPTS` must be positive integers.

Edit [assets/daily_message.txt](assets/daily_message.txt) to customize the introduction. It supports `{date}`, `{easy_rating}`, `{medium_rating}`, and `{hard_rating}`; use `{{` and `}}` for literal braces. The rendered message must contain 1–3,000 characters.

## Development

Source lives in `src/`, runtime data in `data/`, templates and the Slack manifest in `assets/`, launch scripts in `scripts/`, and tests in `tests/`.

Run the tests from the repository root:

```sh
uv run --locked python -m unittest discover -s tests
```
