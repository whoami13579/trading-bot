"""Explicit, cached, read-only data acquisition for the Donchian study."""

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .cache import Cache
from .models import Candle, DataError, OHLC, json_value
from .timeframes import normalize

START = datetime(2023, 11, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 1, tzinfo=timezone.utc)
FETCH_START = START - timedelta(days=20)


def capital_candles(payload):
    """Capital snapshotTimeUTC is an interval start; never use local snapshotTime."""
    rows = []
    for row in payload["prices"]:
        stamp = datetime.fromisoformat(row["snapshotTimeUTC"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        sides = [OHLC(*(row[k][side] for k in
                        ("openPrice", "highPrice", "lowPrice", "closePrice")))
                 for side in ("bid", "ask")]
        rows.append(Candle(stamp, sides[1], "EURUSD", "MINUTE_30",
                           "capital_demo", *sides))
    return rows


def demo_client():
    # Imported only by the explicit acquisition command, never by simulation.
    from trading_bot.api.client import TradingApi
    from trading_bot.config.trading_config import DEMO_URL

    class HistoryOnlyClient(TradingApi):
        def _request(self, method, url, **kwargs):
            parsed = urlsplit(url)
            allowed = ((method.lower() == "post" and parsed.path == "/api/v1/session")
                       or (method.lower() == "get" and parsed.path in
                           {"/api/v1/ping", "/api/v1/prices/EURUSD"}))
            if not allowed or f"{parsed.scheme}://{parsed.netloc}" != DEMO_URL:
                raise PermissionError("Study permits DEMO login and history reads only")
            return super()._request(method, url, **kwargs)

    return HistoryOnlyClient(environment="demo", auto_authenticate=False)


def acquire_capital(cache, progress=print):
    api = None
    cursor = FETCH_START
    candles = []
    while cursor < END:
        until = min(cursor + timedelta(days=14), END)
        key = f"donchian-capital-v1:{cursor.isoformat()}:{until.isoformat()}"
        payload = cache.get(key)
        if payload is None:
            if api is None:
                api = demo_client()
                if api.createNewSession() is None:
                    raise DataError("Capital DEMO authentication failed")
            payload, _ = api.getHistoricalPrices(
                "EURUSD", "MINUTE_30", 1000,
                from_=cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                to=until.strftime("%Y-%m-%dT%H:%M:%S"))
            # Empty history cannot establish access to requested historical dates.
            if not payload.get("prices"):
                raise DataError("Capital DEMO returned no prices in requested chunk")
            capital_candles(payload)
            cache.put(key, {"prices": payload["prices"]})
        candles.extend(c for c in capital_candles(payload) if cursor <= c.timestamp < until)
        progress(f"Capital DEMO history through {until.date()}")
        cursor = until
    return normalize(candles, completed_before=END)


def snapshot_help(snapshot):
    """Actionable preparation instructions for a missing or invalid snapshot."""
    import shlex

    output = shlex.quote(str(snapshot))
    command = "uv run -m trading_bot.backtesting.study_data"
    return (
        "Expected an integrity-checked EURUSD MINUTE_30 gzip JSON study snapshot "
        f"covering {FETCH_START.date()} (warm-up) through {END.date()} "
        "(scheduled market closures allowed).\n"
        f"Prepare it with: {command} --source capital-demo --snapshot {output}\n"
        "If DEMO history is unavailable, explicitly select the alternate source:\n"
        f"  {command} --source dukascopy --snapshot {output}\n"
        "For an existing invalid snapshot, choose a new --snapshot path or move "
        "the invalid file aside before preparing it."
    )


def write_snapshot(path, candles, metadata):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [c.as_dict() for c in candles]
    encoded = json.dumps(rows, default=json_value, sort_keys=True).encode()
    metadata = dict(metadata, candles_sha256=hashlib.sha256(encoded).hexdigest(),
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    first=candles[0].timestamp.isoformat(),
                    last_end=candles[-1].end.isoformat(), count=len(candles))
    with gzip.open(path, "wt") as f:
        json.dump({"metadata": metadata, "candles": rows}, f, default=json_value)
    return metadata


def read_snapshot(path):
    with gzip.open(path, "rt") as f:
        data = json.load(f)
    encoded = json.dumps(data["candles"], sort_keys=True).encode()
    if hashlib.sha256(encoded).hexdigest() != data["metadata"]["candles_sha256"]:
        raise DataError("Study snapshot fingerprint mismatch")
    raw = tuple(Candle.from_dict(c) for c in data["candles"])
    normalized = normalize(raw, completed_before=END)
    if len(raw) != len(normalized):
        raise DataError("Snapshot contains duplicate or incomplete candles")
    if not normalized or any(c.symbol != "EURUSD" or c.timeframe != "MINUTE_30"
                             for c in normalized):
        raise DataError("Study requires EURUSD 30m source candles")
    if normalized[0].timestamp > FETCH_START or normalized[-1].end < END - timedelta(days=3):
        raise DataError("Insufficient target common history")
    return normalized, data["metadata"]


def acquire_dukascopy(cache):
    """Four independent daily packets at a time; cache and decoder are shared code.

    Bounded concurrency avoids retaining three years of minute objects in memory.
    Empty successful paired packets remain missing data in the subsequent audit.
    HTTP failures/challenges abort; there is no bypass or silent partial success.
    """
    from concurrent.futures import ThreadPoolExecutor
    from .providers.dukascopy import DukascopyProvider, decode
    from .timeframes import resample

    def day(stamp):
        provider = DukascopyProvider(cache)
        sides = [dict(decode(provider._bucket(side, "minute", stamp, False), "MINUTE"))
                 for side in ("BID", "ASK")]
        if set(sides[0]) != set(sides[1]):
            raise DataError("Dukascopy bid/ask timestamps differ")
        minute = [Candle(t, sides[1][t], "EURUSD", "MINUTE", "dukascopy",
                         sides[0][t], sides[1][t]) for t in sorted(sides[0])]
        return resample(minute, "MINUTE_30", completed_before=END)

    days = []
    cursor = FETCH_START
    while cursor < END:
        if cursor.weekday() != 5:  # Saturday has no modeled FX session.
            days.append(cursor)
        cursor += timedelta(days=1)
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, result in enumerate(pool.map(day, days), 1):
            rows.extend(result)
            if i % 25 == 0:
                print(f"Dukascopy {i}/{len(days)} daily bid/ask packets", flush=True)
    return normalize(rows, completed_before=END)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("capital-demo", "dukascopy"), default="capital-demo")
    parser.add_argument("--snapshot", type=Path, default=Path(".cache/donchian/eurusd-30m.json.gz"))
    args = parser.parse_args()
    if args.snapshot.exists():
        parser.error("Snapshot exists; use it unchanged or choose another output path")
    cache = Cache(".cache/backtests")
    try:
        if args.source == "capital-demo":
            candles = acquire_capital(cache)
            url = "https://open-api.capital.com/"
        else:
            from .providers.dukascopy import BASE
            candles = acquire_dukascopy(cache)
            url = BASE
        metadata = write_snapshot(args.snapshot, candles,
                                  {"source": args.source, "interface": url,
                                   "historical_financing": "unavailable"})
        print(json.dumps(metadata, indent=2))
    except (Exception, SystemExit) as exc:
        # Never print response bodies, credential-bearing exceptions or account data.
        print(json.dumps({"source": args.source, "failed": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, DataError) else "history unavailable"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
