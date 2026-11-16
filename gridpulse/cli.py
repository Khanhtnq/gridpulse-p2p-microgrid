"""GridPulse Command Line Interface.

Provides simulation and optimization commands for Ghana microgrids:
- simulate-accra-feeder: Runs AI peak-load forecasting and Dumsor risk detection
- optimize-home: Runs exact DP appliance load-shedding and battery switchover
- match-p2p: Simulates neighborhood P2P double auction matching
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from gridpulse.forecaster import FeederLoadForecaster
from gridpulse.p2p_matcher import MicrogridOrderbook, Order
from gridpulse.scheduler import BatteryState, GridStatus, HouseholdScheduler


def cmd_simulate_accra_feeder(args: argparse.Namespace) -> None:
    forecaster = FeederLoadForecaster(feeder_capacity_kw=args.capacity)
    hours, temps, kw = forecaster.generate_accra_telemetry(days=args.days)
    metrics = forecaster.train(hours, temps, kw)

    # Next 24h temperature forecast based on typical Accra diurnal curve
    temps_24h = [25.0 + 7.0 * (1.0 + math_sin((h - 8.0) * 3.14159 / 12.0)) / 2.0 for h in range(24)]
    result = forecaster.predict_24h(temps_24h)

    output: dict[str, Any] = {
        "feeder_capacity_kw": result.feeder_capacity_kw,
        "max_predicted_demand_kw": result.max_demand_kw,
        "peak_hours_window_gmt": f"{result.peak_hours_window[0]:02d}:00 - {result.peak_hours_window[1]:02d}:00",
        "dumsor_alert": result.dumsor_alert,
        "training_metrics": metrics,
        "hourly_forecast_kw": result.predicted_demand_kw,
        "overload_probabilities": result.overload_probability,
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("=== GridPulse AI Dumsor Feeder Simulation (Accra/Kumasi) ===")
        print(f"Feeder Transformer Capacity: {result.feeder_capacity_kw} kW")
        print(f"Peak Predicted Demand:       {result.max_demand_kw} kW")
        print(f"Peak Risk Window:            {output['peak_hours_window_gmt']}")
        print(f"Dumsor Alert Triggered:      {result.dumsor_alert}")
        print(f"Model Training Accuracy:     MAE={metrics['mae']} kW, RMSE={metrics['rmse']} kW, R2={metrics['r2']}")


def math_sin(val: float) -> float:
    import math

    return math.sin(val)


def cmd_optimize_home(args: argparse.Namespace) -> None:
    if args.outage:
        status = GridStatus.DUMSOR_OUTAGE
    elif args.peak:
        status = GridStatus.PEAK_TARIFF
    else:
        status = GridStatus.GRID_NORMAL

    battery = BatteryState(
        soc_wh=args.battery_soc * 5000.0,
        capacity_wh=5000.0,
    )

    scheduler = HouseholdScheduler()
    res = scheduler.optimize(
        grid_status=status,
        solar_pv_watts=args.solar_watts,
        battery=battery,
        smart_meter_watt_cap=args.budget_watts,
    )

    output: dict[str, Any] = {
        "grid_status": status.value,
        "switchover_mode": res.switchover_mode.value,
        "total_power_available_watts": res.total_power_available_watts,
        "critical_load_watts": res.critical_load_watts,
        "scheduled_load_watts": res.scheduled_load_watts,
        "active_appliances": res.active_appliances,
        "shed_appliances": res.shed_appliances,
        "total_utility_score": res.total_utility_score,
        "critical_deficit": res.critical_deficit,
        "solar_watts_used": res.solar_watts_used,
        "solar_watts_exported": res.solar_watts_exported,
        "battery_watts_flow": res.battery_watts_flow,
        "next_battery_soc_wh": res.next_battery_soc_wh,
        "next_battery_soc_pct": res.next_battery_soc_pct,
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("=== GridPulse Smart Load-Shedding & Switchover ===")
        print(f"Grid Status:        {status.value}")
        print(f"Switchover Mode:    {res.switchover_mode.value}")
        print(f"Scheduled Load:     {res.scheduled_load_watts} W / {res.total_power_available_watts} W available")
        print(f"Active Appliances:  {', '.join(res.active_appliances)}")
        print(f"Shed Appliances:    {', '.join(res.shed_appliances)}")
        print(f"Next Battery SoC:   {res.next_battery_soc_pct}% ({res.next_battery_soc_wh} Wh)")


def cmd_match_p2p(args: argparse.Namespace) -> None:
    orderbook = MicrogridOrderbook(protocol_fee_bps=200)

    # Neighborhood Accra Prosumer Asks and Consumer Bids
    asks = [
        Order(order_id="ASK-01", trader_id="PROSUMER-A1", wh_amount=3500, price_ghs_per_kwh=1.20),
        Order(order_id="ASK-02", trader_id="PROSUMER-A2", wh_amount=5000, price_ghs_per_kwh=1.35),
        Order(order_id="ASK-03", trader_id="PROSUMER-A3", wh_amount=2000, price_ghs_per_kwh=1.50),
    ]

    bids = [
        Order(order_id="BID-01", trader_id="CONSUMER-B1", wh_amount=4000, price_ghs_per_kwh=1.80),
        Order(order_id="BID-02", trader_id="CONSUMER-B2", wh_amount=3000, price_ghs_per_kwh=1.65),
        Order(order_id="BID-03", trader_id="CONSUMER-B3", wh_amount=4500, price_ghs_per_kwh=1.40),
    ]

    summary = orderbook.match_orders(asks, bids, pricing_mode="midpoint")

    output: dict[str, Any] = {
        "total_kwh_matched": summary.total_kwh_matched,
        "total_volume_ghs": summary.total_volume_ghs,
        "average_clearing_price_ghs_per_kwh": summary.average_clearing_price_ghs_per_kwh,
        "prosumer_revenue_ghs": summary.total_prosumer_revenue_ghs,
        "protocol_fees_ghs": summary.total_protocol_fees_ghs,
        "buyer_savings_vs_peak_ghs": summary.total_buyer_savings_vs_peak_ghs,
        "buyer_savings_vs_diesel_ghs": summary.total_buyer_savings_vs_diesel_ghs,
        "diesel_co2_kg_avoided": summary.total_diesel_co2_kg_avoided,
        "num_trades": len(summary.trades),
        "trades": [
            {
                "seller": t.seller_id,
                "buyer": t.buyer_id,
                "wh": t.wh_cleared,
                "price": t.clearing_price_ghs_per_kwh,
                "cost_ghs": t.total_cost_ghs,
                "co2_avoided_kg": t.diesel_co2_kg_avoided,
            }
            for t in summary.trades
        ],
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("=== GridPulse P2P Double Auction Clearing ===")
        print(f"Energy Cleared:    {summary.total_kwh_matched} kWh ({summary.total_wh_matched} Wh)")
        print(f"Total Traded:      {summary.total_volume_ghs} GHS")
        print(f"Average Price:     {summary.average_clearing_price_ghs_per_kwh} GHS/kWh")
        print(f"Prosumer Revenue:  {summary.total_prosumer_revenue_ghs} GHS")
        print(f"Buyer Peak Saving: {summary.total_buyer_savings_vs_peak_ghs} GHS")
        print(f"Buyer Diesel Save: {summary.total_buyer_savings_vs_diesel_ghs} GHS")
        print(f"Diesel CO2 Avoid:  {summary.total_diesel_co2_kg_avoided} kg")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gridpulse", description="GridPulse Accra/Kumasi P2P Microgrid CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # simulate-accra-feeder
    p_sim = subparsers.add_parser("simulate-accra-feeder", help="Simulate Accra feeder demand and Dumsor risk")
    p_sim.add_argument("--days", type=int, default=14, help="Historical training days")
    p_sim.add_argument("--capacity", type=float, default=800.0, help="Feeder rating in kW")
    p_sim.add_argument("--json", action="store_true", help="Output as JSON")
    p_sim.set_defaults(func=cmd_simulate_accra_feeder)

    # optimize-home
    p_opt = subparsers.add_parser("optimize-home", help="Optimize home load shedding and battery dispatch")
    p_opt.add_argument("--budget-watts", type=float, default=2000.0, help="Smart meter watt cap")
    p_opt.add_argument("--solar-watts", type=float, default=850.0, help="Solar PV generation watts")
    p_opt.add_argument("--battery-soc", type=float, default=0.65, help="Battery state of charge (0.0 to 1.0)")
    p_opt.add_argument("--outage", action="store_true", help="Simulate Dumsor blackout")
    p_opt.add_argument("--peak", action="store_true", help="Simulate peak tariff period")
    p_opt.add_argument("--json", action="store_true", help="Output as JSON")
    p_opt.set_defaults(func=cmd_optimize_home)

    # match-p2p
    p_match = subparsers.add_parser("match-p2p", help="Simulate neighborhood P2P double auction clearing")
    p_match.add_argument("--json", action="store_true", help="Output as JSON")
    p_match.set_defaults(func=cmd_match_p2p)

    parsed_args = parser.parse_args()
    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()
