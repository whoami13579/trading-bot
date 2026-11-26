"""Strategy capability registry shared by ``tb-trade`` and ``tb-backtest``.

Each strategy is declared once, explicitly, with the execution modes it already
implements. Importing this module imports no strategy, broker, credential or
backtesting code: trading entry points are resolved only when ``tb-trade``
dispatches to one, so help, listing and rejected commands stay offline.

The registry coordinates; it does not re-implement. A trading entry point is the
strategy's existing ``main()`` (its own argument parser, defaults and safety
gates). A backtest is named by its key in
``trading_bot.backtesting.adapters.ADAPTERS``, which remains the single source of
simulation parameters and defaults. Offline tests keep both in sync with this
table and with the installed console scripts.

Adding a strategy: implement it under ``trading_bot.strategies``, add one
``Strategy`` entry below with only the modes it really supports, add a
``StrategyAdapter`` if it backtests, and add tests and documentation. No
executable wrapper file is needed.
"""

from dataclasses import dataclass
from importlib import import_module

DEMO = "demo"
LIVE = "live"
BACKTEST = "backtest"
MODES = (DEMO, LIVE, BACKTEST)
_TRADING_MODES = frozenset((DEMO, LIVE))
_ENTRY_PACKAGE = "trading_bot.strategies."


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    modes: frozenset
    # "module:function" of the strategy's existing argparse entry point.
    trading_entry: str | None = None
    # Key of the strategy's adapter in trading_bot.backtesting.adapters.ADAPTERS.
    backtest_adapter: str | None = None
    # Pre-existing commands that run the same implementation.
    legacy_commands: tuple = ()
    # Options of the online command, beyond --symbol/--size, that tb-backtest also
    # defines with the same meaning. tb-trade refuses every other tb-backtest option
    # for this strategy. Offline tests check this against the strategy's parser.
    trading_options: frozenset = frozenset()

    def __post_init__(self):
        modes = frozenset(self.modes)
        object.__setattr__(self, "modes", modes)
        if not modes or not modes <= set(MODES):
            raise ValueError(f"{self.key}: modes must be a non-empty subset of {MODES}")
        if bool(modes & _TRADING_MODES) != (self.trading_entry is not None):
            raise ValueError(f"{self.key}: DEMO/LIVE modes require a trading entry point")
        if LIVE in modes and DEMO not in modes:
            raise ValueError(f"{self.key}: LIVE support requires DEMO support")
        if (BACKTEST in modes) != (self.backtest_adapter is not None):
            raise ValueError(f"{self.key}: backtest mode requires a backtest adapter")
        object.__setattr__(self, "trading_options", frozenset(self.trading_options))
        if self.trading_options and self.trading_entry is None:
            raise ValueError(f"{self.key}: trading options require a trading entry point")
        if self.trading_entry is not None:
            module, _, function = self.trading_entry.partition(":")
            if not module.startswith(_ENTRY_PACKAGE) or not function.isidentifier():
                raise ValueError(f"{self.key}: trading entry must be trading_bot.strategies.*:function")

    def supports(self, mode):
        return mode in self.modes

    @property
    def tradable(self):
        return bool(self.modes & _TRADING_MODES)

    def load_trading_entry(self):
        """Import and return the strategy's trading ``main``; only called on dispatch."""
        if self.trading_entry is None:
            raise LookupError(f"Strategy {self.key} has no DEMO/LIVE trading entry point")
        module, _, function = self.trading_entry.partition(":")
        return getattr(import_module(module), function)


STRATEGIES = {
    strategy.key: strategy
    for strategy in (
        Strategy(
            "moving-average",
            "Price vs. 20-candle moving average (1-minute)",
            frozenset((DEMO, LIVE, BACKTEST)),
            trading_entry="trading_bot.strategies.moving_average:main",
            backtest_adapter="moving-average",
            legacy_commands=("trading-bot-moving-average", "main.py"),
        ),
        Strategy(
            "sma",
            "SMA 9/21 crossover (1-minute)",
            frozenset((DEMO, LIVE, BACKTEST)),
            trading_entry="trading_bot.strategies.sma:main",
            backtest_adapter="sma",
            legacy_commands=("trading-bot-sma", "sma.py"),
        ),
        Strategy(
            "hma",
            "Hull moving average, configured side / flat",
            frozenset((DEMO, LIVE, BACKTEST)),
            # hma.trade runs hma.main without advertising its --backtest options.
            trading_entry="trading_bot.strategies.hma:trade",
            backtest_adapter="hma",
            legacy_commands=("trading-bot-hma", "hma.py", "hma.py --backtest"),
            trading_options=frozenset(("--hma-period", "--hma_period", "--direction",
                                       "--time-frame", "--time_frame")),
        ),
        Strategy(
            "ema-hma",
            "EMA 200/20 trend filter with HMA confirmation",
            frozenset((DEMO, LIVE, BACKTEST)),
            trading_entry="trading_bot.strategies.ema_hma:main",
            backtest_adapter="ema-hma",
            legacy_commands=("trading-bot-ema-hma", "ema_hma.py"),
            trading_options=frozenset(("--long_period", "--short_period", "--hma_period",
                                       "--time_frame")),
        ),
        Strategy(
            "multiple-hma",
            "4H 13-indicator vote with 30-minute HMA confirmation",
            frozenset((DEMO, LIVE, BACKTEST)),
            trading_entry="trading_bot.strategies.multiple_hma:main",
            backtest_adapter="multiple-hma",
            legacy_commands=("trading-bot-multiple-hma", "multiple_hma.py"),
            trading_options=frozenset(("--hma_period", "--long_term")),
        ),
        Strategy(
            "donchian",
            "Donchian channel breakout (historical simulation only)",
            frozenset((BACKTEST,)),
            backtest_adapter="donchian",
            legacy_commands=("src/trading_bot/strategies/donchian.py --backtest",),
        ),
        Strategy(
            "grid",
            "Candle-directed fixed grid (historical simulation only)",
            frozenset((BACKTEST,)),
            backtest_adapter="grid",
        ),
    )
}


def get(key):
    """Return a registered strategy; KeyError for anything else (no dynamic lookup)."""
    return STRATEGIES[key]


def with_mode(mode):
    return tuple(s for s in STRATEGIES.values() if s.supports(mode))


def capability_table():
    """The capability matrix as plain text, from this registry only."""
    width = max(len(key) for key in STRATEGIES)
    rows = [f"{'Strategy':<{width}}  DEMO  LIVE  Backtest  Description"]
    for s in STRATEGIES.values():
        flags = ("yes" if s.supports(mode) else "no" for mode in MODES)
        rows.append(f"{s.key:<{width}}  {next(flags):<4}  {next(flags):<4}  {next(flags):<8}  {s.name}")
    return "\n".join(rows)
