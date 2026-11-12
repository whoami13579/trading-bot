"""Durable order intent and lock handling (preserved from the legacy tool)."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.risk.live_risk import require

EPIC = "EURUSD"

class TradeJournal:
    """Durable intent before mutations; unresolved runs block subsequent trading runs."""

    TERMINAL = {"closed", "rejected"}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "trade.json"
        self.data = None
        self.lock = None
        self.blocked_prior = False

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.lock = os.fdopen(fd, "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                previous = json.loads(self.path.read_text())
                terminal = (previous.get("stage") == "rejected" or
                            previous.get("stage") == "closed" and previous.get("closure_verified") is True)
                self.blocked_prior = not terminal
                require(terminal, "unresolved_prior_trade_requires_reconciliation")
        except BaseException:
            self.lock.close()
            raise
        return self

    def save(self, **changes):
        if self.data is None:
            require(changes.get("stage") == "prepared", "journal_not_reserved")
            if self.path.exists():
                os.replace(self.path, self.directory / f"completed-{uuid.uuid4().hex}.json")
            self.data = {"run_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                         "base_url": DEMO_URL, "epic": EPIC, "direction": "BUY"}
        self.data.update(changes)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, prefix="journal-", delete=False) as stream:
            pending = Path(stream.name)
            try:
                json.dump(self.data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                pending.unlink(missing_ok=True)
                raise
        os.replace(pending, self.path)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None and self.data is not None and self.data.get("stage") not in self.TERMINAL:
                self.save(previous_stage=self.data["stage"], stage="uncertain", error_type=exc_type.__name__)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
