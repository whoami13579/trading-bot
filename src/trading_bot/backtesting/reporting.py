"""Versioned reports, exact Decimal JSON/CSV and a standalone interactive equity chart."""

from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import uuid
from .cache import safe_directory
from .models import dec, json_value


TIMEFRAME_LABELS = {
    "MINUTE": "1m",
    "MINUTE_5": "5m",
    "MINUTE_15": "15m",
    "MINUTE_30": "30m",
    "HOUR": "1H",
    "HOUR_4": "4H",
    "DAY": "1D",
}
ENVIRONMENT = "DEMO (simulated only)"


def build_report(
    result,
    stats,
    dataset,
    *,
    adapter,
    strategy,
    execution,
    data_source,
    holdouts=(),
    reproducibility=None,
):
    """One report contract for every strategy and execution model.

    Pre-existing HMA keys and meanings are unchanged; later keys are additive.
    """
    from .engine import pnl_decomposition

    ledger = result.ledger
    unmodeled = execution.unmodeled(dataset)
    pnl = pnl_decomposition(result)
    modeled = pnl["modeled_costs"]
    report = {
        "schema_version": 1,
        "strategy": result.strategy,
    }
    if adapter.key == "hma":  # Original HMA report keys, retained for compatibility.
        report["hma_period"] = strategy.period
        report["direction"] = strategy.direction
    report.update(
        {
            "symbol": dataset.symbol,
            "source": dataset.source,
            "timeframe": dataset.timeframe,
            "performance_start": result.start,
            "performance_end": result.end,
            "historical_days": (result.end - result.start).total_seconds() / 86400,
            "candles": result.candles,
            "configuration": execution.configuration(),
            "data": dataset.metadata,
            "statistics": stats,
            "open_positions": [asdict(ledger.position)] if ledger.position else [],
            "trades": [asdict(t) for t in ledger.trades],
            "rejected_entries": ledger.rejections,
            "equity_curve": result.equity_curve,
            "decisions": result.decisions,
            "execution": execution.description,
            "unmodeled_costs": unmodeled,
            "total_cost_estimate": None if unmodeled else modeled,
            "assumptions": execution.assumptions(),
            # Additive, strategy-neutral fields.
            "strategy_key": adapter.key,
            "strategy_description": adapter.describe(strategy),
            "parameters": adapter.values(strategy),
            "signal_price": adapter.signal_price,
            "environment": ENVIRONMENT,
            "data_source": data_source,
            "quote_basis": execution.spread_basis(dataset),
            "execution_model": execution.name,
            "execution_policy": asdict(execution.policy),
            "assumed_costs": execution.assumed(dataset),
            "summary": {
                # See engine.pnl_decomposition for the exact definitions.
                "gross_pnl": pnl["gross_pnl"],
                "realized_gross_pnl": pnl["realized_gross_pnl"],
                "unrealized_gross_pnl": pnl["unrealized_gross_pnl"],
                "net_pnl": stats["net_pnl"],
                "realized_cash_pnl": stats["realized_pnl"],
                "unrealized_pnl": stats["unrealized_pnl"],
                "modeled_costs": modeled,
                "open_entry_spread_and_slippage": pnl["open_entry_spread_and_slippage"],
                "open_valuation_spread": pnl["open_valuation_spread"],
                "modeled_cash_costs": ledger.costs,
                "embedded_spread": ledger.embedded_spread,
                "embedded_slippage": ledger.embedded_slippage,
                "last_candle_end": result.last_candle.end if result.last_candle else None,
            },
            "trade_details": result.trade_records,
            "holdout_overlap": list(holdouts),
            "reproducibility": reproducibility or {},
            "notes": execution.notes(dataset),
        }
    )
    return report


def save_report(report, root):
    root = safe_directory(root)
    destination = root / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "report.json").write_text(
        json.dumps(report, default=json_value, indent=2, allow_nan=False) + "\n"
    )
    fields = [
        "entry_timestamp",
        "exit_timestamp",
        "direction",
        "size",
        "entry_price",
        "exit_price",
        "gross_pnl",
        "costs",
        "net_pnl",
        "balance_after_closing",
    ]
    details = report.get("trade_details") or []
    detail_fields = fields + [
        "entry_signal_time",
        "exit_signal_time",
        "boundary_exit",
        "midpoint_gross_pnl",
        "spread_cost",
        "slippage_cost",
        "financing_cost",
        "commission_cost",
        "total_cost",
    ]
    for filename, rows, names in [
        ("trades.csv", report["trades"], fields),
        ("trade_details.csv", details, detail_fields),
        (
            "equity.csv",
            report["equity_curve"],
            [
                "timestamp",
                "phase",
                "balance",
                "equity",
                "unrealized_pnl",
                "used_margin",
                "available_funds",
                "mark_price",
                "quote_timestamp",
            ],
        ),
    ]:
        with (destination / filename).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        k: json_value(v) if hasattr(v, "isoformat") else v
                        for k, v in row.items()
                    }
                )
    points = [
        {"time": p["timestamp"].isoformat(), "equity": float(p["equity"])}
        for p in report["equity_curve"]
    ]
    html = """<!doctype html><html><meta charset="utf-8"><title>Backtest equity</title>
<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#101827;color:#e2e8f0}canvas{width:100%;height:420px;background:#172237}p{color:#b8c7d8}</style>
<h1>Historical simulated equity (USD)</h1><p>Observed candle opens/closes; unknown costs remain unmodeled. Hover for values.</p><canvas id="chart"></canvas><p id="value"></p><script>
const data=POINTS, c=document.getElementById('chart'),ctx=c.getContext('2d'),out=document.getElementById('value');
function draw(){c.width=c.clientWidth*devicePixelRatio;c.height=420*devicePixelRatio;ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);let w=c.clientWidth,h=420;ctx.clearRect(0,0,w,h);let lo=data.reduce((v,p)=>Math.min(v,p.equity),Infinity),hi=data.reduce((v,p)=>Math.max(v,p.equity),-Infinity);if(hi===lo){hi+=1;lo-=1;}ctx.strokeStyle='#43d9ac';ctx.lineWidth=2;ctx.beginPath();data.forEach((p,i)=>{let x=55+i/Math.max(1,data.length-1)*(w-75),y=380-(p.equity-lo)/(hi-lo)*340;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.fillStyle='#d1dce8';ctx.fillText(hi.toFixed(2),5,30);ctx.fillText(lo.toFixed(2),5,385);}
c.onmousemove=e=>{let i=Math.round((e.offsetX-55)/(c.clientWidth-75)*(data.length-1));i=Math.max(0,Math.min(data.length-1,i));out.textContent=data[i].time+' — $'+data[i].equity.toFixed(4);};window.onresize=draw;draw();
</script></html>""".replace("POINTS", json.dumps(points))
    (destination / "equity.html").write_text(html)
    return destination


def money(value):
    return f"${value:,.4f}"


def pct(value):
    return "unavailable" if value is None else f"{value:.4f}%"


def print_report_table(lines, note):
    """Common terminal layout; callers supply strategy-specific fields and notes."""
    print("========== BACKTEST REPORT ==========")
    for name, value in lines:
        print(f"{name + ':':24} {value}")
    print(note)
    print("=====================================")


def units(value):
    return format(dec(value).normalize(), "f")


def report_lines(report, path=None):
    """Standard fields in a fixed order, then model/data-specific details."""
    stats = report["statistics"]
    summary = report["summary"]
    data = report["data"]
    profit = stats["profit_factor"]
    factor = f"{profit:.4f}" if profit is not None else stats["profit_factor_status"]
    cached = "cached " if report["data_source"] == "snapshot" else ""
    timeframe = report["timeframe"]
    lines = [
        ("Strategy", report["strategy_description"]),
        ("Symbol", report["symbol"]),
        ("Size", f"{units(report['configuration']['size'])} EUR"),
        ("Environment", report["environment"]),
        ("Data source", f"{report['source']} ({cached}{report['quote_basis']})"),
        ("Timeframe", f"{timeframe} ({TIMEFRAME_LABELS.get(timeframe, timeframe)})"),
        ("Historical days", str(report["historical_days"])),
        (
            "Period",
            f"{report['performance_start'].isoformat()} to "
            f"{report['performance_end'].isoformat()} (UTC, end exclusive)",
        ),
        ("Candles", str(report["candles"])),
        ("Warm-up candles", str(data.get("warmup_candles", "unavailable"))),
        ("Initial balance", money(stats["initial_balance"])),
        ("Final balance", money(stats["final_balance"])),
        ("Final equity", money(stats["final_equity"])),
        ("Gross P&L", money(summary["gross_pnl"])),
        ("Net P&L", money(stats["net_pnl"])),
        ("Total return", pct(stats["total_return_pct"])),
        ("Total closed trades", str(stats["total_trades"])),
        ("Winning trades", str(stats["winning_trades"])),
        ("Losing trades", str(stats["losing_trades"])),
        ("Win rate", pct(stats["win_rate_pct"])),
        ("Profit factor", factor),
        (
            "Max drawdown",
            money(stats["max_drawdown"]) + " / " + pct(stats["max_drawdown_pct"]),
        ),
        ("Realized cash P&L", money(stats["realized_pnl"])),
        ("Unrealized P&L", money(stats["unrealized_pnl"])),
        ("Modeled costs", money(summary["modeled_costs"])),
        (
            "Unmodeled costs",
            ", ".join(report["unmodeled_costs"]) or "none of the supported assumptions",
        ),
        ("Assumed costs", "; ".join(report["assumed_costs"]) or "none"),
        ("Remaining positions", str(stats["remaining_positions"])),
        ("Available funds", money(stats["available_funds"])),
        # Model- and data-specific details.
        ("Execution model", report["execution_model"]),
    ]
    lines += [
        ("Realized gross P&L", money(summary["realized_gross_pnl"])),
        ("Unrealized gross P&L", money(summary["unrealized_gross_pnl"])),
        ("Open valuation spread", money(summary["open_valuation_spread"])),
    ]
    if summary["last_candle_end"] is not None:
        lines.append(("Last candle end", summary["last_candle_end"].isoformat()))
    if "missing_intervals" in data:  # Provider coverage audit.
        lines += [
            ("Missing target intervals", str(data["missing_intervals"])),
            ("Declared no-quote intervals", str(data["provider_no_quote_intervals"])),
            ("Unexplained intervals", str(data["unexplained_missing_intervals"])),
            ("Sparse source slots", str(data["missing_source_slots_in_existing_candles"])),
        ]
    if "missing_active_30m_bars" in data:  # Snapshot coverage audit.
        lines += [
            ("Missing 30m source bars", str(data["missing_active_30m_bars"])),
            ("Excluded aggregates", str(data["discarded_incomplete_aggregates"])),
            ("Unexpected gap events", str(data["unexpected_gap_events"])),
        ]
    if "boundary_exits" in stats:  # Research-window execution.
        lines += [
            ("Boundary exits", str(stats["boundary_exits"])),
            ("Cancelled gap entries", str(stats["cancelled_gap_entries"])),
            ("Gap holding events", str(stats["gap_holding_events"])),
            ("Exposure", pct(stats["exposure_pct"])),
        ]
    lines += [
        (
            "Estimated total costs",
            "unavailable"
            if report["total_cost_estimate"] is None
            else money(report["total_cost_estimate"]),
        ),
        ("Modeled cash costs", money(stats["known_cash_costs"])),
        ("Remaining base units", str(stats["remaining_base_units"])),
        ("Remaining USD notional", money(stats["remaining_notional_usd"])),
        ("Rejected entries", str(stats["rejected_entries"])),
    ]
    for overlap in report["holdout_overlap"]:
        lines.append(
            (
                "Holdout overlap",
                f"{overlap['study']} [{overlap['start'].isoformat()}, "
                f"{overlap['end'].isoformat()}); not for parameter selection",
            )
        )
    if path is not None:
        lines.append(("Results", str(path)))
    return lines


def print_report(report, path=None):
    print_report_table(report_lines(report, path), report["notes"])
