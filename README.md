# daily-chess

A Slack bot that posts one easy, medium, and hard Lichess puzzle as inline board images every day, then mentions subscribers in the post's thread: "It's time for daily chess!"

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) on Linux or macOS and one long-lived process. The bot uses Python 3.11+.

1. In [Slack app settings](https://api.slack.com/apps), create an app **From a manifest** using [assets/slack-manifest.json](assets/slack-manifest.json). It enables [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/) and needs no public request URL.
2. Install the app to your workspace. Copy the **Bot User OAuth Token** (`xoxb-...`) to `SLACK_BOT_TOKEN` in `.env`.
3. Under **Basic Information → App-Level Tokens**, generate a token with [`connections:write`](https://docs.slack.dev/reference/scopes/connections.write/). Set `SLACK_APP_TOKEN` to this `xapp-...` token.
4. Invite `daily-chess` to the target channel. Set `SLACK_CHANNEL_ID` to its channel ID, available in the channel details.
5. Copy `.env.example` to `.env` if none exists; otherwise merge its missing entries into your existing `.env`. Set the daily time and timezone there. An incoming webhook (`SLACK_HOOK_URL`) alone cannot support commands and is unused.

Start the bot from the repository root; the launcher uses uv to create the environment and install locked dependencies automatically:

```sh
./scripts/start.sh
```

Keep the process running under your service manager, with this repository as its working directory. Restart after changing `.env` or the message template.

For an existing installation, add the manifest's `users:read` bot scope and reinstall the app to enable nickname lookup for ranking summaries. If lookup fails, the saved command username is used.

Carousel previews require `files:write`. Add this bot scope under **OAuth & Permissions** and reinstall the Slack app before restarting the bot; update `SLACK_BOT_TOKEN` in `.env` if Slack issues a new token. Source changes take effect on restart.

## Commands

Use these in the configured channel. Submissions and command status replies, including duplicate notices, are visible only to the person using the command. Puzzle posts and subscriber threads are visible to the channel.

```text
/daily-chess submit https://lichess.org/training/00008
/daily-chess submit 00008
/daily-chess answer 00008 Rxe7
/daily-chess post
/daily-chess subscribe
/daily-chess unsubscribe
/daily-chess help
```

`submit` validates the puzzle through Lichess and chooses its bin by rating: up to `EASY_MAX_RATING` is easy, then up to `MEDIUM_MAX_RATING` is medium, and higher ratings are hard. Subscription changes persist across restarts.

`answer` checks only your first move in algebraic notation (for example `Nf3`, `Qxe5+`, `O-O`, or `e8=Q`). Each puzzle card also has a **Submit answer** button that opens an input form; results are sent privately in the channel. Correct moves earn a daily place, separately for easy, medium, and hard, across scheduled and manual posts. Each user earns one place per difficulty per day. Only places #1–#3 are announced, mentioning the solver, difficulty, and place in the original puzzle post's thread, with **Also send to channel** enabled. Later solvers still receive private confirmation. Puzzles must have been posted today; dates follow `TIMEZONE`. Rankings survive restarts, and failed announcements retry automatically.

Scheduled puzzles also keep their own first three solvers per difficulty. The next day's scheduled post includes these rankings as `🥇 nickname · 🥈 nickname · 🥉 nickname`, without mentions. Manual puzzles are excluded from this summary. Only the previous calendar day's scheduled post is summarized; an unsolved difficulty displays `No solvers`.

`post` publishes an independent set of three puzzles and mentions subscribers each time, before or after the scheduled post. It uses the same queues and duplicate history without checking or marking the scheduled day's completion. If manual delivery fails, run `post` again to resume the oldest unfinished manual post, including after a restart, before starting another set.

## Configuration and behavior

All options live in `.env`; defaults are in [.env.example](.env.example).

| Option | Meaning |
| --- | --- |
| `POST_TIME` | Daily local time, `HH:MM` (default `09:00`) |
| `TIMEZONE` | IANA timezone (default `Asia/Seoul`) |
| `HISTORY_DAYS` | Days before a posted puzzle can be reused (default `365`) |
| `EASY_MAX_RATING` | Inclusive upper rating for easy submissions (default `1400`) |
| `MEDIUM_MAX_RATING` | Inclusive upper rating for medium submissions (default `1800`) |
| `DATABASE_PATH` | Persistent SQLite file |
| `TEMPLATE_PATH` | Daily message template |
| `FETCH_ATTEMPTS` | Maximum random puzzle attempts per empty bin |

Rating limits must be integers with `0 <= EASY_MAX_RATING < MEDIUM_MAX_RATING`.

The three bins are persistent FIFO queues. At posting time, empty bins receive a random Lichess puzzle. Random selection uses Lichess's anonymous difficulty bands (`easier`, `normal`, `harder`), not strict rating cutoffs. Duplicate IDs are rejected across all bins and retained history; queued puzzles stay protected even after the retention period.

Boards are static Lichess puzzle positions displayed side by side in a horizontally scrollable Slack carousel, with difficulty and rating titles. The bot pads each board locally to 4:3, preserving all eight ranks, then uploads the PNG to Slack. Uploaded file IDs are saved for retries. Scroll sideways in narrow views, or click **View full board** to open the original static image in a Slack popup. Beneath the boards, each difficulty shows the side to move. Images do not animate solutions.

Edit [assets/daily_message.txt](assets/daily_message.txt) to customize the introduction (maximum 3,000 rendered characters). Supported Python format fields are `{date}`, `{easy_rating}`, `{medium_rating}`, and `{hard_rating}`. Use `{{` and `}}` for literal braces.

The scheduler checks every 30 seconds and posts once per local day, catching up today's post after a late restart. It retries unfinished scheduled posts only and does not backfill other missed days. Manual and scheduled delivery recover independently, though Slack rate limits are shared. SQLite records progress for restart recovery. Delivery is best effort: a timeout immediately after Slack accepts a message can cause a duplicate on retry. Run only one instance and keep the database on persistent storage.

Source is in `src/`, runtime data in `data/`, templates and the Slack manifest in `assets/`, and checks in `tests/`.

```sh
uv run --locked python -m unittest discover -s tests
```
