"""Unified historical-only backtest CLI. Never imports or constructs a broker API.

    uv run tb-backtest --list-strategies
    uv run tb-backtest --strategy hma --symbol EURUSD --time-frame HOUR --size 100
    uv run tb-backtest --strategy donchian --symbol EURUSD --time-frame HOUR --size 100
    uv run tb-backtest --strategy grid --grid-lower 1.15 --grid-upper 1.19 \
        --start 2025-09-01 --end 2025-10-01
    uv run tb-backtest --strategy grid --grid-lookback 100 \
        --start 2025-09-01 --end 2025-10-01    # automatic bounds

trading-bot-backtest is the same command under its original name.

Every strategy shares the data sources, engine, cost models and report. Omitted
options use the selected strategy's preserved defaults (see --help).
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from .adapters import ADAPTERS
from .evaluation import HoldoutReuseError
from .execution import EXECUTION_MODELS
from .models import DataError, dec, utc
from .reporting import print_report, save_report
from .runner import (
    BacktestRequest,
    DATA_SOURCES,
    DEFAULT_SNAPSHOT,
    PlanError,
    execute as execute_plan,
    plan as make_plan,
)
from .study_engine import COSTS
from .timeframes import MINUTES
from trading_bot.strategies import registry


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

FORBIDDEN = frozenset(
    {
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
)


class _Recorder:
    """Forwards add_argument calls and keeps the returned (public) Action objects.

    ``hidden`` adds the options with help suppressed: they parse exactly as before
    but are not advertised (HMA's simulation options under tb-trade).
    """

    def __init__(self, target, actions, *, hidden=False):
        self.target, self.actions, self.hidden = target, actions, hidden

    def add_argument(self, *args, **kwargs):
        if self.hidden:
            kwargs["help"] = argparse.SUPPRESS
        action = self.target.add_argument(*args, **kwargs)
        self.actions.append(action)
        return action

    def add_argument_group(self, *args, **kwargs):
        return _Recorder(self.target.add_argument_group(*args, **kwargs), self.actions,
                         hidden=self.hidden)


def _common_arguments(parser, *, sources, source_default, lookback_default):
    parser.add_argument("--data-source", choices=sources, default=source_default)
    parser.add_argument("--lookback-days", type=int, default=lookback_default)
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


def add_arguments(parser, *, hidden=False):
    """Simulation options added to the HMA strategy command (hma.py --backtest).

    ``hidden`` keeps them parseable but out of --help (tb-trade refuses them).
    """
    parser = _Recorder(parser, [], hidden=hidden)
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Historical simulation only; no Capital.com access",
    )
    _common_arguments(
        parser,
        sources=("dukascopy", "yfinance"),
        source_default="dukascopy",
        lookback_default=30,
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


def reject_broker_options(args, parser, argv):
    if (
        getattr(args, "real", False)
        or getattr(args, "account", None)
        or any(arg.split("=", 1)[0] in FORBIDDEN for arg in argv)
    ):
        parser.error(
            "Backtesting cannot be combined with broker account/risk, scheduling or "
            "live-read-only options; DEMO/LIVE trading uses tb-trade"
        )


def _run(plan, args, *, save=True):
    report = execute_plan(plan)
    path = save_report(report, args.output_dir) if save else None
    print_report(report, path)
    return 0


def execute(args, parser, *, argv=None, now=None):
    """HMA continuous backtest for ``hma.py --backtest`` (its defaults are unchanged)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    reject_broker_options(args, parser, argv)
    if args.lookback_days <= 0:
        parser.error("--lookback-days must be positive")
    try:
        plan = make_plan(
            BacktestRequest(
                strategy="hma",
                parameters={"hma_period": args.hma_period, "direction": args.direction},
                symbol=args.symbol,
                time_frame=args.time_frame or "MINUTE_30",
                data_source=args.data_source,
                execution="continuous",
                start=date(args.start) if args.start else None,
                end=date(args.end) if args.end else None,
                lookback_days=args.lookback_days,
                initial_balance=args.initial_balance,
                size=args.size,
                leverage=args.leverage,
                commission_per_order=args.commission_per_order,
                slippage_pips=args.slippage_pips,
                financing_usd_per_unit_day=args.financing_usd_per_unit_day,
                cache_dir=args.cache_dir,
                refresh_data=args.refresh_data,
                allow_data_gaps=args.allow_data_gaps,
            ),
            now=now,
        )
        return _run(plan, args)
    except (DataError, ValueError, OSError, OverflowError) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1


def build_parser():
    strategies = ", ".join(
        f"{a.key}: {a.time_frame}, {a.data_source} data, {a.execution} execution"
        for a in ADAPTERS.values()
    )
    actions = []
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"Strategy defaults — {strategies}.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    root, parser = parser, _Recorder(parser, actions)
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Accepted for compatibility; this command only runs historical simulations",
    )
    parser.add_argument("--strategy", choices=tuple(ADAPTERS), default="hma")
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="List backtestable strategies and their defaults, then exit",
    )
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument(
        "--size",
        type=number,
        default=None,
        help="EUR base units (default 100); grid strategies use --level-size",
    )
    parser.add_argument(
        "--time-frame",
        "--time_frame",
        dest="time_frame",
        choices=MINUTES,
        default=None,
        help="Completed UTC candle timeframe (default: strategy default)",
    )
    _common_arguments(
        parser, sources=DATA_SOURCES, source_default=None, lookback_default=None
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help="Verified study snapshot used by --data-source snapshot",
    )
    parser.add_argument(
        "--execution-model",
        choices=EXECUTION_MODELS,
        default=None,
        help="continuous (HMA default), research windows (Donchian default) or grid "
        "limit orders (grid strategies only)",
    )
    parser.add_argument(
        "--cost-case",
        choices=tuple(c.name for c in COSTS),
        default=None,
        help="Research cost case (default base)",
    )
    parser.add_argument(
        "--allow-holdout-overlap",
        action="store_true",
        help="Acknowledge evaluating non-preregistered parameters on a frozen holdout",
    )
    shared, groups = None, {}
    for dest, (parameter, owners) in strategy_parameters().items():
        if len(owners) > 1:
            shared = shared or parser.add_argument_group(
                "parameters shared by several strategies (default depends on --strategy)")
            group = shared
            label = parameter.help.rsplit(" (default", 1)[0]
            defaults = ", ".join(f"{a.key} {_shown(a, dest)}" for a in owners)
            help_text = f"{label}; defaults: {defaults}"
        else:
            key = owners[0].key
            if key not in groups:
                groups[key] = parser.add_argument_group(f"{key} parameters")
            group = groups[key]
            help_text = parameter.help
        group.add_argument(
            *parameter.flags, dest=dest, type=parameter.type, choices=parameter.choices,
            default=None, help=help_text,
        )
    root.defined_options = frozenset(
        {"-h", "--help"} | {option for action in actions for option in action.option_strings})
    return root


def option_names():
    """Every option string the unified backtest command defines."""
    return build_parser().defined_options


def strategy_parameters():
    """Each strategy option once: dest -> (parameter, adapters declaring it).

    Adapters that share an option must declare it identically (flags, type and
    choices); only the default may differ.
    """
    found = {}
    for adapter in ADAPTERS.values():
        for p in adapter.parameters:
            if p.dest in found:
                first = found[p.dest][0]
                if (first.flags, first.type, first.choices) != (p.flags, p.type, p.choices):
                    raise ValueError(f"Inconsistent shared strategy option {p.flags[0]}")
                found[p.dest][1].append(adapter)
            else:
                found[p.dest] = (p, [adapter])
    return found


def _shown(adapter, dest):
    return next(p.default for p in adapter.parameters if p.dest == dest)


def request_from_args(args):
    adapter = ADAPTERS[args.strategy]
    parameters = {}
    for dest, (p, owners) in strategy_parameters().items():
        value = getattr(args, dest)
        if value is None:
            continue
        if adapter not in owners:
            keys = ", ".join(owner.key for owner in owners)
            raise PlanError(f"{p.flags[0]} applies only to --strategy {keys}")
        parameters[dest] = value
    return BacktestRequest(
        strategy=args.strategy,
        parameters=parameters,
        symbol=args.symbol,
        time_frame=args.time_frame,
        data_source=args.data_source,
        execution=args.execution_model,
        start=date(args.start) if args.start else None,
        end=date(args.end) if args.end else None,
        lookback_days=args.lookback_days,
        initial_balance=args.initial_balance,
        size=args.size,
        leverage=args.leverage,
        commission_per_order=args.commission_per_order,
        slippage_pips=args.slippage_pips,
        financing_usd_per_unit_day=args.financing_usd_per_unit_day,
        cost_case=args.cost_case,
        snapshot=args.snapshot,
        cache_dir=args.cache_dir,
        refresh_data=args.refresh_data,
        allow_data_gaps=args.allow_data_gaps,
        allow_holdout_overlap=args.allow_holdout_overlap,
    )


def listing():
    width = max(len(key) for key in ADAPTERS)
    names = {s.backtest_adapter: s.name for s in registry.with_mode(registry.BACKTEST)}
    rows = [f"{'Strategy':<{width}}  Time frame  Data source  Execution   Description"]
    for adapter in ADAPTERS.values():
        name = names[adapter.key]
        rows.append(f"{adapter.key:<{width}}  {adapter.time_frame:<10}  "
                    f"{adapter.data_source:<11}  {adapter.execution:<10}  {name}")
    trading_only = [s.key for s in registry.STRATEGIES.values()
                    if s.tradable and not s.supports(registry.BACKTEST)]
    rows += ["", "Defaults shown are used when --time-frame, --data-source or "
             "--execution-model is omitted; the default strategy is hma.",
             "Strategy parameters and their defaults: tb-backtest --help"]
    if trading_only:
        rows.append(f"No historical backtest (DEMO/LIVE trading via tb-trade): "
                    f"{', '.join(trading_only)}")
    return "\n".join(rows)


def _check_strategy_selection(parser, argv):
    """Refuse repeated or trading-only --strategy values before parsing."""
    selected = []
    for i, token in enumerate(argv):
        if token == "--":
            break
        if token == "--strategy" and i + 1 < len(argv):
            selected.append(argv[i + 1])
        elif token.startswith("--strategy="):
            selected.append(token.split("=", 1)[1])
    if len(selected) > 1:
        parser.error("--strategy may be given only once")
    for key in selected:
        strategy = registry.STRATEGIES.get(key)
        if strategy is not None and key not in ADAPTERS:
            parser.error(f"strategy {key!r} has no historical backtest; it supports "
                         f"DEMO/LIVE trading via: tb-trade --strategy {key}")


def main(argv=None, *, now=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Broker, LIVE and scheduling options are refused before anything else.
    reject_broker_options(argparse.Namespace(), parser, argv)
    _check_strategy_selection(parser, argv)
    args = parser.parse_args(argv)
    if args.list_strategies:
        if len(argv) > 1:
            parser.error("--list-strategies takes no other arguments")
        print(listing())
        return 0
    reject_broker_options(args, parser, argv)
    try:
        plan = make_plan(request_from_args(args), now=now)
    except (PlanError, HoldoutReuseError) as exc:
        parser.error(str(exc))  # Invalid combination or refused holdout reuse.
    except (DataError, ValueError, OverflowError) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1
    try:
        return _run(plan, args)
    except HoldoutReuseError as exc:  # Only known once the snapshot fingerprint is read.
        print(f"Backtest refused: {exc}", file=sys.stderr)
        return 2
    except (DataError, ValueError, OSError, OverflowError, KeyError, EOFError) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        if plan.data_source == "snapshot":
            from .study_data import snapshot_help

            print(snapshot_help(plan.request.snapshot), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
