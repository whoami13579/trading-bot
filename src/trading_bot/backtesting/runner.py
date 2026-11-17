"""Programmatic backtest pipeline shared by every CLI: plan -> data -> simulate -> report.

``plan`` validates everything without I/O. ``execute`` loads the dataset
(provider cache or verified snapshot), runs the selected execution model on
the shared engine and returns a standardized report. Nothing here imports a
broker client, reads credentials or submits orders.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .adapters import adapter_for
from .cache import Cache
from .data import load_history, load_snapshot
from .evaluation import canonical_sha256, holdout_check
from .execution import ContinuousExecution, ResearchExecution
from .engine import SimulationConfig
from .models import DataError, utc
from .reporting import build_report
from .timeframes import MINUTES, floor_time

PROVIDER_SOURCES = ("dukascopy", "yfinance")
DATA_SOURCES = (*PROVIDER_SOURCES, "snapshot")
DEFAULT_SNAPSHOT = Path(".cache/donchian/eurusd-30m.json.gz")
DEFAULT_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class BacktestRequest:
    """Unset (None) fields use the selected strategy's preserved defaults."""

    strategy: str = "hma"
    parameters: dict = field(default_factory=dict)
    symbol: str = "EURUSD"
    time_frame: str | None = None
    data_source: str | None = None
    execution: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    lookback_days: int | None = None
    initial_balance: Decimal = Decimal(1000)
    size: Decimal = Decimal(100)
    leverage: Decimal = Decimal(1)
    commission_per_order: Decimal | None = None
    slippage_pips: Decimal | None = None
    financing_usd_per_unit_day: Decimal | None = None
    cost_case: str | None = None
    snapshot: Path = DEFAULT_SNAPSHOT
    cache_dir: Path = Path(".cache/backtests")
    refresh_data: bool = False
    allow_data_gaps: bool = False
    allow_holdout_overlap: bool = False


@dataclass(frozen=True)
class Plan:
    request: BacktestRequest
    adapter: object
    strategy: object
    time_frame: str
    data_source: str
    execution: object
    start: datetime
    end: datetime
    holdouts: tuple
    now: datetime


class PlanError(ValueError):
    """Invalid option combination, detected before any data access."""


def plan(request: BacktestRequest, *, now=None) -> Plan:
    adapter = adapter_for(request.strategy)
    now = utc(now or datetime.now(timezone.utc))
    unknown = set(request.parameters) - set(adapter.defaults())
    if unknown:
        raise PlanError(
            f"{', '.join(sorted(unknown))} not supported by --strategy {adapter.key}"
        )
    strategy = adapter.build(**request.parameters)
    time_frame = request.time_frame or adapter.time_frame
    if time_frame not in MINUTES:
        raise PlanError(f"Unsupported timeframe: {time_frame}")
    source = request.data_source or adapter.data_source
    if source not in DATA_SOURCES:
        raise PlanError(f"Unsupported data source: {source}")
    model = request.execution or adapter.execution
    configured = {
        "--commission-per-order": request.commission_per_order,
        "--slippage-pips": request.slippage_pips,
        "--financing-usd-per-unit-day": request.financing_usd_per_unit_day,
    }
    if model == "continuous":
        if request.cost_case is not None:
            raise PlanError("--cost-case applies only to --execution-model research")
        execution = ContinuousExecution(
            SimulationConfig(
                request.initial_balance,
                request.size,
                request.leverage,
                request.commission_per_order,
                request.slippage_pips,
                request.financing_usd_per_unit_day,
            )
        )
    elif model == "research":
        given = [name for name, value in configured.items() if value is not None]
        if given:
            raise PlanError(
                f"{', '.join(given)} apply only to --execution-model continuous; research "
                "costs come from --cost-case base|high|financing"
            )
        execution = ResearchExecution.from_case(
            request.cost_case or "base",
            initial_balance=request.initial_balance,
            size=request.size,
            leverage=request.leverage,
        )
    else:
        raise PlanError(f"Unsupported execution model: {model}")
    if request.lookback_days is not None and request.lookback_days <= 0:
        raise PlanError("--lookback-days must be positive")
    if source == "snapshot":
        from .study_data import START, END

        end = request.end or END
        if request.start is not None:
            start = request.start
        elif request.lookback_days is None:
            start = START
        else:
            start = end - timedelta(days=request.lookback_days)
    else:
        end = request.end or floor_time(now, "DAY")
        start = request.start or end - timedelta(
            days=request.lookback_days or DEFAULT_LOOKBACK_DAYS
        )
    start, end = utc(start), utc(end)
    if start >= end:
        raise PlanError("--start must be before --end")
    holdouts = holdout_check(
        adapter.key,
        adapter.values(strategy),
        request.symbol,
        start,
        end,
        allow=request.allow_holdout_overlap,
    )
    return Plan(request, adapter, strategy, time_frame, source, execution, start, end,
                tuple(holdouts), now)


def load(plan: Plan):
    r = plan.request
    warmup = plan.strategy.warmup
    if plan.data_source == "snapshot":
        # Research windows decide first at the first scored close, so one fewer
        # prior bar suffices; continuous mode primes the first scored open.
        required = warmup - 1 if plan.execution.name == "research" else warmup
        return load_snapshot(r.snapshot, r.symbol, plan.time_frame, plan.start, plan.end,
                             warmup, required_warmup=required)
    return load_history(
        plan.data_source,
        r.symbol,
        plan.time_frame,
        plan.start,
        plan.end,
        warmup,
        Cache(r.cache_dir, refresh=r.refresh_data),
        allow_gaps=r.allow_data_gaps,
        now=plan.now,
    )


def configuration_record(plan: Plan, dataset):
    """Everything that determines a result; its hash identifies a reproducible run."""
    return {
        "strategy": plan.adapter.key,
        "parameters": plan.adapter.values(plan.strategy),
        "symbol": plan.request.symbol,
        "timeframe": plan.time_frame,
        "data_source": plan.data_source,
        "start": plan.start,
        "end": plan.end,
        "execution_model": plan.execution.name,
        "configuration": plan.execution.configuration(),
        "allow_data_gaps": plan.request.allow_data_gaps,
        "data_sha256": dataset.metadata.get("data_sha256"),
    }


def execute(plan: Plan, dataset=None):
    if dataset is None:
        dataset = load(plan)
    if not dataset.candles:
        raise DataError("No candles available for the requested range")
    result, stats = plan.execution.simulate(dataset.candles, plan.strategy, plan.start,
                                            plan.end)
    record = configuration_record(plan, dataset)
    return build_report(
        result,
        stats,
        dataset,
        adapter=plan.adapter,
        strategy=plan.strategy,
        execution=plan.execution,
        data_source=plan.data_source,
        holdouts=plan.holdouts,
        reproducibility={
            "configuration_sha256": canonical_sha256(record),
            "data_sha256": dataset.metadata.get("data_sha256"),
            "configuration": record,
        },
    )


def run_backtest(request: BacktestRequest, *, now=None):
    return execute(plan(request, now=now))
