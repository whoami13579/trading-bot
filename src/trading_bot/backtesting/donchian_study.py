"""Reproducible staged research CLI. Every fill is simulated; no broker imports."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from trading_bot.strategies.donchian import DonchianStrategy
from .strategy import HmaStrategy
from .models import json_value
from .study_data import START, END, read_snapshot
from .study_engine import COSTS, prepare_frames, simulate

ROOT = Path(__file__).resolve().parents[3]
PLAN = ROOT / "research/donchian/PLAN.md"
SETTINGS = ((20, 10), (15, 10), (25, 10), (20, 8), (20, 12))


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=json_value, sort_keys=True, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_hashes():
    paths = [*Path(__file__).parent.glob("*.py"),
             ROOT / "src/trading_bot/strategies/donchian.py",
             ROOT / "src/trading_bot/strategies/indicators.py"]
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths)}


def splits():
    days = (END - START).days
    edges = [START + timedelta(days=round(days * f)) for f in (.4, .5, .6, .7, .8, 1)]
    windows = [{"name": f"validation_{i+1}", "train_start": START,
                "train_end": edges[i], "start": edges[i], "end": edges[i+1]}
               for i in range(4)]
    windows.append({"name": "holdout", "start": edges[4], "end": END})
    return windows


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def concentration(trades):
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


def prepare(snapshot, output):
    if (output / "study.json").exists():
        raise ValueError("Study already prepared; choose a new output directory for reproduction")
    candles, metadata = read_snapshot(snapshot)
    _, audit = prepare_frames(candles, START, END)
    manifest = {"recorded_at": datetime.now(timezone.utc), "plan_sha256": digest(PLAN),
                "code_sha256": code_hashes(), "data": metadata,
                "snapshot": str(snapshot.resolve()), "start": START, "end": END,
                "windows": splits(), "primary_timeframe": "HOUR",
                "settings": SETTINGS, "costs": [asdict(c) for c in COSTS],
                "symbol": "EURUSD", "size": 100, "environment": "DEMO",
                "capital_demo_attempt": "2026-11-13: historical DEMO request failed HTTP 404; no orders",
                "coverage": audit}
    dump(output / "study.json", manifest)
    print("Prepared data/splits only; no performance calculated", flush=True)


def load_prepared(output):
    study = json.loads((output / "study.json").read_text())
    if study["plan_sha256"] != digest(PLAN) or study["code_sha256"] != code_hashes():
        raise ValueError("Plan/code changed since preparation; use a new study directory")
    candles, metadata = read_snapshot(study["snapshot"])
    if metadata != study["data"]:
        raise ValueError("Data changed since preparation")
    frames, _ = prepare_frames(candles, START, END)
    return study, frames


class Cash:
    name = "Cash"
    warmup = 1

    def target(self, candles):
        return None


def configurations(stage):
    for tf in ("HOUR", "MINUTE_30", "HOUR_4"):
        for entry, exit in SETTINGS if stage == "development" else SETTINGS[:1]:
            for cost in COSTS if (entry, exit) == (20, 10) else COSTS[:2]:
                yield tf, f"donchian_{entry}_{exit}", DonchianStrategy(entry, exit), cost
        for cost in COSTS:
            yield tf, "hma_16_buy", HmaStrategy(), cost
        yield tf, "cash", Cash(), COSTS[0]


def run_stage(output, stage):
    study, frames = load_prepared(output)
    marker = output / f"{stage}_started.json"
    if marker.exists():
        raise ValueError(f"{stage} already attempted; inspect registry, do not re-evaluate holdout")
    if stage == "holdout":
        freeze = json.loads((output / "freeze.json").read_text())
        if freeze["study_sha256"] != digest(output / "study.json"):
            raise ValueError("Frozen study changed")
        if freeze["development_sha256"] != digest(output / "development.json"):
            raise ValueError("Development results changed after freeze")
    windows = splits()[:4] if stage == "development" else splits()[4:]
    dump(marker, {"started_at": datetime.now(timezone.utc), "stage": stage})
    rows = []
    for tf, label, strategy, case in configurations(stage):
        for window in windows:
            run_id = f"{window['name']}-{tf}-{label}-{case.name}"
            path = output / "runs" / run_id
            path.mkdir(parents=True, exist_ok=False)
            config = {"id": run_id, "stage": stage, "timeframe": tf, "strategy": label,
                      "cost": case.name, "window": window["name"], "start": window["start"],
                      "end": window["end"], "size": 100, "symbol": "EURUSD"}
            # Persist before evaluation so failures and the full search extent are visible.
            with (output / "attempts.jsonl").open("a") as f:
                f.write(json.dumps(dict(config, status="started"), default=json_value) + "\n")
            dump(path / "configuration.json", config)
            try:
                stats, trades, curve = simulate(frames[tf], strategy, case, window["start"], window["end"])
                dump(path / "metrics.json", dict(stats, concentration=concentration(trades)))
                write_csv(path / "trades.csv", trades)
                write_csv(path / "equity.csv", curve)
            except Exception as exc:
                with (output / "attempts.jsonl").open("a") as f:
                    f.write(json.dumps(dict(config, status="failed", error=type(exc).__name__),
                                       default=json_value) + "\n")
                raise
            rows.append(dict(config, **stats))
            with (output / "attempts.jsonl").open("a") as f:
                f.write(json.dumps(dict(config, status="completed"), default=json_value) + "\n")
        print(f"{stage}: {tf} {label} {case.name} complete", flush=True)
    dump(output / f"{stage}.json", rows)
    write_csv(output / f"{stage}.csv", rows)
    print(f"{stage}: {len(rows)} prespecified runs", flush=True)


def freeze(output):
    load_prepared(output)
    if (output / "freeze.json").exists() or (output / "holdout_started.json").exists():
        raise ValueError("Already frozen or holdout attempted")
    dump(output / "freeze.json", {
        "frozen_at": datetime.now(timezone.utc), "study_sha256": digest(output / "study.json"),
        "development_sha256": digest(output / "development.json"),
        "strategy": "20/10, primary 1H; no selection; all planned costs/comparators",
        "decision": "Proceed once with the preregistered holdout; no assumptions changed",
    })
    print("Strategy, data and assumptions frozen before holdout")


def aggregates(output, rows):
    groups = {}
    for row in rows:
        key = row["timeframe"], row["strategy"], row["cost"]
        groups.setdefault(key, []).append(row)
    combined = []
    for (tf, strategy, cost), group in groups.items():
        trades = []
        peak = Decimal(1000)
        dd = Decimal(0)
        dd_pct = Decimal(0)
        offset = Decimal(0)
        for row in sorted(group, key=lambda x: x["start"]):
            path = output / "runs" / row["id"]
            with (path / "trades.csv").open() as f:
                trades.extend(csv.DictReader(f))
            with (path / "equity.csv").open() as f:
                for point in csv.DictReader(f):
                    equity = Decimal(point["equity"]) + offset
                    peak = max(peak, equity)
                    dd = max(dd, peak - equity)
                    dd_pct = max(dd_pct, (peak - equity) / peak * 100)
            offset += Decimal(row["net_pnl"])
        wins = sum((Decimal(t["net_pnl"]) for t in trades if Decimal(t["net_pnl"]) > 0), Decimal(0))
        losses = -sum((Decimal(t["net_pnl"]) for t in trades if Decimal(t["net_pnl"]) < 0), Decimal(0))
        result = dict(timeframe=tf, strategy=strategy, cost=cost, trades=len(trades),
                      max_drawdown=dd, max_drawdown_pct=dd_pct,
                      profit_factor=wins / losses if losses else None,
                      exposure_pct=sum(Decimal(r["exposure_pct"]) *
                                       Decimal((datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).days)
                                       for r in group) /
                                       sum((datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).days for r in group),
                      **concentration(trades))
        for field in ("midpoint_gross_pnl", "modeled_costs", "embedded_spread_estimate", "embedded_slippage", "financing_cost"):
            result[field] = sum((Decimal(r[field]) for r in group), Decimal(0))
        combined.append(result)
    return combined


def fmt(value):
    return "—" if value is None else f"{Decimal(value):.4f}"


def report(output):
    study = json.loads((output / "study.json").read_text())
    dev = json.loads((output / "development.json").read_text())
    hold = json.loads((output / "holdout.json").read_text())
    agg = aggregates(output, dev)
    dump(output / "development_aggregates.json", agg)
    write_csv(output / "development_aggregates.csv", [dict(r, pnl_by_exit_year=json.dumps(r["pnl_by_exit_year"], default=json_value)) for r in agg])
    primary = [r for r in dev if r["timeframe"] == "HOUR" and r["strategy"] == "donchian_20_10" and r["cost"] == "base"]
    a = next(r for r in agg if r["timeframe"] == "HOUR" and r["strategy"] == "donchian_20_10" and r["cost"] == "base")
    h = next(r for r in hold if r["timeframe"] == "HOUR" and r["strategy"] == "donchian_20_10" and r["cost"] == "base")
    ah = next(r for r in agg if r["timeframe"] == "HOUR" and r["strategy"] == "donchian_20_10" and r["cost"] == "high")
    hh = next(r for r in hold if r["timeframe"] == "HOUR" and r["strategy"] == "donchian_20_10" and r["cost"] == "high")
    neighbours = [r for r in agg if r["timeframe"] == "HOUR" and r["strategy"].startswith("donchian_") and r["strategy"] != "donchian_20_10" and r["cost"] == "base"]
    criteria = {
        "3_of_4_validations_positive": sum(Decimal(r["net_pnl"]) > 0 for r in primary) >= 3,
        "development_and_holdout_net_positive": Decimal(a["net_pnl"]) > 0 and Decimal(h["net_pnl"]) > 0,
        "development_and_holdout_PF_above_1": (a["profit_factor"] or 0) > 1 and Decimal(h["profit_factor"] or 0) > 1,
        "100_development_30_holdout_trades": a["trades"] >= 100 and h["total_trades"] >= 30,
        "high_cost_development_and_holdout_positive": Decimal(ah["net_pnl"]) > 0 and Decimal(hh["net_pnl"]) > 0,
        "most_neighbours_positive": sum(Decimal(r["net_pnl"]) > 0 for r in neighbours) >= 3,
        "development_positive_without_top5": Decimal(a["net_without_top5_winners"]) > 0,
    }
    dump(output / "evaluation.json", {"criteria": criteria, "warrants_demo_forward_test": all(criteria.values())})
    text = ["# EURUSD Donchian reproducible study", "",
            f"Conclusion: {'meets' if all(criteria.values()) else 'does not meet'} the preregistered threshold for DEMO forward testing.", "",
            "100 EUR fixed size; USD 1,000 initial cash per window; all orders simulated. Cash earns zero.",
            "Dukascopy historical bid/ask, not Capital.com CFD quotes; DEMO historical request failed HTTP 404.",
            f"Source SHA256: `{study['data']['candles_sha256']}`.",
            "Common requested UTC interval [2023-11-01, 2026-11-01). Raw source includes warm-up from 2023-10-12.",
            "Each scored window starts flat and ends flat at a costed last observed close (synthetic boundary valuation).",
            "Initial 40% is unscored training/warm-up. Development aggregates chain the four validation windows only.",
            "The training interval expands; fixed rules require no fit. Warm-up uses only preceding completed bars.",
            "20/10 durations: 30m 10h/5h; primary 1H 20h/10h; 4H 80h/40h, in trading time.",
            "Base: observed spread + 0.1 pip per leg slippage, zero assumed commission, financing unmodeled (zero arithmetic).",
            "High: twice observed spread + 0.5 pip per leg, 5% annualized financing sensitivity.",
            "Financing: base execution + 5% financing. Both sides pay entry midpoint notional / 360 at NY 17:00 weekdays, Wednesday triple.",
            "These are assumptions, not historical Capital.com fees. Midpoint-only fallback would use 1/2 pip base/high full spread.",
            "Gross below is midpoint P&L; costs include spread, slippage, assumed commission and financing. Profit factor uses net trades.",
            "Exposure is calendar time including weekend holding; drawdown is observed open/close equity, not intrabar/tick maximum.", "",
            "## Exact windows", "", "| Window | Start UTC | End UTC (exclusive) |", "|---|---|---|"]
    for w in study["windows"]:
        text.append(f"| {w['name']} | {w['start']} | {w['end']} |")
    text += ["", "## Coverage", "", "| Timeframe | Bars | First start UTC | Last end UTC | Incomplete aggregates excluded | Gap events |", "|---|---:|---|---|---:|---:|"]
    for tf, c in study["coverage"]["timeframes"].items():
        text.append(f"| {tf} | {c['bars']} | {c['first_start']} | {c['last_end']} | {c['discarded_incomplete_aggregates']} | {c['unexpected_gap_count']} |")
    text += ["", f"Missing active 30m bars: {study['coverage']['missing_active_30m_bars']}; sparse minute-derived 30m candles: {study['coverage']['sparse_30m_source_candles']}; missing minute slots inside observed bars: {study['coverage']['missing_minute_slots_within_observed_bars']}.",
             "Full missing timestamps and coverage limitations are in study.json. Weekend edges are DST-aware; exceptional holidays are flagged as unexpected gaps. No forward filling.",
             "Aggregates missing an expected 30m constituent are excluded; sparse observed source bars remain, with their actual first observation time. OHLC cannot prove tick completeness.",
             "Unexpected active gaps cancel pending entries, preserve held exits, reset channel warm-up, and carry positions to the next quote; weekend closures retain signals. Missing minute slots within a bar are disclosed but do not reset the channel.", "",
             "## Primary rules by window, including final holdout", "",
             "| TF | Window | Cost | Trades | Gross USD | Costs USD | Net USD | Max DD USD | PF | Exposure % |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in dev + hold:
        if r["strategy"] == "donchian_20_10":
            text.append(f"| {r['timeframe']} | {r['window']} | {r['cost']} | {r['total_trades']} | {fmt(r['midpoint_gross_pnl'])} | {fmt(r['modeled_costs'])} | {fmt(r['net_pnl'])} | {fmt(r['max_drawdown'])} | {fmt(r['profit_factor'])} | {fmt(r['exposure_pct'])} |")
    text += ["", "## All development specifications and comparators", "", "All four validations pooled; no nearby parameter was run on holdout.", "",
             "| TF | Strategy | Cost | Trades | Gross | Costs | Net | Max DD | PF | Exposure % | Net excluding top 5 winners |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in agg:
        text.append(f"| {r['timeframe']} | {r['strategy']} | {r['cost']} | {r['trades']} | {fmt(r['midpoint_gross_pnl'])} | {fmt(r['modeled_costs'])} | {fmt(r['net_pnl'])} | {fmt(r['max_drawdown'])} | {fmt(r['profit_factor'])} | {fmt(r['exposure_pct'])} | {fmt(r['net_without_top5_winners'])} |")
    text += ["", "## Holdout comparators", "", "HMA16 BUY/flat uses unchanged ask-close slope rules, adapted to completed bars and this study's common execution model.", "",
             "| TF | Strategy | Cost | Trades | Gross | Costs | Net | Max DD | PF | Exposure % |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in hold:
        if r["strategy"] != "donchian_20_10":
            text.append(f"| {r['timeframe']} | {r['strategy']} | {r['cost']} | {r['total_trades']} | {fmt(r['midpoint_gross_pnl'])} | {fmt(r['modeled_costs'])} | {fmt(r['net_pnl'])} | {fmt(r['max_drawdown'])} | {fmt(r['profit_factor'])} | {fmt(r['exposure_pct'])} |")
    text += ["", "## Primary 1H P&L concentration", "", "| Sample | Net | Top winner | Top 5 winners | Top 5 / positive P&L % | Net without top 5 | Exit-year P&L |", "|---|---:|---:|---:|---:|---:|---|"]
    for label, c in (("development", a), ("holdout", concentration(list(csv.DictReader((output / "runs" / h["id"] / "trades.csv").open()))))):
        text.append(f"| {label} | {fmt(c['net_pnl'])} | {fmt(c['top_winner'])} | {fmt(c['top5_winners'])} | {fmt(c['top5_share_of_positive_pnl_pct'])} | {fmt(c['net_without_top5_winners'])} | {', '.join(f'{y}: {fmt(p)}' for y,p in c['pnl_by_exit_year'].items())} |")
    text += ["", "## Fixed evaluation criteria", ""]
    text += [f"- {k}: {'PASS' if v else 'FAIL'}" for k, v in criteria.items()]
    text += ["", f"Search extent: {len(dev)} development configurations/windows and {len(hold)} holdout configurations; attempts.jsonl records every attempt. Freeze precedes the single holdout stage. Weak runs are retained.",
             "All per-window HMA, cash and nearby-setting results are in development.csv; holdout.csv is separate. Each runs/ directory contains exact configuration, metrics, simulated trades and equity CSVs.",
             "Reports give stale boundary quote ages, gap holding events and cancelled entries per run. Read study.json, freeze.json and PLAN.md with these results.",
             "This descriptive backtest does not establish statistical significance or live execution feasibility."]
    (output / "RESULTS.md").write_text("\n".join(text) + "\n")
    print(json.dumps({"criteria": criteria, "warrants_demo_forward_test": all(criteria.values())}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "development", "freeze", "holdout", "report"))
    parser.add_argument("--output", type=Path, default=Path("backtests/donchian-eurusd"))
    parser.add_argument("--snapshot", type=Path, default=Path(".cache/donchian/eurusd-30m.json.gz"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage == "prepare":
        prepare(args.snapshot, args.output)
    elif args.stage in ("development", "holdout"):
        run_stage(args.output, args.stage)
    elif args.stage == "freeze":
        freeze(args.output)
    else:
        report(args.output)


if __name__ == "__main__":
    main()
