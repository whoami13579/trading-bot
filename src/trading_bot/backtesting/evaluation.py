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


@dataclass(frozen=True)
class Holdout:
    """A frozen, already-evaluated holdout; not for parameter selection."""

    study: str
    symbol: str
    start: datetime
    end: datetime
    # Strategy keys whose preregistered parameters were scored on this holdout.
    # Any other parameter set is a new specification and must not be chosen here.
    protected_strategies: tuple[str, ...]
    preregistered: dict

    def overlaps(self, symbol, start, end):
        return symbol == self.symbol and start < self.end and self.start < end


def registered_holdouts():
    """Holdouts frozen by completed studies in this repository."""
    from .study_data import START, END

    window = chronological_windows(START, END)[-1]
    return (
        Holdout(
            "research/donchian",
            "EURUSD",
            window["start"],
            window["end"],
            ("donchian",),
            {"donchian": {"entry_lookback": 20, "exit_lookback": 10}},
        ),
    )


class HoldoutReuseError(ValueError):
    """A non-preregistered specification would be evaluated on a frozen holdout."""


def holdout_check(strategy_key, parameters, symbol, start, end, *, allow=False):
    """Return overlap disclosures; refuse unacknowledged non-preregistered reuse."""
    overlaps = []
    for holdout in registered_holdouts():
        if not holdout.overlaps(symbol, start, end):
            continue
        protected = strategy_key in holdout.protected_strategies
        preregistered = holdout.preregistered.get(strategy_key) == parameters
        if protected and not preregistered and not allow:
            raise HoldoutReuseError(
                f"[{start.isoformat()}, {end.isoformat()}) overlaps the frozen "
                f"{holdout.study} holdout [{holdout.start.isoformat()}, "
                f"{holdout.end.isoformat()}); {strategy_key} parameters {parameters} "
                "were not preregistered for it. Evaluate new parameters outside the "
                "holdout, or pass --allow-holdout-overlap to acknowledge that the "
                "result must not be used for parameter selection."
            )
        overlaps.append(
            {
                "study": holdout.study,
                "start": holdout.start,
                "end": holdout.end,
                "preregistered_specification": preregistered if protected else None,
                "acknowledged_reuse": bool(protected and not preregistered),
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
