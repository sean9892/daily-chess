from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from io import BytesIO
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import chess
from PIL import Image

from daily_chess.lichess import Lichess, LichessError, Puzzle, puzzle_id


def response(identifier="Ab123", rating=1500):
    return BytesIO(json.dumps({"puzzle": {
        "id": identifier, "rating": rating, "fen": chess.STARTING_FEN, "solution": ["e2e4"],
    }}).encode())


class LichessTests(unittest.TestCase):
    def test_id_and_links_are_normalized_without_fetching_arbitrary_urls(self):
        for value in (" Ab123 ", "https://lichess.org/training/Ab123?x=1#2",
                      "https://lichess.org/training/fork/Ab123",
                      "<https://lichess.org/training/Ab123|Puzzle>",
                      "https://lichess.org/fr/training/Ab123"):
            with self.subTest(value=value):
                self.assertEqual(puzzle_id(value), "Ab123")
        for value in ("abc", "åb123", "https://evil.test/training/Ab123",
                      "https://lichess.org.evil.test/training/Ab123",
                      "https://evil@lichess.org/training/Ab123",
                      "file:///training/Ab123", "https://lichess.org/training", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                puzzle_id(value)

    @patch("daily_chess.lichess.urlopen")
    def test_api_paths_difficulty_mapping_and_response_validation(self, fetch):
        client = Lichess(timeout=3)
        fetch.return_value = response()
        self.assertEqual(client.get("https://lichess.org/training/Ab123"),
                         Puzzle("Ab123", 1500, chess.STARTING_FEN, "e2e4"))
        self.assertEqual(fetch.call_args.args[0].full_url, "https://lichess.org/api/puzzle/Ab123")
        self.assertEqual(fetch.call_args.kwargs, {"timeout": 3})
        for difficulty, api_name in (("easy", "easier"), ("medium", "normal"), ("hard", "harder")):
            fetch.return_value = response()
            self.assertEqual(client.random(difficulty), Puzzle("Ab123", 1500, chess.STARTING_FEN, "e2e4"))
            request = fetch.call_args.args[0]
            self.assertEqual(request.full_url, f"https://lichess.org/api/puzzle/next?difficulty={api_name}")
            self.assertNotIn("Authorization", request.headers)
        with self.assertRaises(ValueError):
            client.random("unknown")
        for payload in (b"not json", b"[]", b"{}", b'{"puzzle":null}'):
            fetch.return_value = BytesIO(payload)
            with self.subTest(payload=payload), self.assertRaises(LichessError):
                client.random("easy")
        for identifier, rating in (("wrong", True), ("wrong", -1), ("!oops", 1500), ("wrong", "1500")):
            fetch.return_value = response(identifier, rating)
            with self.subTest(identifier=identifier, rating=rating), self.assertRaises(LichessError):
                client.random("easy")
        fetch.return_value = response("other")
        with self.assertRaisesRegex(LichessError, "different puzzle"):
            client.get("Ab123")

    @patch("daily_chess.lichess.urlopen")
    def test_preview_preserves_every_pixel_in_a_static_four_by_three_image(self, fetch):
        client = Lichess(timeout=3)
        for size in (80, 81):
            with self.subTest(size=size):
                board = Image.new("RGB", (size, size), "gray")
                board.paste("red", (0, 0, size, size // 8))
                board.paste("blue", (0, size * 7 // 8, size, size))
                source = BytesIO()
                board.save(source, format="GIF", save_all=True,
                           append_images=[Image.new("RGB", board.size, "green")])
                fetch.return_value = BytesIO(source.getvalue())
                with Image.open(BytesIO(client.preview("Ab123"))) as preview:
                    self.assertEqual(preview.format, "PNG")
                    self.assertFalse(getattr(preview, "is_animated", False))
                    self.assertEqual(preview.width * 3, preview.height * 4)
                    left, top = (preview.width - size) // 2, (preview.height - size) // 2
                    self.assertEqual(preview.crop((left, top, left + size, top + size)).tobytes(),
                                     board.tobytes())
                    self.assertEqual(preview.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(fetch.call_args.args[0].full_url,
                         "https://lichess.org/training/export/gif/thumbnail/Ab123.gif")
        self.assertEqual(fetch.call_args.args[0].headers["Accept"], "image/gif")
        self.assertEqual(fetch.call_args.kwargs, {"timeout": 3})
        fetch.return_value = BytesIO(b"not an image")
        with self.assertRaisesRegex(LichessError, "invalid board image"):
            client.preview("Ab123")
        fetch.reset_mock()
        with self.assertRaises(ValueError):
            client.preview("https://evil.test/board.gif")
        fetch.assert_not_called()

    @patch("daily_chess.lichess.urlopen")
    @patch("daily_chess.lichess.time.monotonic", return_value=100)
    @patch("daily_chess.lichess.time.time", return_value=1000)
    def test_rate_limit_cooldown_is_shared_by_get_and_random(self, now, monotonic, fetch):
        for header, cooldown in (("2", 60), ("90", 90), ("bad", 60),
                                 ("inf", 60), (formatdate(1120, usegmt=True), 120)):
            with self.subTest(header=header):
                client = Lichess()
                monotonic.return_value = 100
                fetch.reset_mock()
                fetch.side_effect = HTTPError("https://lichess.org", 429, "limited", {"Retry-After": header}, None)
                with self.assertRaises(LichessError):
                    client.random("easy")
                monotonic.return_value = 100 + cooldown - 1
                with self.assertRaisesRegex(LichessError, "retry in 1 seconds"):
                    client.get("Ab123")
                with self.assertRaisesRegex(LichessError, "retry in 1 seconds"):
                    client.preview("Ab123")
                self.assertEqual(fetch.call_count, 1)
                monotonic.return_value += 1
                fetch.side_effect = None
                fetch.return_value = response()
                self.assertEqual(client.get("Ab123"), Puzzle("Ab123", 1500, chess.STARTING_FEN, "e2e4"))

    @patch("daily_chess.lichess.urlopen")
    def test_fetch_errors_have_useful_messages(self, fetch):
        for error, expected, text in (
            (HTTPError("url", 404, "missing", {}, None), ValueError, "does not exist"),
            (HTTPError("url", 503, "unavailable", {}, None), LichessError, "HTTP 503"),
            (URLError("offline"), LichessError, "Could not reach"),
            (TimeoutError(), LichessError, "Could not reach"),
        ):
            fetch.side_effect = error
            with self.subTest(error=error), self.assertRaisesRegex(expected, text):
                Lichess().get("Ab123")

    @patch("daily_chess.lichess.urlopen")
    def test_answer_data_validation_and_position_after_opponents_move(self, fetch):
        data = json.loads(response().getvalue())
        for field, value in (("fen", None), ("fen", "invalid"),
                             ("fen", "8/8/8/8/8/8/8/8 w - - 0 1"),
                             ("solution", []), ("solution", None), ("solution", "e2e4"),
                             ("solution", [3]), ("solution", ["0000"]), ("solution", ["e7e5"])):
            fetch.return_value = BytesIO(json.dumps({"puzzle": {**data["puzzle"], field: value}}).encode())
            with self.subTest(field=field, value=value), self.assertRaises(LichessError):
                Lichess().get("Ab123")
        fetch.return_value = BytesIO(json.dumps({
            "game": {"pgn": "e4 e5 Nf3 Nc6 Bb5 a6"},
            "puzzle": {"id": "Ab123", "rating": 1500, "solution": ["b5c6", "d7c6"]},
        }).encode())
        puzzle = Lichess().get("Ab123")
        self.assertEqual(puzzle.first_move, "b5c6")
        self.assertTrue(puzzle.accepts("Bxc6"))
        self.assertFalse(puzzle.accepts("a6"))
        fetch.return_value = BytesIO(json.dumps({
            "game": {"pgn": "not a game"},
            "puzzle": {"id": "Ab123", "rating": 1500, "solution": ["e2e4"]},
        }).encode())
        with self.assertRaises(LichessError):
            Lichess().get("Ab123")

    def test_first_move_notation_castling_promotion_captures_and_alternative_mate(self):
        cases = (
            (chess.STARTING_FEN, "e2e4", ("e4", "`e4`"), ("d4", "e4 e5", "--", "??", "")),
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", ("O-O", "0-0"), ("O-O-O",)),
            ("8/4P2k/8/8/8/8/8/K7 w - - 0 1", "e7e8q", ("e8=Q",), ("e8=N", "e8")),
            ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 2", "e5d6", ("exd6",), ("e6",)),
            ("7k/8/8/8/8/8/8/KN3N2 w - - 0 1", "b1d2", ("Nbd2",), ("Nd2", "Nfd2")),
            ("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1", "g6g7", ("Qg7#", "Qh6#"), ("Qf6+",)),
        )
        for fen, first, correct, incorrect in cases:
            puzzle = Puzzle("Ab123", 1500, fen, first)
            for move in correct:
                with self.subTest(fen=fen, move=move):
                    self.assertTrue(puzzle.accepts(move))
            for move in incorrect:
                with self.subTest(fen=fen, move=move):
                    self.assertFalse(puzzle.accepts(move))

    def test_requests_are_serialized_between_threads(self):
        client = Lichess()
        active = 0
        maximum = 0
        counter_lock = threading.Lock()

        class SlowResponse(BytesIO):
            def __enter__(self):
                nonlocal active, maximum
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.005)
                return self

            def __exit__(self, *args):
                nonlocal active
                with counter_lock:
                    active -= 1
                return super().__exit__(*args)

        def fetch(*args, **kwargs):
            return SlowResponse(response().getvalue())

        with patch("daily_chess.lichess.urlopen", side_effect=fetch), ThreadPoolExecutor(4) as pool:
            results = list(pool.map(client.random, ["easy"] * 8))
        self.assertEqual(results, [Puzzle("Ab123", 1500, chess.STARTING_FEN, "e2e4")] * 8)
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
