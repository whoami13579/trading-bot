"""Unified DEMO/LIVE trading command: ``tb-trade --strategy STRATEGY [options]``.

The command validates only what it owns (strategy selection, the requested
environment against the strategy's registered modes, and options that belong
to other commands), then calls the selected strategy's existing ``main()``
exactly once. The strategy parses every remaining option with its own parser,
defaults and validation, and runs its unchanged authentication, LIVE gate, risk
limits and network-recovery code. Nothing here authenticates, reads
credentials or contacts a broker; help, listing and every rejected command exit
before any strategy module is imported.
"""

import argparse
import sys

from trading_bot.strategies import registry

PROG = "tb-trade"
_LIVE_FLAGS = frozenset(("--REAL", "--real"))
# Plausible environment spellings that strategies do not define. They are refused
# with a clear message instead of reaching a strategy parser as unknown options.
_ENVIRONMENT_SELECTORS = frozenset(("--environment", "--demo", "--DEMO", "--live", "--LIVE"))
# Options both commands define with the same meaning for an order-capable strategy.
_SHARED_WITH_BACKTEST = frozenset(
    ("-h", "--help", "--strategy", "--list-strategies", "--symbol", "--size",
     "--time-frame", "--time_frame")
)

EXAMPLES = """\
examples:
  tb-trade --list-strategies
  tb-trade --strategy hma --help                       # that strategy's own options
  tb-trade --strategy hma                              # DEMO (default)
  tb-trade --strategy hma --symbol EURUSD --size 100 --time-frame MINUTE_30
  tb-trade --strategy hma --read-only --cycles 1       # DEMO diagnostics, no orders
  tb-trade --strategy hma --REAL                       # LIVE, real money, guarded

Every strategy run without --read-only is order-capable. DEMO is the default;
LIVE requires --REAL or --real and passes the existing LIVE preflight.
Historical simulations (hma, donchian, grid) use: tb-backtest --strategy NAME
"""


def build_parser():
    tradable = ", ".join(s.key for s in registry.with_mode(registry.DEMO))
    parser = argparse.ArgumentParser(
        prog=PROG,
        usage="%(prog)s --strategy STRATEGY [--REAL | --real] [strategy options]",
        description=__doc__.split("\n\n", 1)[0].replace("``", ""),
        epilog=f"strategies:\n{registry.capability_table()}\n\n{EXAMPLES}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("--strategy", action="append", metavar="STRATEGY",
                        help=f"Required, exactly once. Order-capable strategies: {tradable}")
    parser.add_argument("--list-strategies", action="store_true",
                        help="List every registered strategy and its supported modes, then exit")
    parser.add_argument("-h", "--help", action="store_true",
                        help="Show this help; with --strategy, show that strategy's options")
    parser.add_argument_group(
        "environment and strategy options",
        "--REAL/--real selects LIVE (default DEMO). All other options, such as\n"
        "--symbol, --size, --time-frame, --account and the LIVE risk limits, are\n"
        "parsed by the selected strategy unchanged; see: tb-trade --strategy NAME --help",
    )
    return parser


def listing():
    return (f"{registry.capability_table()}\n\n"
            "tb-trade runs strategies with DEMO/LIVE support: DEMO by default, LIVE only "
            "with --REAL/--real.\ntb-backtest runs strategies with backtest support: "
            "historical simulation, no broker access.")


def _select(parser, key, live):
    try:
        strategy = registry.get(key)
    except KeyError:
        parser.error(f"unknown strategy {key!r}; choose from: {', '.join(registry.STRATEGIES)}")
    if not strategy.tradable:
        parser.error(f"strategy {key!r} supports historical backtesting only and cannot "
                     f"trade on DEMO or LIVE; use: tb-backtest --strategy {key}")
    mode = registry.LIVE if live else registry.DEMO
    if not strategy.supports(mode):
        parser.error(f"strategy {key!r} does not support {mode.upper()} trading")
    return strategy


def _backtest_only_options():
    """Options of the backtest command that mean nothing to an order-capable strategy."""
    from trading_bot.backtesting.adapters import ADAPTERS
    from trading_bot.backtesting.cli import build_parser as backtest_parser

    shared = set(_SHARED_WITH_BACKTEST)
    for strategy in registry.with_mode(registry.DEMO):
        if strategy.backtest_adapter:  # e.g. --hma-period/--direction for hma
            shared.update(flag for parameter in ADAPTERS[strategy.backtest_adapter].parameters
                          for flag in parameter.flags)
    options = {option for action in backtest_parser()._actions for option in action.option_strings}
    return options - shared


def _reject_foreign_options(parser, strategy, forwarded):
    names = {token.split("=", 1)[0] for token in forwarded if token.startswith("-")}
    environment = sorted(names & _ENVIRONMENT_SELECTORS)
    if environment:
        parser.error(f"{', '.join(environment)}: not a tb-trade option; DEMO is the default "
                     "and LIVE is selected only with --REAL or --real")
    if names:
        backtest = sorted(names & _backtest_only_options())
        if backtest:
            parser.error(f"{', '.join(backtest)}: historical backtesting options are not "
                         f"accepted by tb-trade; use: tb-backtest --strategy {strategy.key}"
                         if strategy.supports(registry.BACKTEST) else
                         f"{', '.join(backtest)}: historical backtesting options are not "
                         f"accepted by tb-trade, and {strategy.key!r} has no backtest")


def dispatch(strategy, forwarded):
    """Run the strategy's own ``main()`` once, exactly as its legacy command would."""
    entry = strategy.load_trading_entry()
    saved = sys.argv
    sys.argv = [f"{PROG} --strategy {strategy.key}", *forwarded]
    try:
        return entry()
    finally:
        sys.argv = saved


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args, forwarded = parser.parse_known_args(argv)
    if args.list_strategies:
        if args.strategy or args.help or forwarded:
            parser.error("--list-strategies takes no other arguments")
        print(listing())
        return 0
    if not args.strategy:
        if args.help:
            parser.print_help()
            parser.exit()  # SystemExit(0), as for every other command's --help.
        parser.error("--strategy is required; see: tb-trade --list-strategies")
    if len(args.strategy) > 1:
        parser.error("--strategy may be given only once")
    strategy = _select(parser, args.strategy[0], live=bool(_LIVE_FLAGS & set(forwarded)))
    _reject_foreign_options(parser, strategy, forwarded)
    if args.help:
        forwarded = ["--help", *forwarded]
    return dispatch(strategy, forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
