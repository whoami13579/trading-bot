"""Atomic public market-data snapshots with versioned, integrity-checked keys."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from .models import DataError, json_value


def safe_directory(path):
    path = Path(path).expanduser().resolve()
    if any(p in {".demo-integration", ".live-integration"} for p in path.parts):
        raise ValueError("Backtests cannot write broker integration state")
    return path


class Cache:
    def __init__(self, root, *, refresh=False):
        self.root = safe_directory(root)
        self.refresh = refresh

    def path(self, key):
        digest = hashlib.sha256(("v1:" + key).encode()).hexdigest()
        return self.root / (digest + ".json")

    def get(self, key):
        path = self.path(key)
        if self.refresh or not path.exists():
            return None
        try:
            outer = json.loads(path.read_text())
            payload = outer["payload"]
            if (
                outer["key"] != key
                or hashlib.sha256(payload.encode()).hexdigest() != outer["sha256"]
            ):
                raise ValueError()
            return json.loads(payload)
        except (ValueError, KeyError, TypeError):
            raise DataError(
                "Corrupt market-data cache; use --refresh-data to replace it"
            ) from None

    def put(self, key, data):
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, default=json_value, sort_keys=True, allow_nan=False)
        outer = {
            "key": key,
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as file:
            json.dump(outer, file)
            name = file.name
        os.replace(name, self.path(key))
