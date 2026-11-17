"""Reusable research safeguards: chronological splits, frozen holdouts and hashes.

Nothing here selects parameters. These helpers keep evaluation windows
chronological, make every configuration/data input fingerprintable, and stop a
registered holdout from being reused to choose parameters.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .models import json_value


def chronological_windows(start, end, edges=(0.4, 0.5, 0.6, 0.7, 0.8, 1)):
    """Expanding-train walk-forward validations followed by one final holdout.

    ``edges`` are calendar-day fractions rounded to whole UTC days. The first
    edge ends the initial (unscored) training period, each following edge ends
    one validation window, and the last validation edge starts the holdout.
    """
    days = (end - start).days
    points = [start + timedelta(days=round(days * f)) for f in edges]
    windows = [
        {
            "name": f"validation_{i + 1}",
            "train_start": start,
            "train_end": points[i],
            "start": points[i],
            "end": points[i + 1],
        }
        for i in range(len(points) - 2)
    ]
    windows.append({"name": "holdout", "start": points[-2], "end": end})
    return windows


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_sha256(value) -> str:
    """Order-independent fingerprint of a JSON-compatible configuration."""
    encoded = json.dumps(value, default=json_value, sort_keys=True, allow_nan=False)
    return sha256_bytes(encoded.encode())


def concentration(trades):
    """P&L concentration among winners and by exit year (net trade P&L)."""
    values = [Decimal(t["net_pnl"]) for t in trades]
    winners = sorted((v for v in values if v > 0), reverse=True)
    net = sum(values, Decimal(0))
    years = {}
    for trade in trades:
        year = str(trade["exit_timestamp"])[:4]
        years[year] = years.get(year, Decimal(0)) + Decimal(trade["net_pnl"])
    return {"net_pnl": net, "top_winner": winners[0] if winners else 0,
            "top5_winners": sum(winners[:5], Decimal(0)),
            "top5_share_of_positive_pnl_pct": sum(winners[:5], Decimal(0)) /
            sum(winners) * 100 if winners else None,
            "net_without_top5_winners": net - sum(winners[:5], Decimal(0)),
            "pnl_by_exit_year": years}


# Account terms every preregistered study configuration used (study.json, PLAN.md).
STUDY_ACCOUNT = {"size": Decimal(100), "initial_balance": Decimal(1000), "leverage": Decimal(1)}


@dataclass(frozen=True)
class Specification:
    """One configuration a study preregistered and scored on its holdout."""

    role: str  # primary | primary_cost_sensitivity | robustness | comparator
    strategy: str
    parameters: dict
    timeframe: str
    execution_model: str
    cost_case: str


@dataclass(frozen=True)
class Holdout:
    """A frozen, already-evaluated holdout; not for parameter selection.

    Only the complete preregistered specifications (strategy parameters,
    timeframe, execution model and cost case, on the registered dataset and
    account terms) are exempt. Anything else for a protected strategy is a new
    experiment and must not be evaluated on this period without acknowledgement.
    """

    study: str
    symbol: str
    start: datetime
    end: datetime
    data_source: str
    dataset_sha256: str
    account: dict
    protected_strategies: tuple[str, ...]
    specifications: tuple[Specification, ...]

    def overlaps(self, symbol, start, end):
        return symbol == self.symbol and start < self.end and self.start < end

    def match(self, spec, dataset_sha256=None):
        """Role of an exactly preregistered specification, else None.

        ``dataset_sha256`` None means the dataset has not been read yet; the
        runner repeats the check with the actual snapshot fingerprint.
        """
        if spec["data_source"] != self.data_source:
            return None
        if dataset_sha256 is not None and dataset_sha256 != self.dataset_sha256:
            return None
        if any(Decimal(str(spec[k])) != v for k, v in self.account.items()):
            return None
        for s in self.specifications:
            if (s.strategy, s.parameters, s.timeframe, s.execution_model, s.cost_case) == (
                spec["strategy"], spec["parameters"], spec["timeframe"],
                spec["execution_model"], spec["cost_case"],
            ):
                return s.role
        return None


def _role(label, timeframe, case):
    if label.startswith("donchian_"):
        if timeframe != "HOUR":
            return "robustness"
        return "primary" if case == "base" else "primary_cost_sensitivity"
    return "comparator"


def registered_holdouts():
    """Holdouts frozen by completed studies, derived from their own stage plans."""
    from .donchian_study import configurations, splits

    specs = []
    for timeframe, label, strategy, case in configurations("holdout"):
        if label.startswith("donchian_"):
            key = "donchian"
            parameters = {"entry_lookback": strategy.entry, "exit_lookback": strategy.exit}
        elif label.startswith("hma_"):
            key = "hma"
            parameters = {"hma_period": strategy.period, "direction": strategy.direction}
        else:
            continue  # The cash comparator is not a backtestable strategy.
        specs.append(Specification(_role(label, timeframe, case.name), key, parameters,
                                   timeframe, "research", case.name))
    window = splits()[-1]
    return (
        Holdout(
            "research/donchian",
            "EURUSD",
            window["start"],
            window["end"],
            "snapshot",
            # Canonical candle fingerprint of the frozen study snapshot (study.json).
            "f7ad8f615eb2dacd9a79417934f99f09a18da32f3c291f37ed605c4db1eda72a",
            STUDY_ACCOUNT,
            ("donchian",),
            tuple(specs),
        ),
    )


class HoldoutReuseError(ValueError):
    """A non-preregistered specification would be evaluated on a frozen holdout."""


def _describe(spec):
    return (f"{spec['strategy']} {spec['parameters']}, {spec['timeframe']}, "
            f"{spec['execution_model']} execution, cost case {spec['cost_case']}, "
            f"{spec['data_source']} data, size {spec['size']}, initial balance "
            f"{spec['initial_balance']}, leverage {spec['leverage']}")


def holdout_check(spec, start, end, *, dataset_sha256=None, allow=False):
    """Return overlap disclosures; refuse unacknowledged non-preregistered reuse.

    ``spec`` keys: strategy, parameters, symbol, timeframe, data_source,
    execution_model, cost_case, size, initial_balance, leverage.
    """
    overlaps = []
    for holdout in registered_holdouts():
        if not holdout.overlaps(spec["symbol"], start, end):
            continue
        role = holdout.match(spec, dataset_sha256)
        protected = spec["strategy"] in holdout.protected_strategies
        if protected and role is None and not allow:
            raise HoldoutReuseError(
                f"[{start.isoformat()}, {end.isoformat()}) overlaps the frozen "
                f"{holdout.study} holdout [{holdout.start.isoformat()}, "
                f"{holdout.end.isoformat()}). This configuration ({_describe(spec)}) is "
                "not one of the study's preregistered holdout specifications (primary: "
                "donchian 20/10, HOUR, research execution, base costs, on the frozen "
                "snapshot with size 100, balance 1000, leverage 1). Evaluate it outside "
                "the holdout, or pass --allow-holdout-overlap to acknowledge that the "
                "result must not be used for parameter or configuration selection."
            )
        verified = None
        if spec["data_source"] != holdout.data_source:
            verified = False
        elif dataset_sha256 is not None:
            verified = dataset_sha256 == holdout.dataset_sha256
        overlaps.append(
            {
                "study": holdout.study,
                "start": holdout.start,
                "end": holdout.end,
                "preregistered_role": role,
                "preregistered_specification": role is not None,
                "registered_dataset": verified,
                "acknowledged_reuse": bool(protected and role is None),
            }
        )
    return overlaps


class StageRegistry:
    """Append-only research stage markers for one study output directory.

    A stage can start once. The holdout can start only after a freeze that
    fingerprints the prepared study and development results, and only once.
    """

    def __init__(self, output):
        self.output = Path(output)

    def _dump(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, default=json_value, sort_keys=True, indent=2) + "\n")

    def begin(self, stage, *, frozen_inputs=()):
        marker = self.output / f"{stage}_started.json"
        if marker.exists():
            raise ValueError(f"{stage} already attempted; inspect registry, do not re-evaluate holdout")
        if stage == "holdout":
            freeze = json.loads((self.output / "freeze.json").read_text())
            for key, name in frozen_inputs:
                if freeze[key] != sha256_file(self.output / name):
                    raise ValueError("Frozen study changed" if name == "study.json"
                                     else "Development results changed after freeze")
        self._dump(marker, {"started_at": datetime.now(timezone.utc), "stage": stage})

    def freeze(self, inputs, **statement):
        if (self.output / "freeze.json").exists() or (self.output / "holdout_started.json").exists():
            raise ValueError("Already frozen or holdout attempted")
        record = {"frozen_at": datetime.now(timezone.utc)}
        record.update({key: sha256_file(self.output / name) for key, name in inputs})
        record.update(statement)
        self._dump(self.output / "freeze.json", record)

    def attempt(self, config, status, **extra):
        """Persist every attempt, including failures, before and after evaluation."""
        with (self.output / "attempts.jsonl").open("a") as f:
            f.write(json.dumps(dict(config, status=status, **extra), default=json_value) + "\n")
