"""Execution models: one shared engine, two explicit and different cost/fill contracts.

``ContinuousExecution`` is the historical HMA mode: next-open fills against the
source bid/ask, positions carried across closures and never force-liquidated,
optional linear costs that are *unmodeled* (not zero) unless configured.

``ResearchExecution`` is the Donchian research mode: flat-to-flat windows,
unexpected-gap resets, a costed synthetic exit at the last completed close, a
preregistered cost case (spread multiple, slippage, NY-rollover financing) and an
explicit zero-commission assumption.

Both use ``engine.simulate`` and the same Decimal ``Ledger``.

``GridExecution`` is the fixed-grid mode: many working limit orders and
separately accounted lots (``backtesting.grid``), with the same optional
continuous-mode cost assumptions and the same report contract.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal

from .engine import (
    CONTINUOUS,
    RESEARCH_WINDOW,
    ExecutionPolicy,
    SimulationConfig,
    pnl_decomposition,
    run,
    statistics,
)
from .grid import ending_inventory, grid_pnl, grid_statistics, run_grid
from .study_engine import COSTS, CostCase, research_config, simulate_window

EXECUTION_MODELS = ("continuous", "research", "grid")


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


class SinglePositionReport:
    """Report hooks for models simulated on the single-position ``Ledger``."""

    def pnl(self, result):
        return pnl_decomposition(result)

    def open_positions(self, result):
        position = result.ledger.position
        return [asdict(position)] if position else []

    def extras(self, result):
        return {}

    def reproducibility(self, result):
        return {}


@dataclass(frozen=True)
class ContinuousExecution(SinglePositionReport):
    config: SimulationConfig
    name = "continuous"
    policy = CONTINUOUS
    description = (
        "completed close decision; next available quoted open; no final forced liquidation"
    )

    def simulate(self, candles, strategy, start, end):
        result = run(candles, strategy, self.config, start=start, end=end)
        return result, statistics(result)

    def configuration(self):
        return asdict(self.config)

    def unmodeled(self, dataset):
        missing = [
            name
            for name in ("commission_per_order", "slippage_pips", "financing_per_unit_day")
            if getattr(self.config, name) is None
        ]
        if not dataset.candles[0].bid:
            missing.append("historical_bid_ask_spread")
        return missing

    def assumed(self, dataset):
        c = self.config
        values = []
        if c.commission_per_order is not None:
            values.append(f"commission {_plain(c.commission_per_order)} USD/order")
        if c.slippage_pips is not None:
            values.append(f"slippage {_plain(c.slippage_pips)} pip/leg")
        if c.financing_per_unit_day is not None:
            values.append(
                f"financing {_plain(c.financing_per_unit_day)} USD/unit/calendar day"
            )
        return values

    def spread_basis(self, dataset):
        return ("historical bid/ask" if dataset.candles[0].bid
                else "indicative OHLC; bid/ask unavailable")

    def assumptions(self):
        return {
            "account_currency": "USD",
            "contract": "EURUSD base-currency units; 1 EUR per unit",
            "margin": "fixed entry notional / configured leverage, collateral only, not an expense",
            "financing": "configured linear USD/unit/calendar-day debit, not a broker swap schedule",
            "liquidation": "no assumed broker stop-out; report negative available funds and reject new entries",
            "valuation": "last observed liquidation-side close; open/close drawdown excludes unseen intrabar paths",
            "spread_and_slippage": "embedded in execution prices; never deducted again; spread estimate sums executed-leg half-spreads, excludes future liquidation",
            "balance": "initial + realized net cash P&L, including paid costs of any still-open position",
            "unknown_costs": "not charged in arithmetic; explicitly unmodeled, never asserted to be zero",
        }

    def notes(self, dataset):
        return (
            "Gross P&L is midpoint price P&L before costs, including any open position "
            "at the closing midpoint. Net P&L marks open positions at the liquidation "
            "side, so it also excludes the unexecuted exit half-spread (open valuation "
            "spread). All orders are simulated.\n"
            "Costs unavailable from the provider are unmodeled; results are not "
            "guaranteed net of all trading costs."
        )


@dataclass(frozen=True)
class ResearchExecution(SinglePositionReport):
    case: CostCase
    config: SimulationConfig
    name = "research"
    policy = RESEARCH_WINDOW
    description = (
        "flat start; completed close decision; next observed open; unexpected gaps "
        "cancel entries and reset warm-up; costed synthetic exit at last completed close"
    )

    @classmethod
    def from_case(cls, case_name="base", *, initial_balance=Decimal(1000),
                  size=Decimal(100), leverage=Decimal(1)):
        case = {c.name: c for c in COSTS}.get(case_name)
        if case is None:
            raise ValueError(f"Unknown research cost case: {case_name}")
        return cls(case, research_config(case, initial_balance=initial_balance,
                                         size=size, leverage=leverage))

    def simulate(self, candles, strategy, start, end):
        return simulate_window(candles, strategy, self.case, start, end, config=self.config)

    def configuration(self):
        return dict(asdict(self.config), cost_case=asdict(self.case))

    def unmodeled(self, dataset):
        return ["historical_financing"] if self.case.financing_rate == 0 else []

    def assumed(self, dataset):
        case = self.case
        values = ["commission 0 USD/order (assumed, not a verified fee)",
                  f"slippage {_plain(case.slippage_pips)} pip/leg"]
        if not dataset.candles[0].bid:
            values.append(
                f"spread {_plain(case.midpoint_spread_pips * case.spread_multiplier)} pip "
                "full spread around midpoint"
            )
        elif case.spread_multiplier != 1:
            values.append(f"spread {_plain(case.spread_multiplier)}x observed bid/ask")
        if case.financing_rate:
            values.append(
                f"financing {_plain(case.financing_rate * 100)}% annualized entry-midpoint "
                "notional/360 at NY 17:00 weekdays, Wednesday triple (sensitivity)"
            )
        return values

    def spread_basis(self, dataset):
        return ("historical bid/ask" if dataset.candles[0].bid
                else f"midpoint; assumed {_plain(self.case.midpoint_spread_pips)} pip spread")

    def assumptions(self):
        return {
            "account_currency": "USD",
            "contract": "EURUSD base-currency units; 1 EUR per unit",
            "margin": "fixed entry notional / configured leverage, collateral only, not an expense",
            "execution_prices": "buy at ask + slippage, sell at bid - slippage; bid/ask rebuilt around the observed midpoint with the cost case spread multiple",
            "financing": "cost-case annual rate on entry midpoint notional / 360 at NY 17:00 weekday rollovers, Wednesday triple; zero rate means historical financing is omitted, not known to be zero",
            "commission": "assumed zero; not a verified broker fee",
            "gaps": "unexpected active-session gaps cancel pending entries, keep held positions and their pending exits, and reset indicator warm-up; weekend closures queue signals",
            "boundary": "each window starts flat and ends with a costed synthetic exit at the last completed close; this is a valuation convention, not a guaranteed fill",
            "valuation": "observed open/close equity; drawdown excludes unseen intrabar paths",
        }

    def notes(self, dataset):
        case = self.case
        spread = ("spread" if case.spread_multiplier == 1
                  else f"{_plain(case.spread_multiplier)}x spread")
        financing = ("historical financing omitted" if not case.financing_rate else
                     f"{_plain(case.financing_rate * 100)}% annualized financing sensitivity")
        return (
            "Gross P&L uses midpoint prices. All orders are simulated.\n"
            f"{case.name.capitalize()} costs: {spread} + {_plain(case.slippage_pips)} "
            f"pip/leg slippage; commission assumed zero; {financing}."
        )


GRID_POLICY = ExecutionPolicy("grid_limit_orders")


@dataclass(frozen=True)
class GridExecution(ContinuousExecution):
    """Fixed-grid limit orders on the shared data, cost and report contracts."""

    name = "grid"
    policy = GRID_POLICY
    description = (
        "completed close EMA direction; grid activates or flattens at the next observed "
        "open; limit orders fill intrabar on bid/ask with gap-through at the open; "
        "orders created by a fill wait one candle; no final forced liquidation"
    )

    def simulate(self, candles, strategy, start, end):
        result = run_grid(candles, strategy, self.config, start=start, end=end)
        return result, grid_statistics(result)

    def assumed(self, dataset):
        c = self.config
        values = []
        if c.commission_per_order is not None:
            values.append(f"commission {_plain(c.commission_per_order)} USD/fill")
        if c.slippage_pips is not None:
            values.append(
                f"slippage {_plain(c.slippage_pips)} pip per market (flatten) fill; "
                "limit fills at limit or better"
            )
        if c.financing_per_unit_day is not None:
            values.append(
                f"financing {_plain(c.financing_per_unit_day)} USD/unit/calendar day per lot"
            )
        return values

    def assumptions(self):
        return dict(
            super().assumptions(),
            direction="EMA of completed midpoint closes (source close without bid/ask); "
            "above selects long, below short, equal keeps the current grid",
            bounds="manual --grid-lower/--grid-upper, or automatic: lowest low and "
            "highest high of the last --grid-lookback completed candles ending at or "
            "before the start (bid/ask: mean of bid and ask lows/highs, a midpoint "
            "estimate); levels fixed for the run",
            activation="direction changes cancel working orders at the signal close, "
            "flatten lots at the next observed open, then seed the new grid relative to "
            "the signal close",
            fills="buys on ask, sells on bid; gap-through fills at the open, touches fill "
            "at the limit; one candle path open -> nearer extreme -> farther extreme -> "
            "close orders multi-level fills; fill-created orders wait one candle",
            exposure="open lot units + working entry units <= max position; nearer "
            "levels admitted first; funds checked on admission and on fill",
            spread_and_slippage="embedded in execution prices; spread estimate is half "
            "the fill candle's opening spread per fill; slippage only on market "
            "(flatten) fills",
        )

    def notes(self, dataset):
        return (
            "Grid lots are accounted separately; gross P&L is midpoint-estimated before "
            "costs and net P&L marks open lots at the liquidation side. Intrabar fill "
            "order follows an assumed candle path. All orders are simulated.\n"
            "Costs unavailable from the provider are unmodeled; results are not "
            "guaranteed net of all trading costs."
        )

    def pnl(self, result):
        return grid_pnl(result)

    def open_positions(self, result):
        return ending_inventory(result)["open_lots"]

    def extras(self, result):
        strategy = result.ledger.strategy
        return {
            "grid": {
                "bounds": result.bounds,
                "levels": list(strategy.levels),
                "ema_period": strategy.ema_period,
                "level_size": strategy.level_size,
                "max_position": strategy.max_position,
                "peak_exposure": dict(result.ledger.peak),
                "ending_inventory": ending_inventory(result),
                "activations": [e for e in result.ledger.events
                                if e["action"] in ("ACTIVATE", "DEACTIVATE")],
                "orders": [asdict(o) for o in result.ledger.orders],
            }
        }

    def reproducibility(self, result):
        # Measured bounds depend on the data window, so they are part of the record.
        return {"grid_bounds": result.bounds}
