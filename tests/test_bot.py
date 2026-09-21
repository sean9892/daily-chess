import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import chess

from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from daily_chess.bot import DailyChess, Settings
from daily_chess.lichess import LichessError, Puzzle
from daily_chess.store import DIFFICULTIES, Store

TEMPLATE = "{date}: Easy {easy_rating}, Medium {medium_rating}, Hard {hard_rating}"
NOW = datetime(2026, 9, 20, 9, tzinfo=ZoneInfo("Asia/Seoul"))


class BotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "chess.sqlite3"
        self.settings = Settings("C123", time(9), NOW.tzinfo, 2, self.path, TEMPLATE, 3, 1400, 1800)
        self.store = Store(self.path, 2)
        self.lichess = Mock()
        self.lichess.get.side_effect = lambda identifier: Puzzle(identifier, 1500, chess.STARTING_FEN, "e2e4")
        self.lichess.preview.side_effect = lambda identifier: f"PNG {identifier}".encode()
        self.slack = Mock()
        self.slack.chat_postMessage.return_value = {"ts": "123.456"}
        self.slack.files_upload_v2.side_effect = lambda **kw: {"files": [{"id": f"F{Path(kw['filename']).stem}"}]}
        self.slack.users_info.side_effect = lambda user: {"user": {"profile": {"display_name": f"Player {user}"}}}
        self.bot = DailyChess(self.settings, self.store, self.lichess, self.slack)

    def fill(self):
        for index, difficulty in enumerate(DIFFICULTIES):
            self.store.add(Puzzle(f"0000{index}", 1000 + index * 500, chess.STARTING_FEN, "e2e4"),
                           difficulty, NOW.timestamp())

    def test_fifo_history_expiration_and_atomic_duplicate_rejection(self):
        self.fill()
        later = Puzzle("later", 1000)
        self.store.add(later, "easy", NOW.timestamp() + 1)
        self.store.prepare("2026-09-20", "C123", TEMPLATE)
        self.assertEqual([puzzle["id"] for puzzle in self.store.selected("2026-09-20")],
                         ["00000", "00001", "00002"])
        self.assertTrue(self.store.has_queued("easy"))
        self.assertFalse(self.store.has_queued("medium"))
        self.assertFalse(self.store.add(Puzzle("00000", 1000), "hard", NOW.timestamp() + 864000))
        self.store.posted("2026-09-20", "123.456", NOW.timestamp())
        self.assertFalse(self.store.add(Puzzle("00000", 1000), "hard", NOW.timestamp() + 172799))
        self.assertTrue(self.store.add(Puzzle("00000", 1000), "hard", NOW.timestamp() + 172800))
        self.assertFalse(self.store.add(later, "medium", NOW.timestamp() + 864000))
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.store.add(Puzzle("fresh", 1500), "medium", NOW.timestamp()), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_prepare_rolls_back_if_any_bin_is_empty_or_template_invalid(self):
        self.store.add(Puzzle("00000", 1000), "easy", NOW.timestamp())
        with self.assertRaises(ValueError):
            self.store.prepare("2026-09-20", "C123", TEMPLATE)
        self.assertTrue(self.store.has_queued("easy"))
        self.assertIsNone(self.store.post("2026-09-20"))
        self.fill()
        with self.assertRaises(KeyError):
            self.store.prepare("2026-09-20", "C123", "{unknown}")
        self.assertTrue(all(self.store.has_queued(d) for d in DIFFICULTIES))

    def test_legacy_database_migration_preserves_posts_history_and_queue(self):
        self.fill()
        self.store.subscribe("U123", True)
        self.store.add(Puzzle("later", 1000), "easy", NOW.timestamp() + 1)
        self.store.prepare("2026-09-20", "C123", TEMPLATE)
        self.store.posted("2026-09-20", "123.456", NOW.timestamp())
        post = dict(self.store.post("2026-09-20"))
        puzzles = [dict(puzzle) for puzzle in self.store.selected("2026-09-20")]
        for puzzle in puzzles:
            puzzle.update(fen=None, first_move=None)
        self.store.solve("2026-09-20", "easy", "U1", "manual:2026-09-20:old")
        self.store.solve("2026-09-20", "easy", "U2", "2026-09-20")
        self.store.announced({"day": "2026-09-20", "difficulty": "easy", "user_id": "U2"})
        with self.store.connect() as db:
            db.execute("DROP TABLE scheduled_solves")
            db.execute("ALTER TABLE posts RENAME COLUMN id TO day")
            db.execute("ALTER TABLE puzzles RENAME COLUMN reserved_post TO reserved_day")
            for name in ("fen", "first_move", "preview_file_id"):
                db.execute(f"ALTER TABLE puzzles DROP COLUMN {name}")
        for _ in range(2):
            migrated = Store(self.path, 2)
            self.assertEqual(dict(migrated.post("2026-09-20")), post)
            self.assertEqual([dict(puzzle) for puzzle in migrated.selected("2026-09-20")], puzzles)
            self.assertTrue(migrated.has_queued("easy"))
            self.assertFalse(migrated.add(Puzzle("00000", 1000), "easy", NOW.timestamp()))
            self.assertEqual(migrated.pending(manual=True), [])
            self.assertEqual([(row["user_id"], row["place"]) for row in migrated.scheduled_ranking("2026-09-20")],
                             [("U2", 1)])
            with migrated.connect() as db:
                self.assertEqual([row[0] for row in db.execute("SELECT user_id FROM subscribers")], ["U123"])
        restarted = DailyChess(self.settings, migrated, self.lichess, self.slack)
        self.assertTrue(restarted.tick(NOW))
        self.slack.chat_postMessage.assert_called_once_with(
            channel="C123", thread_ts="123.456", text="It's time for daily chess! <@U123>", reply_broadcast=False)
        self.assertFalse(restarted.tick(NOW))

    def test_schedule_restart_and_thread_notifications(self):
        self.fill()
        self.store.cache_answer(Puzzle("00001", 1500, chess.STARTING_FEN.replace(" w ", " b "), "e7e5"))
        for user in ("U123", "U123", "U456", "U789"):
            self.bot.handle(["subscribe"], user)
        self.bot.handle(["unsubscribe"], "U789")
        self.bot.tick(NOW - timedelta(seconds=1))
        self.slack.chat_postMessage.assert_not_called()
        self.bot.tick(NOW.astimezone(ZoneInfo("UTC")))
        parent, reply = self.slack.chat_postMessage.call_args_list
        self.assertNotIn("thread_ts", parent.kwargs)
        blocks = parent.kwargs["blocks"]
        self.assertEqual([block["type"] for block in blocks], ["section", "carousel", "section"])
        self.assertEqual(blocks[2]["text"]["text"],
                         "Easy: *White* to move\nMedium: *Black* to move\nHard: *White* to move")
        cards = blocks[1]["elements"]
        self.assertEqual([card["type"] for card in cards], ["card"] * 3)
        self.assertEqual([card["hero_image"]["slack_file"] for card in cards],
                         [{"id": f"F0000{i}"} for i in range(3)])
        self.assertEqual([call.args[0] for call in self.lichess.preview.call_args_list],
                         [f"0000{i}" for i in range(3)])
        self.assertEqual([call.kwargs for call in self.slack.files_upload_v2.call_args_list], [
            {"file": f"PNG 0000{i}".encode(), "filename": f"0000{i}.png",
             "title": f"{difficulty.title()} · Rating {rating}"}
            for i, (difficulty, rating) in enumerate(zip(DIFFICULTIES, (1000, 1500, 2000)))
        ])
        for card, difficulty, rating in zip(cards, DIFFICULTIES, (1000, 1500, 2000)):
            self.assertEqual(card["title"], {"type": "plain_text", "text": f"{difficulty.title()} · Rating {rating}"})
            self.assertEqual(card["hero_image"]["type"], "image")
            self.assertIn("to move", card["hero_image"]["alt_text"])
            self.assertIn(card["hero_image"]["alt_text"], parent.kwargs["text"])
        self.assertNotIn("https://", parent.kwargs["text"])
        self.assertEqual(reply.kwargs["text"], "It's time for daily chess! <@U123> <@U456>")
        self.assertEqual(reply.kwargs["thread_ts"], "123.456")
        self.assertFalse(reply.kwargs["reply_broadcast"])
        self.lichess.random.assert_not_called()
        restarted = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
        restarted.tick(NOW + timedelta(hours=5))
        self.assertEqual(self.slack.chat_postMessage.call_count, 2)
        self.lichess.random.side_effect = [Puzzle("new00", 1000), Puzzle("new01", 1500), Puzzle("new02", 2000)]
        restarted.tick(NOW + timedelta(days=1, hours=2))
        self.assertEqual(self.slack.chat_postMessage.call_count, 4)
        recap = self.slack.chat_postMessage.call_args_list[2].kwargs["blocks"][-1]["text"]["text"]
        self.assertEqual(recap,
                         "Scheduled puzzle rankings — 2026-09-20\nEasy: No solvers\nMedium: No solvers\nHard: No solvers")
        self.assertEqual([c.args[0] for c in self.lichess.random.call_args_list], list(DIFFICULTIES))

    def test_board_buttons_open_full_images_in_slack(self):
        self.fill()
        self.bot.tick(NOW)
        cards = self.slack.chat_postMessage.call_args.kwargs["blocks"][1]["elements"]
        ack, respond = Mock(), Mock()
        def open_view(**kwargs):
            ack.assert_called_once_with()
            return {"ok": True}
        self.slack.views_open.side_effect = open_view
        for index, card in enumerate(cards):
            button = card["actions"][0]
            self.assertEqual(button, {
                "type": "button",
                "text": {"type": "plain_text", "text": "View full board"},
                "action_id": f"view_board_0000{index}",
            })
            self.assertIn(f"/daily-chess answer 0000{index}", card["body"]["text"])
            ack.reset_mock()
            self.bot.view_board(ack, {"trigger_id": "trigger"}, button, respond)
            request = self.slack.views_open.call_args.kwargs
            self.assertEqual(request["trigger_id"], "trigger")
            view = request["view"]
            self.assertEqual(view["type"], "modal")
            self.assertEqual(view["close"], {"type": "plain_text", "text": "Close"})
            board, = view["blocks"]
            self.assertEqual(board["type"], "image")
            self.assertEqual(board["image_url"],
                             f"https://lichess.org/training/export/gif/thumbnail/0000{index}.gif")
            self.assertEqual(board["alt_text"], f"Starting position of puzzle 0000{index}.")
        respond.assert_not_called()
        self.lichess.get.assert_not_called()
        self.slack.chat_postMessage.assert_called_once()

        self.slack.views_open.reset_mock()
        self.bot.view_board(Mock(), {"trigger_id": "trigger"},
                            {"action_id": "view_board_../../solution"}, respond)
        self.slack.views_open.assert_not_called()
        self.slack.views_open.side_effect = SlackApiError("expired trigger", {"error": "expired_trigger_id"})
        self.bot.view_board(Mock(), {"trigger_id": "expired"}, button, respond)
        self.assertEqual(respond.call_count, 2)
        for call in respond.call_args_list:
            self.assertEqual(call.kwargs["response_type"], "ephemeral")
            self.assertFalse(call.kwargs["replace_original"])

    def test_failed_posts_preserve_selection_and_resume_notification_batches(self):
        self.fill()
        for index in range(51):
            self.store.subscribe(f"U{index}", True)
        self.slack.chat_postMessage.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            self.bot.tick(NOW)
        prepared = self.store.post("2026-09-20")
        original_blocks = self.slack.chat_postMessage.call_args.kwargs["blocks"]
        self.assertIsNone(prepared["thread_ts"])
        self.assertFalse(self.store.has_queued("easy"))
        self.bot = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
        self.slack.chat_postMessage.side_effect = [{"ts": "123.456"}, {"ts": "124"}, RuntimeError("offline")]
        with self.assertRaises(RuntimeError):
            self.bot.tick(NOW)
        self.assertEqual(self.slack.chat_postMessage.call_args_list[1].kwargs["blocks"], original_blocks)
        self.assertEqual(self.slack.files_upload_v2.call_count, 3)
        self.assertEqual(self.lichess.preview.call_count, 3)
        self.assertEqual(self.store.post("2026-09-20")["notified"], 1)
        self.slack.reset_mock(side_effect=True)
        self.bot.tick(NOW)
        self.slack.chat_postMessage.assert_called_once()
        self.assertEqual(self.slack.chat_postMessage.call_args.kwargs["thread_ts"], "123.456")
        self.assertEqual(self.slack.chat_postMessage.call_args.kwargs["text"], json.loads(prepared["notifications"])[1])
        self.assertEqual(self.store.pending(), [])
        self.lichess.random.assert_not_called()

    def test_partial_preview_uploads_resume_after_restart_without_posting_incomplete_boards(self):
        self.fill()
        upload = self.slack.files_upload_v2.side_effect
        self.slack.files_upload_v2.side_effect = [{"files": [{"id": "F00000"}]}, RuntimeError("offline")]
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.bot.tick(NOW)
        self.slack.chat_postMessage.assert_not_called()
        saved = self.store.selected("2026-09-20")
        self.assertEqual([puzzle["preview_file_id"] for puzzle in saved], ["F00000", None, None])
        self.assertTrue(all(puzzle["used_at"] is None for puzzle in saved))
        self.slack.files_upload_v2.reset_mock(side_effect=True)
        self.slack.files_upload_v2.side_effect = upload
        self.lichess.preview.reset_mock()
        restarted = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
        restarted.tick(NOW)
        self.assertEqual([call.args[0] for call in self.lichess.preview.call_args_list], ["00001", "00002"])
        self.assertEqual(self.slack.files_upload_v2.call_count, 2)
        self.slack.chat_postMessage.assert_called_once()
        cards = self.slack.chat_postMessage.call_args.kwargs["blocks"][1]["elements"]
        self.assertEqual([card["hero_image"]["slack_file"] for card in cards],
                         [{"id": f"F0000{i}"} for i in range(3)])
        self.assertEqual(self.store.pending(), [])

    def test_duplicates_are_skipped_and_exhaustion_does_not_post(self):
        self.fill()
        self.bot.tick(NOW)
        self.slack.reset_mock()
        self.lichess.random.return_value = Puzzle("00000", 1000)
        with self.assertRaisesRegex(RuntimeError, "fresh easy"):
            self.bot.tick(NOW + timedelta(days=1))
        self.assertEqual(self.lichess.random.call_count, 3)
        self.slack.chat_postMessage.assert_not_called()
        self.assertIsNone(self.store.post("2026-09-21"))
        self.lichess.random.side_effect = [Puzzle("00000", 1000), Puzzle("new00", 1000),
                                            Puzzle("new01", 1500), Puzzle("new02", 2000)]
        self.bot.tick(NOW + timedelta(days=1))
        self.assertEqual(self.store.selected("2026-09-21")[0]["id"], "new00")

    def test_commands_ack_before_fetch_and_always_respond_privately(self):
        ack, respond = Mock(), Mock()
        def fetch(_):
            ack.assert_called_once_with()
            return Puzzle("00008", 1500)
        self.lichess.get.side_effect = fetch
        command = {"channel_id": "C123", "user_id": "U123", "text": "submit 00008"}
        self.bot.command(ack, command, respond)
        self.assertIn("added", respond.call_args.kwargs["text"])
        ack.reset_mock()
        self.bot.command(ack, command, respond)
        self.assertIn("already queued", respond.call_args.kwargs["text"])
        self.lichess.get.side_effect = LichessError("offline")
        self.bot.command(ack, command, respond)
        self.assertIn("try again", respond.call_args.kwargs["text"])
        self.lichess.get.reset_mock()
        self.bot.command(ack, {**command, "channel_id": "C999"}, respond)
        self.lichess.get.assert_not_called()
        self.bot.command(ack, {**command, "text": "submit easy 00008"}, respond)
        self.lichess.get.assert_not_called()
        self.bot.command(ack, {**command, "text": "help"}, respond)
        self.assertTrue(all(c.kwargs["response_type"] == "ephemeral" for c in respond.call_args_list))
        self.slack.chat_postMessage.assert_not_called()

    def test_manual_posts_are_private_and_independent_of_daily_status(self):
        self.fill()
        self.store.subscribe("U123", True)
        self.lichess.random.side_effect = [Puzzle(f"new{i}{j}", 1000 + j * 500)
                                            for i in range(3) for j in range(3)]
        early = NOW.replace(hour=0, minute=30).astimezone(ZoneInfo("UTC"))
        self.assertFalse(self.bot.tick(early))
        ack, respond = Mock(), Mock()
        command = {"channel_id": "C123", "user_id": "U123", "text": "post"}
        def send(**kwargs):
            ack.assert_called_once_with()
            return {"ts": "123.456"}
        self.slack.chat_postMessage.side_effect = send
        self.bot.command(ack, {**command, "channel_id": "C999"}, respond)
        self.slack.chat_postMessage.assert_not_called()
        ack.reset_mock()
        with patch("daily_chess.bot.datetime") as clock:
            clock.now.return_value = early
            self.bot.command(ack, command, respond)
            self.assertEqual(respond.call_args.kwargs["text"], "Puzzle set posted.")
            ack.reset_mock()
            self.bot.command(ack, command, respond)
            self.assertEqual(respond.call_args.kwargs["text"], "Puzzle set posted.")
        self.assertIsNone(self.store.post("2026-09-20"))
        self.assertTrue(self.bot.tick(NOW))
        daily = dict(self.store.post("2026-09-20"))
        with patch("daily_chess.bot.datetime") as clock:
            clock.now.return_value = NOW
            ack.reset_mock()
            self.bot.command(ack, command, respond)
        self.assertEqual(respond.call_args.kwargs["text"], "Puzzle set posted.")
        self.assertEqual(dict(self.store.post("2026-09-20")), daily)
        self.assertFalse(self.bot.tick(NOW))
        self.assertEqual(self.slack.chat_postMessage.call_count, 8)
        parent, reply = self.slack.chat_postMessage.call_args_list[:2]
        self.assertTrue(parent.kwargs["text"].startswith("2026-09-20:"))
        self.assertEqual(parent.kwargs["blocks"][1]["type"], "carousel")
        self.assertEqual(len(parent.kwargs["blocks"][1]["elements"]), 3)
        self.assertEqual(reply.kwargs["text"], "It's time for daily chess! <@U123>")
        self.assertEqual(reply.kwargs["thread_ts"], "123.456")
        self.assertTrue(all(call.kwargs["response_type"] == "ephemeral" for call in respond.call_args_list))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM posts WHERE id LIKE 'manual:%'").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(DISTINCT reserved_post) FROM puzzles").fetchone()[0], 4)

    def test_manual_retry_survives_restart_without_blocking_daily_post(self):
        self.store.subscribe("U123", True)
        self.lichess.random.side_effect = [Puzzle("new00", 1000), Puzzle("new01", 1500), Puzzle("new02", 2000)]
        self.slack.chat_postMessage.side_effect = [{"ts": "123.456"}, RuntimeError("offline")]
        ack, respond = Mock(), Mock()
        command = {"channel_id": "C123", "user_id": "U123", "text": "post"}
        with patch("daily_chess.bot.datetime") as clock:
            clock.now.return_value = NOW - timedelta(hours=1)
            with self.assertLogs(level="ERROR"):
                self.bot.command(ack, command, respond)
            self.assertIn("try again", respond.call_args.kwargs["text"])
            pending, = self.store.pending(manual=True)
            self.assertEqual(pending["thread_ts"], "123.456")
            self.assertIsNone(self.store.post("2026-09-20"))
            restarted = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
            self.slack.chat_postMessage.reset_mock(side_effect=True)
            self.assertFalse(restarted.tick(NOW - timedelta(hours=1)))
            self.slack.chat_postMessage.assert_not_called()
            self.fill()
            self.assertTrue(restarted.tick(NOW))
            self.assertEqual(self.slack.chat_postMessage.call_count, 2)
            self.assertEqual(dict(self.store.post(pending["id"])), dict(pending))
            daily = dict(self.store.post("2026-09-20"))
            self.slack.reset_mock()
            restarted.command(ack, command, respond)
        self.assertEqual(respond.call_args.kwargs["text"], "Puzzle set posted.")
        self.assertTrue(all(call.kwargs["response_type"] == "ephemeral" for call in respond.call_args_list))
        self.slack.chat_postMessage.assert_called_once()
        self.assertEqual(self.slack.chat_postMessage.call_args.kwargs["thread_ts"], "123.456")
        self.assertEqual(self.lichess.random.call_count, 3)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.pending(manual=True), [])
        self.assertEqual(self.store.post(pending["id"])["notified"], 1)
        self.assertEqual(dict(self.store.post("2026-09-20")), daily)

    def test_manual_post_leaves_unfinished_daily_post_untouched(self):
        self.fill()
        self.store.prepare("2026-09-20", "C123", TEMPLATE)
        daily = dict(self.store.post("2026-09-20"))
        self.lichess.random.side_effect = [Puzzle("new00", 1000), Puzzle("new01", 1500), Puzzle("new02", 2000)]
        self.assertTrue(self.bot.tick(NOW, manual=True))
        self.assertEqual(dict(self.store.post("2026-09-20")), daily)
        self.assertTrue(all(puzzle["used_at"] is None for puzzle in self.store.selected("2026-09-20")))
        self.assertTrue(self.bot.tick(NOW))
        self.assertEqual([puzzle["id"] for puzzle in self.store.selected("2026-09-20")],
                         ["00000", "00001", "00002"])
        self.assertEqual(self.slack.chat_postMessage.call_count, 2)

    def test_manual_and_scheduled_posts_cannot_publish_concurrently(self):
        self.fill()
        self.store.subscribe("U123", True)
        self.lichess.random.side_effect = [Puzzle("new00", 1000), Puzzle("new01", 1500), Puzzle("new02", 2000)]
        posting, release, scheduled = Event(), Event(), Event()
        def send(**kwargs):
            if "thread_ts" not in kwargs:
                posting.set()
                if not release.wait(5):
                    raise RuntimeError("Test posting timed out")
            return {"ts": "123.456"}
        def schedule():
            scheduled.set()
            return self.bot.tick(NOW)
        self.slack.chat_postMessage.side_effect = send
        with ThreadPoolExecutor(2) as pool:
            manual = pool.submit(self.bot.tick, NOW, manual=True)
            try:
                self.assertTrue(posting.wait(5))
                automatic = pool.submit(schedule)
                self.assertTrue(scheduled.wait(5))
                with self.assertRaises(TimeoutError):
                    automatic.result(timeout=0.05)
            finally:
                release.set()
            self.assertTrue(manual.result(timeout=5))
            self.assertTrue(automatic.result(timeout=5))
        self.assertEqual(self.slack.chat_postMessage.call_count, 4)
        self.assertEqual([puzzle["id"] for puzzle in self.store.selected("2026-09-20")],
                         ["new00", "new01", "new02"])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.pending(manual=True), [])

    def test_manual_and_scheduled_posts_share_slack_rate_limit_cooldown(self):
        self.fill()
        self.store.subscribe("U123", True)
        limited = SlackApiError("rate limited", SlackResponse(
            client=self.slack, http_verb="POST", api_url="https://slack.com/api/chat.postMessage",
            req_args={}, data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "120"}, status_code=429,
        ))
        self.slack.chat_postMessage.side_effect = [{"ts": "123.456"}, limited]
        ack, respond = Mock(), Mock()
        command = {"channel_id": "C123", "user_id": "U123", "text": "post"}
        with patch("daily_chess.bot.monotonic", return_value=100) as clock, \
                patch("daily_chess.bot.datetime") as wall_clock:
            wall_clock.now.return_value = NOW
            self.bot.command(ack, command, respond)
            self.assertIn("rate-limited", respond.call_args.kwargs["text"])
            self.slack.reset_mock(side_effect=True)
            clock.return_value = 219
            self.bot.command(ack, command, respond)
            self.assertIn("rate-limited", respond.call_args.kwargs["text"])
            with self.assertRaisesRegex(ValueError, "rate-limited"):
                self.bot.tick(NOW + timedelta(seconds=119))
            self.slack.chat_postMessage.assert_not_called()
            clock.return_value = 220
            self.bot.command(ack, command, respond)
            self.assertEqual(respond.call_args.kwargs["text"], "Puzzle set posted.")
        self.slack.chat_postMessage.assert_called_once()
        self.assertEqual(self.slack.chat_postMessage.call_args.kwargs["thread_ts"], "123.456")
        self.assertTrue(all(call.kwargs["response_type"] == "ephemeral" for call in respond.call_args_list))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.pending(manual=True), [])
        self.assertIsNone(self.store.post("2026-09-20"))

    def test_submission_uses_configured_rating_boundaries(self):
        self.lichess.get.side_effect = None
        self.bot.settings = replace(self.settings, easy_max_rating=900, medium_max_rating=1600)
        for index, (rating, difficulty) in enumerate(((0, "easy"), (900, "easy"),
                                                     (901, "medium"), (1600, "medium"),
                                                     (1601, "hard"), (3000, "hard"))):
            identifier = f"0000{index}"
            self.lichess.get.return_value = Puzzle(identifier, rating)
            value = identifier if index % 2 else f"https://lichess.org/training/{identifier}"
            result = self.bot.handle(["submit", value], "U123")
            self.lichess.get.assert_called_with(value)
            self.assertEqual(result, f"Puzzle added to the {difficulty} bin (rating {rating}).")
            with self.store.connect() as db:
                saved = db.execute("SELECT difficulty,rating FROM puzzles WHERE id=?", (identifier,)).fetchone()
            self.assertEqual(tuple(saved), (difficulty, rating))
        self.lichess.get.return_value = Puzzle("00000", 3000)
        self.assertIn("already queued", self.bot.handle(["submit", "00000"], "U123"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT difficulty FROM puzzles WHERE id='00000'").fetchone()[0], "easy")

    def test_private_answers_rank_today_across_manual_posts_and_broadcast_in_their_threads(self):
        self.fill()
        self.slack.chat_postMessage.return_value = {"ts": "daily"}
        self.bot.tick(NOW)
        self.store.cache_answer(Puzzle("00000", 1000))
        ack, respond = Mock(), Mock()
        def fetch(identifier):
            ack.assert_called_once_with()
            return Puzzle(identifier, 1500, chess.STARTING_FEN, "e2e4")
        self.lichess.get.side_effect = fetch
        def answer(identifier, move="e4", user="U1", channel="C123"):
            ack.reset_mock()
            self.bot.command(ack, {"channel_id": channel, "user_id": user,
                                  "text": f"answer {identifier} {move}"}, respond)
            ack.assert_called_once_with()
            self.assertEqual(respond.call_args.kwargs["response_type"], "ephemeral")
            return respond.call_args.kwargs["text"]
        with patch("daily_chess.bot.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            self.assertIn("configured", answer("00000", channel="C999"))
            self.assertIn("posted today", answer("other"))
            self.lichess.get.assert_not_called()
            self.assertIn("isn't", answer("00000", "d4"))
            self.assertEqual(self.store.pending_solves(), [])
            self.assertIn("#1", answer("00000"))
            self.assertIn("already hold #1", answer("00000"))
            self.lichess.get.assert_called_once_with("00000")

            self.lichess.random.side_effect = [Puzzle(f"new0{i}", 1500, chess.STARTING_FEN, "e2e4")
                                                for i in range(3)]
            self.slack.chat_postMessage.return_value = {"ts": "manual"}
            self.bot.tick(NOW, manual=True)
            self.assertIn("already hold #1", answer("new00"))
            self.assertIn("#2", answer("new00", user="U2"))
            self.assertIn("#3", answer("00000", user="U3"))
            self.assertIn("#4", answer("new00", user="U4"))
            for identifier in ("00001", "00002"):
                self.assertIn("#1", answer(identifier))
            notices = [call.kwargs for call in self.slack.chat_postMessage.call_args_list
                       if "thread_ts" in call.kwargs]
            self.assertEqual([notice["thread_ts"] for notice in notices],
                             ["daily", "manual", "daily", "daily", "daily"])
            for notice, user, difficulty, place in zip(notices, ("U1", "U2", "U3", "U1", "U1"),
                                                       ("easy", "easy", "easy", "medium", "hard"),
                                                       (1, 2, 3, 1, 1)):
                self.assertEqual(notice["text"],
                                 f"<@{user}> solved the {difficulty} daily puzzle — #{place} for 2026-09-20!")
                self.assertTrue(notice["reply_broadcast"])
                self.assertNotIn("e4", notice["text"])
            self.bot = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
            self.slack.reset_mock()
            self.assertIn("already hold #1", answer("00000"))
            self.slack.chat_postMessage.assert_not_called()

            clock.now.return_value = NOW + timedelta(days=1)
            self.assertIn("posted today", answer("00000"))
            self.lichess.random.side_effect = [Puzzle(f"next{i}", 1500, chess.STARTING_FEN, "e2e4")
                                                for i in range(3)]
            self.bot.tick(clock.now.return_value)
            self.assertIn("#1", answer("next0"))
            self.assertIn("#1 for 2026-09-21", self.slack.chat_postMessage.call_args.kwargs["text"])

    def test_solve_ranks_are_atomic_and_failed_announcements_resume_after_restart(self):
        self.fill()
        self.bot.tick(NOW)
        self.slack.reset_mock()
        users = ["U1"] * 5 + [f"U{i}" for i in range(2, 8)]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda user: self.store.solve("2026-09-20", "easy", user, "2026-09-20"), users))
        self.assertEqual(sorted(place for place, fresh in results if fresh), list(range(1, 8)))
        self.assertEqual(len({place for place, _ in results[:5]}), 1)
        self.assertEqual([row["place"] for row in self.store.pending_solves()], [1, 2, 3])
        self.assertEqual([row["place"] for row in self.store.scheduled_ranking("2026-09-20")], [1, 2, 3])
        self.slack.chat_postMessage.side_effect = [{"ts": "first"}, RuntimeError("offline")]
        with self.assertLogs(level="ERROR"):
            self.bot.announce_solves()
        self.assertEqual([row["place"] for row in self.store.pending_solves()], [2, 3])
        self.slack.reset_mock(side_effect=True)
        restarted = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
        restarted.tick(NOW)
        self.assertEqual(self.slack.chat_postMessage.call_count, 2)
        for call, place in zip(self.slack.chat_postMessage.call_args_list, (2, 3)):
            self.assertIn(f"#{place}", call.kwargs["text"])
            self.assertEqual(call.kwargs["thread_ts"], "123.456")
            self.assertTrue(call.kwargs["reply_broadcast"])
        self.assertEqual(self.store.pending_solves(), [])
        restarted.tick(NOW)
        self.assertEqual(self.slack.chat_postMessage.call_count, 2)

    def test_answers_keep_their_place_during_slack_rate_limits(self):
        for index, difficulty in enumerate(DIFFICULTIES):
            self.store.add(Puzzle(f"0000{index}", 1500, chess.STARTING_FEN, "e2e4"), difficulty, NOW.timestamp())
        self.bot.tick(NOW)
        self.slack.reset_mock()
        self.slack.chat_postMessage.side_effect = SlackApiError("rate limited", SlackResponse(
            client=self.slack, http_verb="POST", api_url="https://slack.com/api/chat.postMessage",
            req_args={}, data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "120"}, status_code=429,
        ))
        with patch("daily_chess.bot.datetime", wraps=datetime) as wall_clock, \
                patch("daily_chess.bot.monotonic", return_value=100) as clock:
            wall_clock.now.return_value = NOW
            with self.assertLogs(level="ERROR"):
                self.assertIn("#1", self.bot.handle(["answer", "00000", "e4"], "U1"))
            self.slack.reset_mock(side_effect=True)
            self.assertIn("#2", self.bot.handle(["answer", "00000", "e4"], "U2"))
            clock.return_value = 219
            with self.assertRaisesRegex(ValueError, "rate-limited"):
                self.bot.tick(NOW)
            self.slack.chat_postMessage.assert_not_called()
            clock.return_value = 220
            self.bot.tick(NOW)
        self.assertEqual(self.slack.chat_postMessage.call_count, 2)
        self.assertEqual(self.store.pending_solves(), [])

    def test_answer_buttons_open_forms_and_submit_privately(self):
        self.fill()
        self.bot.tick(NOW)
        cards = self.slack.chat_postMessage.call_args.kwargs["blocks"][1]["elements"]
        ack, respond = Mock(), Mock()
        def open_view(**kwargs):
            ack.assert_called_once_with()
        self.slack.views_open.side_effect = open_view
        for index, card in enumerate(cards):
            button = card["actions"][1]
            self.assertEqual(button["text"]["text"], "Submit answer")
            ack.reset_mock()
            self.bot.open_answer(ack, {"trigger_id": "trigger"}, button, respond)
            request = self.slack.views_open.call_args.kwargs
            self.assertEqual(request["trigger_id"], "trigger")
            form = request["view"]
            self.assertEqual(form["callback_id"], "submit_answer")
            self.assertEqual(form["private_metadata"], f"0000{index}")
            self.assertEqual(form["blocks"][0]["element"]["type"], "plain_text_input")
            self.assertNotIn("initial_value", form["blocks"][0]["element"])
        respond.assert_not_called()

        self.slack.chat_postMessage.reset_mock()
        self.slack.chat_postEphemeral.side_effect = lambda **kwargs: ack.assert_called_once_with()
        body = {"user": {"id": "U1", "username": "기사"}}
        with patch("daily_chess.bot.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            for move, expected in (("d4", "isn't"), ("e4 e5", "one move"), ("e4", "#1"),
                                   ("e4", "already hold #1")):
                ack.reset_mock()
                view = {"private_metadata": "00000", "state": {"values": {"answer": {"move": {"value": move}}}}}
                self.bot.submit_answer(ack, body, view)
                feedback = self.slack.chat_postEphemeral.call_args.kwargs
                self.assertEqual((feedback["channel"], feedback["user"]), ("C123", "U1"))
                self.assertIn(expected, feedback["text"])
            clock.now.return_value = NOW + timedelta(days=1)
            ack.reset_mock()
            self.bot.submit_answer(ack, body, view)
            self.assertIn("posted today", self.slack.chat_postEphemeral.call_args.kwargs["text"])
        self.slack.chat_postMessage.assert_called_once()
        notice = self.slack.chat_postMessage.call_args.kwargs
        self.assertTrue(notice["reply_broadcast"])
        self.assertEqual(notice["thread_ts"], "123.456")
        self.assertEqual(self.store.scheduled_ranking("2026-09-20")[0]["nickname"], "기사")

        self.slack.views_open.reset_mock(side_effect=True)
        self.bot.open_answer(Mock(), {"trigger_id": "trigger"}, {"action_id": "answer_puzzle_../../bad"}, respond)
        self.slack.views_open.assert_not_called()
        self.slack.views_open.side_effect = SlackApiError("expired", {"error": "expired_trigger_id"})
        self.bot.open_answer(Mock(), {"trigger_id": "expired"}, button, respond)
        self.assertEqual(respond.call_count, 2)
        for call in respond.call_args_list:
            self.assertEqual(call.kwargs["response_type"], "ephemeral")
            self.assertFalse(call.kwargs["replace_original"])

    def test_next_scheduled_post_recaps_saved_top_three_with_names_without_mentions(self):
        self.fill()
        self.bot.tick(NOW)
        self.lichess.random.side_effect = [Puzzle(f"set{i}{j}", 1500, chess.STARTING_FEN, "e2e4")
                                            for i in range(4) for j in range(3)]
        self.bot.tick(NOW, manual=True)
        with patch("daily_chess.bot.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            for identifier, user, nickname in (("set00", "UM", "수동왕"), ("00000", "U1", "one"),
                                                ("00000", "U1", "one"), ("00000", "UM", "수동왕"),
                                                ("00000", "U2", "two"), ("00000", "U4", "four"),
                                                ("00001", "U1", "one")):
                self.bot.handle(["answer", identifier, "e4"], user, nickname)
        self.bot = DailyChess(self.settings, Store(self.path, 2), self.lichess, self.slack)
        easy = [row for row in self.store.scheduled_ranking("2026-09-20") if row["difficulty"] == "easy"]
        self.assertEqual([(row["user_id"], row["place"]) for row in easy], [("U1", 1), ("UM", 2), ("U2", 3)])
        self.slack.reset_mock()
        next_day = NOW + timedelta(days=1)
        self.bot.tick(next_day - timedelta(hours=1), manual=True)
        self.assertNotIn("rankings", self.slack.chat_postMessage.call_args.kwargs["text"])
        self.slack.users_info.assert_not_called()
        def profile(user):
            if user == "UM":
                raise SlackApiError("missing scope", {"error": "missing_scope"})
            return {"user": {"profile": {"display_name": "기사 <@U999>" if user == "U1" else "",
                                           "real_name": "김 기사"}}}
        self.slack.users_info.side_effect = profile
        with self.assertLogs(level="WARNING"):
            self.bot.tick(next_day.astimezone(ZoneInfo("UTC")))
        message = self.slack.chat_postMessage.call_args.kwargs
        self.assertEqual(message["blocks"][-1]["text"], {
            "type": "plain_text",
            "text": "Scheduled puzzle rankings — 2026-09-20\n"
                    "Easy: 🥇 기사 <@U999> · 🥈 수동왕 · 🥉 김 기사\nMedium: 🥇 기사 <@U999>\nHard: No solvers",
        })
        self.assertEqual(self.slack.users_info.call_count, 3)
        self.assertIn("&lt;@U999&gt;", message["text"])
        self.assertNotIn("<@", message["text"])
        self.assertEqual(message["parse"], "none")
        self.assertNotIn("U4", message["text"])
        self.slack.users_info.reset_mock()
        self.bot.tick(NOW + timedelta(days=3))
        self.assertNotIn("rankings", self.slack.chat_postMessage.call_args.kwargs["text"])
        self.slack.users_info.assert_not_called()

    def test_configuration_validation(self):
        template = Path(self.temp.name) / "template.txt"
        template.write_text(TEMPLATE)
        config = {"SLACK_CHANNEL_ID": "C123", "TEMPLATE_PATH": str(template)}
        with patch.dict(os.environ, config, clear=True), patch("daily_chess.bot.load_dotenv"):
            settings = Settings.load()
            self.assertEqual(settings.post_time, time(9))
            self.assertEqual((settings.easy_max_rating, settings.medium_max_rating), (1400, 1800))
            with patch.dict(os.environ, {"EASY_MAX_RATING": "900", "MEDIUM_MAX_RATING": "1600"}):
                settings = Settings.load()
                self.assertEqual((settings.easy_max_rating, settings.medium_max_rating), (900, 1600))
            for name, value in (("POST_TIME", "24:00"), ("POST_TIME", "9:00"),
                                ("HISTORY_DAYS", "0"), ("FETCH_ATTEMPTS", "-1"),
                                ("EASY_MAX_RATING", "-1"), ("EASY_MAX_RATING", "1800"),
                                ("EASY_MAX_RATING", "2000"), ("EASY_MAX_RATING", "abc"),
                                ("MEDIUM_MAX_RATING", "1400"), ("MEDIUM_MAX_RATING", "1.5"),
                                ("SLACK_CHANNEL_ID", "#chess")):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        Settings.load()
            template.write_text("Daily chess — {date}")
            self.assertEqual(Settings.load().template, "Daily chess — {date}")
            for content in (" ", "x" * 3001):
                template.write_text(content)
                with self.assertRaises(ValueError):
                    Settings.load()


if __name__ == "__main__":
    unittest.main()
