"""One cached Donchian simulation on the shared backtesting pipeline.

Equivalent to ``trading-bot-backtest --strategy donchian`` with the snapshot,
research execution model and base costs, but prints only (no report files),
exactly as this command always has.
"""

import argparse
from pathlib import Path
import sys

from .cli import date
from .models import DataError
from .reporting import print_report
from .runner import BacktestRequest, execute, plan
from .study_data import START, END, snapshot_help
from .timeframes import floor_time


def utc_date(value):
    try:
        return date(value)
    except DataError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run one simulated EURUSD Donchian backtest: fixed size 100 EUR, "
            "DEMO, primary 1H, 20/10 bars, USD 1,000 initial cash, unlevered. "
            "Base costs: observed bid/ask spread (1 pip full spread for midpoint "
            "data), 0.1 pip slippage per leg, zero assumed commission; unavailable "
            "historical financing omitted. Uses the cached study snapshot; "
            "no authentication, downloads or broker orders."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--backtest", action="store_true", required=True,
        help="Run exactly one historical simulation",
    )
    parser.add_argument(
        "--snapshot", type=Path, default=Path(".cache/donchian/eurusd-30m.json.gz"),
        help="Path to the normalized gzip JSON study snapshot, relative to cwd",
    )
    parser.add_argument(
        "--start", type=utc_date, default=START.isoformat(),
        help="Inclusive UTC date/timestamp within the study interval",
    )
    parser.add_argument(
        "--end", type=utc_date, default=END.isoformat(),
        help="Exclusive UTC date/timestamp within the study interval",
    )
    args = parser.parse_args(argv)
    if not START <= args.start < args.end <= END:
        parser.error(
            f"Require {START.date()} <= --start < --end <= {END.date()}"
        )
    if any(floor_time(t, "HOUR") != t for t in (args.start, args.end)):
        parser.error("--start and --end must align to completed UTC hours")

    try:
        report = execute(plan(BacktestRequest(
            strategy="donchian", time_frame="HOUR", data_source="snapshot",
            execution="research", cost_case="base", start=args.start, end=args.end,
            snapshot=args.snapshot)))
    except (OSError, ValueError, KeyError, TypeError, EOFError, OverflowError) as exc:
        print(f"Donchian backtest failed for {args.snapshot}: {exc}", file=sys.stderr)
        print(snapshot_help(args.snapshot), file=sys.stderr)
        return 1
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
