"""Historical-only CLI. This module never imports or constructs a broker API."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from .cache import Cache
from .data import load_history
from .engine import SimulationConfig, run
from .models import DataError, dec, utc
from .reporting import make_report, print_report, save_report
from .strategy import HmaStrategy
from .timeframes import MINUTES, floor_time


def number(value):
    try:
        return dec(value)
    except ValueError:
        raise argparse.ArgumentTypeError("A finite numeric value is required") from None


SIMULATION_OPTIONS = frozenset(
    (
        "--data-source",
        "--lookback-days",
        "--initial-balance",
        "--start",
        "--end",
        "--cache-dir",
        "--output-dir",
        "--refresh-data",
        "--allow-data-gaps",
        "--leverage",
        "--commission-per-order",
        "--slippage-pips",
        "--financing-usd-per-unit-day",
    )
)


def add_arguments(parser):
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Historical simulation only; no Capital.com access",
    )
    parser.add_argument(
        "--data-source", choices=("dukascopy", "yfinance"), default="dukascopy"
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--initial-balance", type=number, default=dec(1000))
    parser.add_argument(
        "--start",
        help="Optional inclusive UTC timestamp/date; otherwise end minus lookback days",
    )
    parser.add_argument(
        "--end",
        help="Exclusive UTC timestamp/date; default end of the last complete UTC calendar day",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/backtests"))
    parser.add_argument("--output-dir", type=Path, default=Path("backtests"))
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument(
        "--allow-data-gaps",
        action="store_true",
        help="Explicitly accept detected missing active candles; never fills them",
    )
    parser.add_argument(
        "--leverage",
        type=number,
        default=dec(1),
        help="Explicit simulated collateral ratio; default unlevered 1:1",
    )
    parser.add_argument(
        "--commission-per-order",
        type=number,
        default=None,
        help="Optional assumed USD commission on each entry/exit",
    )
    parser.add_argument(
        "--slippage-pips",
        type=number,
        default=None,
        help="Optional assumed adverse execution slippage per leg",
    )
    parser.add_argument(
        "--financing-usd-per-unit-day",
        type=number,
        default=None,
        help="Optional linear daily financing debit assumption",
    )


def date(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if len(value) != 10:
                raise ValueError()
            parsed = parsed.replace(tzinfo=timezone.utc)
        return utc(parsed)
    except (ValueError, AttributeError):
        raise DataError("Use an ISO UTC/offset timestamp, or YYYY-MM-DD") from None


def execute(args, parser, *, argv=None, now=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    forbidden = {
        "--REAL",
        "--real",
        "--account",
        "--read-only",
        "--cycles",
        "--schedule-time-frame",
        "--days",
        "--max-margin",
        "--max-exposure",
        "--max-charges",
        "--max-loss",
        "--max-orders",
        "--stop-distance",
        "--fee-profile",
    }
    if (
        getattr(args, "real", False)
        or getattr(args, "account", None)
        or any(arg.split("=", 1)[0] in forbidden for arg in argv)
    ):
        parser.error(
            "Backtesting cannot be combined with broker account/risk, scheduling or live-read-only options"
        )
    if args.lookback_days <= 0:
        parser.error("--lookback-days must be positive")
    try:
        strategy = HmaStrategy(
            16 if args.hma_period is None else args.hma_period,
            "BUY" if args.direction is None else args.direction,
        )
        timeframe = args.time_frame or "MINUTE_30"
        now = utc(now or datetime.now(timezone.utc))
        end = date(args.end) if args.end else floor_time(now, "DAY")
        start = (
            date(args.start) if args.start else end - timedelta(days=args.lookback_days)
        )
        config = SimulationConfig(
            args.initial_balance,
            args.size,
            args.leverage,
            args.commission_per_order,
            args.slippage_pips,
            args.financing_usd_per_unit_day,
        )
        dataset = load_history(
            args.data_source,
            args.symbol,
            timeframe,
            start,
            end,
            strategy.warmup,
            Cache(args.cache_dir, refresh=args.refresh_data),
            allow_gaps=args.allow_data_gaps,
            now=now,
        )
        result = run(dataset.candles, strategy, config, start=start, end=end)
        report = make_report(
            result, dataset, period=strategy.period, direction=strategy.direction
        )
        path = save_report(report, args.output_dir)
        print_report(report, path)
        return 0
    except (DataError, ValueError, OSError, OverflowError) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    add_arguments(parser)
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--size", type=number, default=dec(100))
    parser.add_argument(
        "--time-frame",
        "--time_frame",
        dest="time_frame",
        choices=MINUTES,
        default="MINUTE_30",
    )
    parser.add_argument(
        "--hma-period", "--hma_period", dest="hma_period", type=int, default=16
    )
    parser.add_argument("--direction", choices=("BUY", "SELL"), default="BUY")
    args = parser.parse_args(argv)
    return execute(args, parser, argv=argv)
