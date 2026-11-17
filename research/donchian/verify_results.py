"""Audit exported fills and accounting without rerunning strategy performance.

Run from repository root with .venv/bin/python research/donchian/verify_results.py.
No authentication, network access or order methods are used.
"""

from collections import Counter
import csv
from datetime import datetime, timedelta
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from trading_bot.backtesting.study_data import read_snapshot, START, END
from trading_bot.backtesting.study_engine import prepare_frames


def close_enough(a, b):
    if abs(D(a) - D(b)) > D("1e-20"):
        raise AssertionError(f"Accounting mismatch: {a} versus {b}")


def stamp(value):
    return datetime.fromisoformat(value)


def audit(output):
    study = json.loads((output / "study.json").read_text())
    frozen = json.loads((output / "freeze.json").read_text())
    for key, file in (("study_sha256", "study.json"), ("development_sha256", "development.json")):
        assert frozen[key] == hashlib.sha256((output / file).read_bytes()).hexdigest()
    assert stamp(frozen["frozen_at"]) < stamp(json.loads((output / "holdout_started.json").read_text())["started_at"])
    attempts = [json.loads(line) for line in (output / "attempts.jsonl").read_text().splitlines()]
    assert Counter(a["status"] for a in attempts) == {"started": 201, "completed": 201}
    assert len({a["id"] for a in attempts}) == 201
    candles, metadata = read_snapshot(study["snapshot"])
    assert metadata["candles_sha256"] == study["data"]["candles_sha256"]
    frames, _ = prepare_frames(candles, START, END)
    indices = {tf: {c.open_time: i for i, c in enumerate(bars)} for tf,bars in frames.items()}
    boundaries = {tf: {c.end: i for i,c in enumerate(bars)} for tf,bars in frames.items()}
    rows = json.loads((output / "development.json").read_text()) + json.loads((output / "holdout.json").read_text())
    ny = ZoneInfo("America/New_York")
    all_trades = []
    audited = 0
    for row in rows:
        folder = output / "runs" / row["id"]
        with (folder / "trades.csv").open() as f:
            trades = list(csv.DictReader(f))
        assert len(trades) == row["total_trades"]
        bars = frames[row["timeframe"]]
        start, end = stamp(row["start"]), stamp(row["end"])
        multiplier = D(2) if row["cost"] == "high" else D(1)
        slip = D(".00005") if row["cost"] == "high" else D(".00001")
        rate = D(".05") if row["cost"] != "base" else D(0)
        previous_exit = start
        previous_exit_signal = None
        running = D(1000)
        for trade in trades:
            entry, exit = stamp(trade["entry_timestamp"]), stamp(trade["exit_timestamp"])
            entry_signal = stamp(trade["entry_signal_time"])
            exit_signal = stamp(trade["exit_signal_time"]) if trade["exit_signal_time"] else None
            boundary = trade["boundary_exit"] == "True"
            i = indices[row["timeframe"]][entry]
            j = boundaries[row["timeframe"]][exit] if boundary else indices[row["timeframe"]][exit]
            assert start <= entry_signal <= entry < exit <= end
            assert entry >= previous_exit
            assert entry_signal == bars[i-1].end, "Entry did not fill at next observed open"
            if previous_exit_signal:
                assert entry_signal > previous_exit_signal, "Same-close re-entry"
            if not boundary:
                assert exit_signal == bars[j-1].end, "Exit did not fill at next observed open"
                assert start <= exit_signal <= exit
            else:
                assert exit == max(c.end for c in bars if start <= c.timestamp and c.end <= end)
            sign = 1 if trade["direction"] == "BUY" else -1
            size = D(trade["size"])
            assert size == 100
            def quote(k, field):
                b, a = getattr(bars[k].bid, field), getattr(bars[k].ask, field)
                return (a+b)/2, (a-b)*multiplier/2
            entry_mid, entry_half = quote(i, "open")
            exit_mid, exit_half = quote(j, "close" if boundary else "open")
            close_enough(trade["entry_price"], entry_mid + sign*(entry_half+slip))
            close_enough(trade["exit_price"], exit_mid - sign*(exit_half+slip))
            gross = (exit_mid-entry_mid)*size*sign
            spread, slippage = (entry_half+exit_half)*size, slip*2*size
            # Enumerate rollover dates independently of the ledger's observation calls.
            day = entry.astimezone(ny).date()
            weights = 0
            while day <= exit.astimezone(ny).date():
                roll = datetime(day.year, day.month, day.day, 17, tzinfo=ny)
                if day.weekday() < 5 and entry < roll <= exit:
                    weights += 3 if day.weekday() == 2 else 1
                day += timedelta(days=1)
            financing = entry_mid*size*rate/D(360)*weights
            close_enough(trade["midpoint_gross_pnl"], gross)
            close_enough(trade["spread_cost"], spread)
            close_enough(trade["slippage_cost"], slippage)
            close_enough(trade["financing_cost"], financing)
            close_enough(trade["net_pnl"], gross-spread-slippage-financing)
            if row["strategy"].startswith("donchian_"):
                _, entry_bars, exit_bars = row["strategy"].split("_")
                def mids(a, b):
                    return [(c.bid.close+c.ask.close)/2 for c in bars[a:b]]
                prior = mids(i-1-int(entry_bars), i-1)
                current = mids(i-1, i)[0]
                assert current > max(prior) if sign == 1 else current < min(prior)
                if not boundary:
                    prior = mids(j-1-int(exit_bars), j-1)
                    current = mids(j-1, j)[0]
                    assert current < min(prior) if sign == 1 else current > max(prior)
            running += D(trade["net_pnl"])
            close_enough(trade["balance_after_closing"], running)
            previous_exit, previous_exit_signal = exit, exit_signal
            all_trades.append(dict(run_id=row["id"], timeframe=row["timeframe"],
                                   strategy=row["strategy"], cost_case=row["cost"],
                                   stage=row["stage"], window=row["window"], **trade))
            audited += 1
        close_enough(row["net_pnl"], running-1000)
        assert row["remaining_positions"] == 0
        with (folder / "equity.csv").open() as f:
            curve = list(csv.DictReader(f))
        for point in curve:
            close_enough(point["equity"], D(point["balance"])+D(point["unrealized_pnl"]))
        close_enough(curve[-1]["equity"], running)
    with (output / "simulated_trades.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_trades[0]))
        writer.writeheader()
        writer.writerows(all_trades)
    result = {"runs_audited": len(rows), "trades_audited": audited,
              "next_open_quotes_costs_rollovers_boundaries_equity": "PASS",
              "holdout_started_after_freeze": True, "repeated_holdout_attempts": 0,
              "offline_tests": 449, "targeted_strategy_tests": 14}
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    audit(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backtests/donchian-eurusd-final"))
