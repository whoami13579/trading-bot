"""Versioned reports, exact Decimal JSON/CSV and a standalone interactive equity chart."""

from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import uuid
from .cache import safe_directory
from .engine import statistics
from .models import json_value


def make_report(result, dataset, *, period, direction):
    config = asdict(result.config)
    missing = [
        name
        for name in ("commission_per_order", "slippage_pips", "financing_per_unit_day")
        if config[name] is None
    ]
    if not dataset.candles[0].bid:
        missing.append("historical_bid_ask_spread")
    return {
        "schema_version": 1,
        "strategy": result.strategy,
        "hma_period": period,
        "direction": direction,
        "symbol": dataset.symbol,
        "source": dataset.source,
        "timeframe": dataset.timeframe,
        "performance_start": result.start,
        "performance_end": result.end,
        "historical_days": (result.end - result.start).total_seconds() / 86400,
        "candles": result.candles,
        "configuration": config,
        "data": dataset.metadata,
        "statistics": statistics(result),
        "open_positions": [asdict(result.ledger.position)]
        if result.ledger.position
        else [],
        "trades": [asdict(t) for t in result.ledger.trades],
        "rejected_entries": result.ledger.rejections,
        "equity_curve": result.equity_curve,
        "decisions": result.decisions,
        "execution": "completed close decision; next available quoted open; no final forced liquidation",
        "unmodeled_costs": missing,
        "total_cost_estimate": None
        if missing
        else result.ledger.costs
        + result.ledger.embedded_slippage
        + result.ledger.embedded_spread,
        "assumptions": {
            "account_currency": "USD",
            "contract": "EURUSD base-currency units; 1 EUR per unit",
            "margin": "fixed entry notional / configured leverage, collateral only, not an expense",
            "financing": "configured linear USD/unit/calendar-day debit, not a broker swap schedule",
            "liquidation": "no assumed broker stop-out; report negative available funds and reject new entries",
            "valuation": "last observed liquidation-side close; open/close drawdown excludes unseen intrabar paths",
            "spread_and_slippage": "embedded in execution prices; never deducted again; spread estimate sums executed-leg half-spreads, excludes future liquidation",
            "balance": "initial + realized net cash P&L, including paid costs of any still-open position",
            "unknown_costs": "not charged in arithmetic; explicitly unmodeled, never asserted to be zero",
        },
    }


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
    for filename, rows, names in [
        ("trades.csv", report["trades"], fields),
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


def print_report(report, path):
    stats = report["statistics"]

    def money(v):
        return f"${v:,.4f}"

    def pct(v):
        return "unavailable" if v is None else f"{v:.4f}%"

    profit = stats["profit_factor"]
    factor = f"{profit:.4f}" if profit is not None else stats["profit_factor_status"]
    lines = [
        (
            "Strategy",
            f"HMA ({report['direction']} / flat), period {report['hma_period']}",
        ),
        ("Symbol", report["symbol"]),
        ("Data source", report["source"]),
        ("Timeframe", report["timeframe"]),
        ("Historical days", str(report["historical_days"])),
        (
            "Period",
            f"{report['performance_start'].isoformat()} to {report['performance_end'].isoformat()}",
        ),
        ("Candles", str(report["candles"])),
        ("Warm-up candles", str(report["data"]["warmup_candles"])),
        ("Missing target intervals", str(report["data"]["missing_intervals"])),
        (
            "Declared no-quote intervals",
            str(report["data"]["provider_no_quote_intervals"]),
        ),
        ("Unexplained intervals", str(report["data"]["unexplained_missing_intervals"])),
        (
            "Sparse source slots",
            str(report["data"]["missing_source_slots_in_existing_candles"]),
        ),
        ("Initial balance", money(stats["initial_balance"])),
        ("Final balance", money(stats["final_balance"])),
        ("Final equity", money(stats["final_equity"])),
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
        (
            "Estimated total costs",
            "unavailable"
            if report["total_cost_estimate"] is None
            else money(report["total_cost_estimate"]),
        ),
        ("Modeled cash costs", money(stats["known_cash_costs"])),
        (
            "Unmodeled costs",
            ", ".join(report["unmodeled_costs"]) or "none of the supported assumptions",
        ),
        ("Remaining positions", str(stats["remaining_positions"])),
        ("Remaining base units", str(stats["remaining_base_units"])),
        ("Remaining USD notional", money(stats["remaining_notional_usd"])),
        ("Available funds", money(stats["available_funds"])),
        ("Rejected entries", str(stats["rejected_entries"])),
        ("Results", str(path)),
    ]
    print("========== BACKTEST REPORT ==========")
    for name, value in lines:
        print(f"{name + ':':24} {value}")
    print(
        "Costs unavailable from the provider are unmodeled; results are not guaranteed net of all trading costs."
    )
    print("=====================================")
