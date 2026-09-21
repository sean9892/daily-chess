"""Small anonymous client for Lichess's public puzzle API."""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from io import StringIO
import json
import math
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import chess
import chess.pgn


_ID = re.compile(r"[A-Za-z0-9]{5}")
_DIFFICULTIES = {"easy": "easier", "medium": "normal", "hard": "harder"}


@dataclass(frozen=True)
class Puzzle:
    id: str
    rating: int
    fen: str | None = None
    first_move: str | None = None

    @property
    def to_move(self) -> str:
        if not self.fen:
            raise LichessError("Puzzle position is unavailable.")
        return "White" if chess.Board(self.fen).turn else "Black"

    def accepts(self, answer: str) -> bool:
        if not self.fen or not self.first_move:
            raise LichessError("Puzzle answer is unavailable.")
        board = chess.Board(self.fen)
        try:
            move = board.parse_san(answer.strip().strip("`"))
        except ValueError:
            return False
        if not move:
            return False
        board.push(move)
        return move.uci() == self.first_move or board.is_checkmate()


class LichessError(RuntimeError):
    pass


def puzzle_id(value: str) -> str:
    """Accept a case-sensitive ID, training link, or Slack-formatted link."""
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].split("|", 1)[0]
        if _ID.fullmatch(value):
            return value
        try:
            url = urlsplit(value)
            match = re.fullmatch(
                r"/(?:[a-z]{2}/)?training/(?:[A-Za-z0-9_-]+/)?([A-Za-z0-9]{5})/?",
                url.path,
            )
            if (url.scheme in ("http", "https")
                    and url.netloc.lower() in ("lichess.org", "www.lichess.org") and match):
                return match[1]
        except ValueError:
            pass
    raise ValueError("Use a five-character Lichess puzzle ID or a lichess.org/training link.")


class Lichess:
    def __init__(self, timeout: float = 15):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Lichess timeout must be a positive number.")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._retry_at = 0.0

    def get(self, value: str) -> Puzzle:
        identifier = puzzle_id(value)
        puzzle = self._request(f"/api/puzzle/{identifier}")
        if puzzle.id != identifier:
            raise LichessError("Lichess returned a different puzzle than requested.")
        return puzzle

    def random(self, difficulty: str) -> Puzzle:
        if difficulty not in _DIFFICULTIES:
            raise ValueError("Difficulty must be easy, medium, or hard.")
        # Anonymous requests avoid authenticated sessions repeating an unsolved puzzle.
        return self._request(f"/api/puzzle/next?difficulty={_DIFFICULTIES[difficulty]}")

    def _request(self, path: str) -> Puzzle:
        # Lichess requires one request at a time, including across scheduler/Slack threads.
        with self._lock:
            remaining = self._retry_at - time.monotonic()
            if remaining > 0:
                raise LichessError(f"Lichess is rate limited; retry in {math.ceil(remaining)} seconds.")
            request = Request("https://lichess.org" + path, headers={
                "Accept": "application/json", "User-Agent": "daily-chess/0.1",
            })
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                data = payload["puzzle"]
                identifier, rating = data["id"], data["rating"]
                if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                    raise ValueError("Invalid puzzle ID")
                if type(rating) is not int or rating < 0:
                    raise ValueError("Invalid puzzle rating")
                if "fen" in data:
                    if not isinstance(data["fen"], str):
                        raise ValueError("Invalid position")
                    board = chess.Board(data["fen"])
                else:
                    game = chess.pgn.read_game(StringIO(payload["game"]["pgn"]))
                    if game is None or game.errors or not game.variations:
                        raise ValueError("Invalid game")
                    board = game.end().board()
                solution = data["solution"]
                if (not board.is_valid() or not isinstance(solution, list)
                        or not solution or not isinstance(solution[0], str)):
                    raise ValueError("Invalid solution")
                move = board.parse_uci(solution[0])
                if not move:
                    raise ValueError("Invalid first move")
                return Puzzle(identifier, rating, board.fen(), move.uci())
            except HTTPError as error:
                error.close()
                if error.code == 404:
                    raise ValueError("That Lichess puzzle does not exist.") from None
                if error.code == 429:
                    retry_after = error.headers.get("Retry-After", "60")
                    try:
                        seconds = float(retry_after)
                    except ValueError:
                        try:
                            seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                        except (TypeError, ValueError, OverflowError):
                            seconds = 60
                    self._retry_at = time.monotonic() + max(60, seconds if math.isfinite(seconds) else 60)
                    raise LichessError("Lichess is rate limited; please try again later.") from None
                raise LichessError(f"Lichess returned HTTP {error.code}; please try again later.") from error
            except (URLError, OSError) as error:
                raise LichessError("Could not reach Lichess; please try again later.") from error
            except (KeyError, TypeError, ValueError) as error:
                raise LichessError("Lichess returned an invalid puzzle response.") from error
