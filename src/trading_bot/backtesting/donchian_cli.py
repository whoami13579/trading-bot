"""One cached Donchian simulation using the research study's execution engine."""

import argparse
from pathlib import Path
import shlex
import sys

from trading_bot.strategies.donchian import DonchianStrategy
from .cli import date
from .models import DataError
from .reporting import money, pct, print_report_table
from .study_data import START, END, FETCH_START, read_snapshot
from .study_engine import COSTS, prepare_frames, simulate
from .timeframes import floor_time


def utc_date(value):
    try:
        return date(value)
    except DataError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def data_help(snapshot):
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
        candles, metadata = read_snapshot(args.snapshot)
        frames, _ = prepare_frames(candles, args.start, args.end)
        hourly = frames["HOUR"]
        strategy = DonchianStrategy()
        warmup = sum(c.end <= args.start for c in hourly)
        required = strategy.warmup - 1
        if warmup < required:
            raise DataError(
                f"Insufficient 1H warm-up: need {required} completed bars before "
                f"{args.start.isoformat()}, found {warmup}"
            )
        scored = [c for c in hourly if args.start <= c.timestamp and c.end <= args.end]
        if not scored:
            raise DataError("Insufficient data: no completed 1H candles in the requested range")
        stats, _, _ = simulate(hourly, strategy, COSTS[0], args.start, args.end)
    except (OSError, ValueError, KeyError, TypeError, EOFError, OverflowError) as exc:
        print(f"Donchian backtest failed for {args.snapshot}: {exc}", file=sys.stderr)
        print(data_help(args.snapshot), file=sys.stderr)
        return 1

    quote_type = "historical bid/ask" if candles[0].bid else "midpoint; assumed 1 pip spread"
    profit = stats["profit_factor"]
    factor = f"{profit:.4f}" if profit is not None else stats["profit_factor_status"]
    lines = [
        ("Strategy", f"Donchian (long / short), {strategy.entry}/{strategy.exit} bars"),
        ("Symbol", "EURUSD"),
        ("Size", "100 EUR"),
        ("Environment", "DEMO (simulated only)"),
        ("Data source", f"{metadata.get('source', candles[0].source)} (cached {quote_type})"),
        ("Timeframe", "HOUR (1H)"),
        ("Historical days", str((args.end - args.start).total_seconds() / 86400)),
        ("Period", f"{args.start.isoformat()} to {args.end.isoformat()} (UTC, end exclusive)"),
        ("Candles", str(len(scored))),
        ("Warm-up candles", str(min(warmup, strategy.warmup))),
        ("Last candle end", scored[-1].end.isoformat()),
        ("Initial balance", money(stats["initial_balance"])),
        ("Final balance", money(stats["final_balance"])),
        ("Final equity", money(stats["final_equity"])),
        ("Gross P&L", money(stats["midpoint_gross_pnl"])),
        ("Net P&L", money(stats["net_pnl"])),
        ("Total return", pct(stats["total_return_pct"])),
        ("Total closed trades", str(stats["total_trades"])),
        ("Winning trades", str(stats["winning_trades"])),
        ("Losing trades", str(stats["losing_trades"])),
        ("Win rate", pct(stats["win_rate_pct"])),
        ("Profit factor", factor),
        ("Max drawdown", money(stats["max_drawdown"]) + " / " + pct(stats["max_drawdown_pct"])),
        ("Realized cash P&L", money(stats["realized_pnl"])),
        ("Unrealized P&L", money(stats["unrealized_pnl"])),
        ("Modeled costs", money(stats["modeled_costs"])),
        ("Remaining positions", str(stats["remaining_positions"])),
        ("Remaining base units", str(stats["remaining_base_units"])),
        ("Remaining USD notional", money(stats["remaining_notional_usd"])),
        ("Available funds", money(stats["available_funds"])),
        ("Rejected entries", str(stats["rejected_entries"])),
    ]
    print_report_table(
        lines,
        "Gross P&L uses midpoint prices. All orders are simulated.\n"
        "Base costs: spread + 0.1 pip/leg slippage; commission assumed zero; historical financing omitted.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
