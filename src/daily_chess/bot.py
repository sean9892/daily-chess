import fcntl
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .lichess import Lichess, LichessError, Puzzle, puzzle_id
from .store import DIFFICULTIES, Store

HELP = ("`/daily-chess submit <Lichess puzzle URL or ID>` — privately add a puzzle; difficulty follows its rating.\n"
        "`/daily-chess answer <puzzle ID> <move>` — privately submit the first move in algebraic notation.\n"
        "`/daily-chess post` — post an extra puzzle set now.\n"
        "`/daily-chess subscribe` — get daily thread mentions.\n"
        "`/daily-chess unsubscribe` — stop mentions.")


@dataclass(frozen=True)
class Settings:
    channel: str
    post_time: time
    timezone: ZoneInfo
    history_days: int
    database: Path
    template: str
    fetch_attempts: int
    easy_max_rating: int
    medium_max_rating: int

    @classmethod
    def load(cls):
        load_dotenv(Path.cwd() / ".env")
        channel = os.environ.get("SLACK_CHANNEL_ID", "")
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel):
            raise ValueError("Set SLACK_CHANNEL_ID to the destination channel ID.")
        post_time = os.environ.get("POST_TIME", "09:00")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", post_time):
            raise ValueError("POST_TIME must be HH:MM in 24-hour time.")
        history_days = int(os.environ.get("HISTORY_DAYS", "365"))
        attempts = int(os.environ.get("FETCH_ATTEMPTS", "20"))
        if history_days <= 0 or attempts <= 0:
            raise ValueError("HISTORY_DAYS and FETCH_ATTEMPTS must be positive integers.")
        easy_max = int(os.environ.get("EASY_MAX_RATING", "1400"))
        medium_max = int(os.environ.get("MEDIUM_MAX_RATING", "1800"))
        if not 0 <= easy_max < medium_max:
            raise ValueError("Rating cutoffs must satisfy 0 <= EASY_MAX_RATING < MEDIUM_MAX_RATING.")
        template = Path(os.environ.get("TEMPLATE_PATH", "assets/daily_message.txt")).read_text()
        values = {"date": "2000-01-01"}
        for difficulty in DIFFICULTIES:
            values[f"{difficulty}_rating"] = 1500
        rendered = template.format(**values)
        if not rendered.strip() or len(rendered) > 3000:
            raise ValueError("Daily message must contain 1–3000 characters.")
        return cls(channel, time.fromisoformat(post_time),
                   ZoneInfo(os.environ.get("TIMEZONE", "Asia/Seoul")), history_days,
                   Path(os.environ.get("DATABASE_PATH", "data/daily-chess.sqlite3")),
                   template, attempts, easy_max, medium_max)


class DailyChess:
    def __init__(self, settings, store, lichess, slack):
        self.settings, self.store, self.lichess, self.slack = settings, store, lichess, slack
        self._post_lock = Lock()
        self._retry_at = 0.0

    def command(self, ack, command, respond):
        ack()
        try:
            if command["channel_id"] != self.settings.channel:
                result = "Use this command in the configured daily-chess channel."
            else:
                result = self.handle(command["text"].split(), command["user_id"], command.get("user_name"))
        except ValueError as exc:
            result = str(exc)
        except LichessError:
            result = "Lichess is unavailable or rate-limited. Please try again later."
        except SlackApiError:
            result = "Slack is unavailable or rate-limited. Please try again later."
        except Exception as exc:
            logging.error("Private command failed (%s).", type(exc).__name__)
            result = "Couldn't finish that request. Please try again."
        respond(text=result, response_type="ephemeral")

    def handle(self, parts, user_id, nickname=None):
        if not re.fullmatch(r"[UW][A-Z0-9]+", user_id):
            raise ValueError("Invalid Slack user ID.")
        if parts and parts[0] == "answer":
            if len(parts) != 3:
                return "Use `/daily-chess answer <puzzle ID> <move>` with one move, such as `Nf3` or `O-O`."
            return self.answer(parts[1], parts[2], user_id, nickname)
        if parts == ["post"]:
            self.tick(datetime.now(self.settings.timezone), manual=True)
            return "Puzzle set posted."
        if parts in (["subscribe"], ["unsubscribe"]):
            enabled = parts == ["subscribe"]
            self.store.subscribe(user_id, enabled)
            return "You're subscribed to daily chess!" if enabled else "You're unsubscribed."
        if len(parts) == 2 and parts[0] == "submit":
            puzzle = self.lichess.get(parts[1])
            if puzzle.rating <= self.settings.easy_max_rating:
                difficulty = "easy"
            elif puzzle.rating <= self.settings.medium_max_rating:
                difficulty = "medium"
            else:
                difficulty = "hard"
            if not self.store.add(puzzle, difficulty, datetime.now().timestamp()):
                return "That puzzle is already queued or was posted within the history period."
            return f"Puzzle added to the {difficulty} bin (rating {puzzle.rating})."
        return HELP

    def load_puzzle(self, saved):
        puzzle = Puzzle(saved["id"], saved["rating"], saved["fen"], saved["first_move"])
        if not puzzle.fen or not puzzle.first_move:
            puzzle = self.lichess.get(puzzle.id)
            self.store.cache_answer(puzzle)
        return puzzle

    def answer(self, identifier, move, user_id, nickname=None):
        identifier = puzzle_id(identifier)
        with self._post_lock:
            now = datetime.now(self.settings.timezone)
            day = now.date().isoformat()
            saved = self.store.posted_puzzle(identifier)
            if (saved is None or saved["channel"] != self.settings.channel
                    or datetime.fromtimestamp(saved["used_at"], self.settings.timezone).date() != now.date()):
                return "Choose a puzzle posted today in this channel; its ID is shown on the puzzle card."
            puzzle = self.load_puzzle(saved)
            if not puzzle.accepts(move):
                return "That isn't the correct first move. Use algebraic notation (e.g. Nf3 or O-O) and try again."
            place, fresh = self.store.solve(day, saved["difficulty"], user_id, saved["reserved_post"], nickname)
            self.announce_solves()
            status = "You're" if fresh else "You already hold"
            return f"Correct! {status} #{place} for {saved['difficulty']} today."

    def announce_solves(self):
        if monotonic() < self._retry_at:
            return
        for solve in self.store.pending_solves():
            try:
                self.slack.chat_postMessage(
                    channel=solve["channel"], thread_ts=solve["thread_ts"], reply_broadcast=True,
                    text=f"<@{solve['user_id']}> solved the {solve['difficulty']} daily puzzle "
                         f"— #{solve['place']} for {solve['day']}!",
                )
            except Exception as exc:
                delay = 30
                if isinstance(exc, SlackApiError) and exc.response.status_code == 429:
                    delay = max(delay, int(exc.response.headers.get("Retry-After", "60")))
                self._retry_at = monotonic() + delay
                logging.error("Solve announcement failed (%s); retrying.", type(exc).__name__)
                return
            self.store.announced(solve)

    def view_board(self, ack, body, action, respond):
        ack()
        try:
            identifier = puzzle_id(action["action_id"].removeprefix("view_board_"))
            self.slack.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Full board"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [{
                    "type": "image",
                    "image_url": f"https://lichess.org/training/export/gif/thumbnail/{identifier}.gif",
                    "alt_text": f"Starting position of puzzle {identifier}.",
                }],
            })
        except (ValueError, SlackApiError):
            respond(text="Couldn't open the board in Slack. Please try again.",
                    response_type="ephemeral", replace_original=False)

    def open_answer(self, ack, body, action, respond):
        ack()
        try:
            identifier = puzzle_id(action["action_id"].removeprefix("answer_puzzle_"))
            self.slack.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal",
                "callback_id": "submit_answer",
                "private_metadata": identifier,
                "title": {"type": "plain_text", "text": f"Answer puzzle {identifier}"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [{
                    "type": "input", "block_id": "answer",
                    "label": {"type": "plain_text", "text": "First move (algebraic notation)"},
                    "element": {
                        "type": "plain_text_input", "action_id": "move", "focus_on_load": True,
                        "placeholder": {"type": "plain_text", "text": "e.g. Nf3, Rxe7, or O-O"},
                    },
                }],
            })
        except (ValueError, SlackApiError):
            respond(text="Couldn't open the answer form. Please try again.",
                    response_type="ephemeral", replace_original=False)

    def submit_answer(self, ack, body, view):
        user = body["user"]
        identifier = view["private_metadata"]
        move = view["state"]["values"]["answer"]["move"]["value"] or ""
        self.command(ack, {
            "channel_id": self.settings.channel, "user_id": user["id"],
            "user_name": user.get("username") or user.get("name"),
            "text": f"answer {identifier} {move}",
        }, lambda **reply: self.slack.chat_postEphemeral(
            channel=self.settings.channel, user=user["id"], text=reply["text"]))

    def previous_rankings(self, post):
        if post["id"].startswith("manual:"):
            return ""
        yesterday = (date.fromisoformat(post["id"]) - timedelta(days=1)).isoformat()
        previous = self.store.post(yesterday)
        if not previous or not previous["thread_ts"] or previous["channel"] != post["channel"]:
            return ""
        entries = {difficulty: [] for difficulty in DIFFICULTIES}
        names = {}
        for solve in self.store.scheduled_ranking(yesterday):
            user_id = solve["user_id"]
            if user_id not in names:
                nickname = solve["nickname"] or user_id
                try:
                    user = self.slack.users_info(user=user_id)["user"]
                    profile = user.get("profile") or {}
                    nickname = (profile.get("display_name") or profile.get("real_name")
                                or user.get("name") or nickname)
                except (SlackApiError, OSError) as exc:
                    logging.warning("Nickname lookup failed (%s); using the saved username.", type(exc).__name__)
                names[user_id] = " ".join(nickname.split()) or user_id
            medal = ("🥇", "🥈", "🥉")[solve["place"] - 1]
            entries[solve["difficulty"]].append(f"{medal} {names[user_id]}")
        return f"Scheduled puzzle rankings — {yesterday}\n" + "\n".join(
            f"{difficulty.title()}: " + (" · ".join(entries[difficulty]) or "No solvers")
            for difficulty in DIFFICULTIES)

    def deliver(self, post, now):
        thread_ts = post["thread_ts"]
        if not thread_ts:
            puzzles = self.store.selected(post["id"])
            if [puzzle["difficulty"] for puzzle in puzzles] != list(DIFFICULTIES):
                raise ValueError("Each daily post needs one puzzle from every bin.")
            cards = []
            turns = []
            for puzzle in puzzles:
                turn = self.load_puzzle(puzzle).to_move
                turns.append(f"{puzzle['difficulty'].title()}: *{turn}* to move")
                title = f"{puzzle['difficulty'].title()} · Rating {puzzle['rating']}"
                # The thumbnail is a starting position; the full GIF reveals the solution.
                image_url = f"https://lichess.org/training/export/gif/thumbnail/{puzzle['id']}.gif"
                cards.append({
                    "type": "card",
                    "title": {"type": "plain_text", "text": title},
                    "body": {"type": "mrkdwn", "text": f"`/daily-chess answer {puzzle['id']} <move>`"},
                    "hero_image": {
                        "type": "image",
                        "image_url": image_url,
                        "alt_text": f"{title}. {turn} to move.",
                    },
                    "actions": [{
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View full board"},
                        "action_id": f"view_board_{puzzle['id']}",
                    }, {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Submit answer"},
                        "action_id": f"answer_puzzle_{puzzle['id']}",
                    }],
                })
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": post["body"]}},
                {"type": "carousel", "elements": cards},
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(turns)}},
            ]
            fallback = post["body"] + "\n" + "\n".join(
                f"{card['hero_image']['alt_text']} {card['body']['text']}" for card in cards)
            recap = self.previous_rankings(post)
            if recap:
                blocks.append({"type": "section", "text": {"type": "plain_text", "text": recap}})
                fallback += "\n\n" + escape(recap, quote=False)
            # ponytail: Slack acceptance and SQLite commit aren't atomic; reconcile via Slack history if needed.
            response = self.slack.chat_postMessage(channel=post["channel"], text=fallback, blocks=blocks,
                                                   unfurl_links=False, unfurl_media=False, parse="none")
            thread_ts = response["ts"]
            self.store.posted(post["id"], thread_ts, now.timestamp())
        notifications = json.loads(post["notifications"])
        for index in range(post["notified"], len(notifications)):
            self.slack.chat_postMessage(channel=post["channel"], thread_ts=thread_ts,
                                        text=notifications[index], reply_broadcast=False)
            self.store.notified(post["id"], index + 1)

    def tick(self, now, *, manual=False):
        # Serialize posting, answer ranking, and Slack delivery.
        with self._post_lock:
            self.announce_solves()
            if monotonic() < self._retry_at:
                raise ValueError("Slack is rate-limited. Please try again later.")
            try:
                now = now.astimezone(self.settings.timezone)
                day = now.date().isoformat()
                posted_today = False
                for post in self.store.pending(manual=manual):
                    self.deliver(post, now)
                    if manual:
                        return True
                    posted_today = posted_today or post["id"] == day
                if not manual and (now.time() < self.settings.post_time or self.store.post(day)):
                    return posted_today
                for difficulty in DIFFICULTIES:
                    for _ in range(self.settings.fetch_attempts):
                        if self.store.has_queued(difficulty):
                            break
                        self.store.add(self.lichess.random(difficulty), difficulty, now.timestamp())
                    if not self.store.has_queued(difficulty):
                        raise RuntimeError(f"Could not find a fresh {difficulty} puzzle.")
                post_id = f"manual:{day}:{uuid4()}" if manual else day
                self.store.prepare(day, self.settings.channel, self.settings.template, post_id=post_id)
                self.deliver(self.store.post(post_id), now)
                return True
            except SlackApiError as exc:
                if exc.response.status_code == 429:
                    self._retry_at = monotonic() + max(1, int(exc.response.headers.get("Retry-After", "60")))
                raise


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    for name, prefix in (("SLACK_BOT_TOKEN", "xoxb-"), ("SLACK_APP_TOKEN", "xapp-")):
        if not os.environ.get(name, "").startswith(prefix):
            raise ValueError(f"Set {name} in .env.")
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: one process per SQLite database; use a distributed scheduler for multiple hosts.
    with settings.database.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("daily-chess is already running for this database.")
        slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=15, retry_handlers=[])
        app = App(client=slack)
        bot = DailyChess(settings, Store(settings.database, settings.history_days), Lichess(), slack)
        app.command("/daily-chess")(bot.command)
        app.action(re.compile(r"^view_board_[A-Za-z0-9]{5}$"))(bot.view_board)
        app.action(re.compile(r"^answer_puzzle_[A-Za-z0-9]{5}$"))(bot.open_answer)
        app.view("submit_answer")(bot.submit_answer)
        socket = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        stop = Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        socket.connect()
        logging.info("daily-chess running; daily post at %s %s.", settings.post_time, settings.timezone)
        try:
            while not stop.is_set():
                delay = 30
                try:
                    bot.tick(datetime.now(settings.timezone))
                except Exception as exc:
                    logging.exception("Daily posting failed; retrying.")
                    delay = 60
                    if isinstance(exc, SlackApiError):
                        delay = max(delay, int(exc.response.headers.get("Retry-After", "60")))
                stop.wait(delay)
        finally:
            socket.close()
