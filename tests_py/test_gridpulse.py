"""Unit and Invariant Test Suite for GridPulse P2P Protocol.

Covers:
- Multi-harmonic Fourier load forecasting and Dumsor risk detection
- Exact 0-1 Dynamic Programming knapsack load-shedding vs brute-force combinatorial search
- Household critical appliance invariants & battery SoC conservation
- P2P Double Auction clearing, zero negative spreads, and emissions displacement
- CLI end-to-end JSON execution
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys

import pytest

from gridpulse.forecaster import FeederLoadForecaster
from gridpulse.p2p_matcher import MicrogridOrderbook, Order
from gridpulse.scheduler import (
    Appliance,
    BatteryState,
    GridStatus,
    HouseholdScheduler,
    SwitchoverMode,
)

# =====================================================================
# 1. AI Diurnal Feeder Forecaster Tests
# =====================================================================


def test_forecaster_feature_extraction_dimensions_and_periodicity() -> None:
    feats_0h = FeederLoadForecaster.extract_features(0.0, 28.0)
    feats_24h = FeederLoadForecaster.extract_features(24.0, 28.0)

    assert len(feats_0h) == 8
    assert feats_0h[0] == 1.0
    # Periodicity check: 0h and 24h should have identical harmonic components
    for i in range(7):
        assert pytest.approx(feats_0h[i], abs=1e-6) == feats_24h[i]


def test_forecaster_cooling_degree_threshold() -> None:
    feats_cool = FeederLoadForecaster.extract_features(12.0, 24.5)
    feats_hot = FeederLoadForecaster.extract_features(12.0, 31.0)

    # Ambient temp below 26C should yield 0 cooling degrees
    assert feats_cool[7] == 0.0
    # Ambient temp 31C should yield exactly 5.0 cooling degrees
    assert pytest.approx(feats_hot[7], abs=1e-5) == 5.0


def test_forecaster_train_minimum_samples_revert() -> None:
    forecaster = FeederLoadForecaster()
    with pytest.raises(ValueError, match="Need at least 8 samples"):
        forecaster.train([0.0, 1.0], [28.0, 28.5], [400.0, 420.0])


def test_forecaster_accuracy_r2_greater_than_90() -> None:
    forecaster = FeederLoadForecaster(feeder_capacity_kw=800.0)
    hours, temps, kw = forecaster.generate_accra_telemetry(days=14, seed=42)
    metrics = forecaster.train(hours, temps, kw)

    assert metrics["r2"] >= 0.90, f"R2 was {metrics['r2']}, expected >= 0.90"
    assert metrics["mae"] < 25.0, f"MAE was {metrics['mae']} kW, expected < 25.0 kW"
    assert metrics["rmse"] < 35.0, f"RMSE was {metrics['rmse']} kW, expected < 35.0 kW"


def test_forecaster_predict_24h_dimensions_and_ranges() -> None:
    forecaster = FeederLoadForecaster(feeder_capacity_kw=800.0)
    hours, temps, kw = forecaster.generate_accra_telemetry(days=14, seed=123)
    forecaster.train(hours, temps, kw)

    temps_24h = [26.0 + (i % 6) * 1.5 for i in range(24)]
    res = forecaster.predict_24h(temps_24h)

    assert len(res.predicted_demand_kw) == 24
    assert len(res.overload_probability) == 24
    assert all(d >= 0.0 for d in res.predicted_demand_kw)
    assert all(0.0 <= p <= 1.0 for p in res.overload_probability)
    assert res.peak_hours_window[0] <= res.peak_hours_window[1]


def test_forecaster_dumsor_risk_alert_trigger() -> None:
    # Use a small feeder capacity that will be overwhelmed during evening peak
    under_rated_capacity = 450.0
    forecaster = FeederLoadForecaster(feeder_capacity_kw=under_rated_capacity)
    hours, temps, kw = forecaster.generate_accra_telemetry(days=7, seed=99)
    forecaster.train(hours, temps, kw)

    hot_temps = [33.0] * 24
    res = forecaster.predict_24h(hot_temps)

    assert res.dumsor_alert is True
    assert any(p > 0.50 for p in res.overload_probability)


def test_forecaster_untrained_predict_raises_error() -> None:
    forecaster = FeederLoadForecaster()
    with pytest.raises(RuntimeError, match="Model must be trained before predicting"):
        forecaster.predict_single(12.0, 30.0)


# =====================================================================
# 2. Exact DP Knapsack & Household Scheduler Invariant Tests
# =====================================================================


def test_scheduler_critical_load_priority_invariant() -> None:
    scheduler = HouseholdScheduler()
    battery = BatteryState(soc_wh=2500.0)

    # Budget strictly enough for critical load (Medical/Fridge = 200W) plus some deferrable load
    res = scheduler.optimize(
        grid_status=GridStatus.DUMSOR_OUTAGE,
        solar_pv_watts=300.0,
        battery=battery,
        smart_meter_watt_cap=500.0,
    )

    assert "Medical/Fridge" in res.active_appliances
    assert not res.critical_deficit


def test_scheduler_critical_deficit_detection() -> None:
    scheduler = HouseholdScheduler()
    # Empty battery below reserve, zero solar, outage -> 0W available
    battery = BatteryState(soc_wh=500.0, min_reserve_pct=0.20)  # Reserve is 1000Wh
    assert battery.usable_energy_wh == 0.0

    res = scheduler.optimize(
        grid_status=GridStatus.DUMSOR_OUTAGE,
        solar_pv_watts=50.0,  # Below 200W critical Medical/Fridge load
        battery=battery,
    )

    assert res.critical_deficit is True
    assert "Medical/Fridge" in res.shed_appliances


def test_scheduler_dp_knapsack_brute_force_optimality() -> None:
    """Proves exact optimality of DP knapsack against exhaustive brute-force search."""
    deferrable_items = [
        Appliance(name="Fan", watts=150, priority=80),
        Appliance(name="Laptop", watts=120, priority=85),
        Appliance(name="Lights", watts=80, priority=90),
        Appliance(name="Pump", watts=600, priority=60),
        Appliance(name="Washer", watts=450, priority=45),
        Appliance(name="AC", watts=1200, priority=40),
    ]

    budget_watts = 750  # 750W knapsack capacity

    # 1. DP solution
    dp_chosen, _ = HouseholdScheduler._solve_01_knapsack(deferrable_items, budget_watts)
    dp_total_utility = sum(item.priority for item in dp_chosen)
    dp_total_weight = sum(item.watts for item in dp_chosen)

    assert dp_total_weight <= budget_watts

    # 2. Exhaustive Brute-Force search over all 2^N subsets
    best_brute_utility = 0
    best_brute_weight = 0

    for r in range(len(deferrable_items) + 1):
        for combo in itertools.combinations(deferrable_items, r):
            wt = sum(item.watts for item in combo)
            val = sum(item.priority for item in combo)
            if wt <= budget_watts and val > best_brute_utility:
                best_brute_utility = val
                best_brute_weight = wt

    # Mathematical optimality theorem: DP must match brute-force utility score
    assert dp_total_utility == best_brute_utility
    assert dp_total_weight <= budget_watts
    assert best_brute_weight <= budget_watts


def test_scheduler_battery_soc_bounds_conservation() -> None:
    scheduler = HouseholdScheduler()
    battery = BatteryState(soc_wh=4900.0, capacity_wh=5000.0)

    # Massive solar generation: 4000W with 500W load
    res = scheduler.optimize(
        grid_status=GridStatus.GRID_NORMAL,
        solar_pv_watts=4000.0,
        battery=battery,
    )

    # Battery cannot exceed 100% capacity (5000 Wh)
    assert 0.0 <= res.next_battery_soc_wh <= battery.capacity_wh
    assert res.next_battery_soc_pct <= 100.0


def test_scheduler_switchover_mode_dumsor_island() -> None:
    scheduler = HouseholdScheduler()
    battery = BatteryState(soc_wh=3000.0)

    res = scheduler.optimize(
        grid_status=GridStatus.DUMSOR_OUTAGE,
        solar_pv_watts=500.0,
        battery=battery,
    )

    assert res.switchover_mode == SwitchoverMode.BATTERY_BACKUP_ISLAND


def test_scheduler_switchover_mode_solar_export_p2p() -> None:
    scheduler = HouseholdScheduler()
    # Fully charged battery with massive excess solar exceeding all household loads
    battery = BatteryState(soc_wh=5000.0, capacity_wh=5000.0)

    # 5500W solar exceeds full household simultaneous load (4300W)
    res = scheduler.optimize(
        grid_status=GridStatus.GRID_NORMAL,
        solar_pv_watts=5500.0,
        battery=battery,
        smart_meter_watt_cap=2000.0,
    )

    assert res.switchover_mode == SwitchoverMode.SOLAR_EXPORT_P2P
    assert res.solar_watts_exported > 0.0


def test_scheduler_switchover_mode_peak_shave() -> None:
    scheduler = HouseholdScheduler()
    battery = BatteryState(soc_wh=3500.0)

    res = scheduler.optimize(
        grid_status=GridStatus.PEAK_TARIFF,
        solar_pv_watts=600.0,
        battery=battery,
    )

    assert res.switchover_mode == SwitchoverMode.HYBRID_PEAK_SHAVE


def test_scheduler_switchover_mode_offpeak_charge() -> None:
    scheduler = HouseholdScheduler()
    # Depleted battery under normal grid tariff
    battery = BatteryState(soc_wh=1100.0, min_reserve_pct=0.20)  # Reserve is 1000Wh

    res = scheduler.optimize(
        grid_status=GridStatus.GRID_NORMAL,
        solar_pv_watts=0.0,
        battery=battery,
    )

    assert res.switchover_mode == SwitchoverMode.GRID_OFFPEAK_CHARGE
    assert res.battery_watts_flow < 0.0  # Charging


def test_scheduler_zero_budget_and_empty_appliances() -> None:
    empty_scheduler = HouseholdScheduler(appliances=[])
    battery = BatteryState(soc_wh=2000.0)

    res = empty_scheduler.optimize(
        grid_status=GridStatus.GRID_NORMAL,
        solar_pv_watts=0.0,
        battery=battery,
    )

    assert res.scheduled_load_watts == 0
    assert len(res.active_appliances) == 0


# =====================================================================
# 3. Microgrid P2P Double Auction & Economics Tests
# =====================================================================


def test_p2p_matcher_individual_rationality_and_zero_negative_spreads() -> None:
    orderbook = MicrogridOrderbook(protocol_fee_bps=200)

    asks = [
        Order(order_id="A1", trader_id="Alice", wh_amount=5000, price_ghs_per_kwh=1.20),
        Order(order_id="A2", trader_id="Bob", wh_amount=3000, price_ghs_per_kwh=1.50),
    ]
    bids = [
        Order(order_id="B1", trader_id="Charlie", wh_amount=4000, price_ghs_per_kwh=1.80),
        Order(order_id="B2", trader_id="David", wh_amount=2000, price_ghs_per_kwh=1.30),
    ]

    summary = orderbook.match_orders(asks, bids)

    assert len(summary.trades) > 0
    # INVARIANT: For each trade, buyer price >= clearing price >= seller price
    for trade in summary.trades:
        assert trade.clearing_price_ghs_per_kwh >= 1.20
        assert trade.clearing_price_ghs_per_kwh <= 1.80
        assert trade.seller_proceeds_ghs > 0.0
        assert trade.total_cost_ghs >= trade.seller_proceeds_ghs


def test_p2p_matcher_orderbook_conservation() -> None:
    orderbook = MicrogridOrderbook()

    asks = [Order(order_id="A1", trader_id="S1", wh_amount=2000, price_ghs_per_kwh=1.10)]
    bids = [Order(order_id="B1", trader_id="B1", wh_amount=3500, price_ghs_per_kwh=1.70)]

    summary = orderbook.match_orders(asks, bids)

    assert summary.total_wh_matched == 2000
    assert summary.unfilled_asks_wh == 0
    assert summary.unfilled_bids_wh == 1500


def test_p2p_matcher_price_clamped_to_statutory_bounds() -> None:
    orderbook = MicrogridOrderbook()

    # Extreme low and high asks/bids
    asks = [Order(order_id="A1", trader_id="S1", wh_amount=1000, price_ghs_per_kwh=0.40)]  # Below floor
    bids = [Order(order_id="B1", trader_id="B1", wh_amount=1000, price_ghs_per_kwh=3.00)]  # Above ceiling

    # Invalid orders outside statutory limits should be filtered or clamped
    summary = orderbook.match_orders(asks, bids)
    # Since ask price 0.40 < 0.60 floor and bid 3.00 > 2.50 ceiling, they must not execute outside bounds
    for t in summary.trades:
        assert 0.60 <= t.clearing_price_ghs_per_kwh <= 2.50


def test_p2p_matcher_co2_avoided_calculation() -> None:
    orderbook = MicrogridOrderbook()

    asks = [Order(order_id="A1", trader_id="S1", wh_amount=10000, price_ghs_per_kwh=1.00)]
    bids = [Order(order_id="B1", trader_id="B1", wh_amount=10000, price_ghs_per_kwh=1.50)]

    summary = orderbook.match_orders(asks, bids)

    # 10,000 Wh = 10 kWh. Displaced diesel emissions factor = 0.78 kg CO2 / kWh -> 7.8 kg CO2
    expected_co2 = 10.0 * 0.78
    assert pytest.approx(summary.total_diesel_co2_kg_avoided, abs=1e-3) == expected_co2


def test_p2p_matcher_bill_savings_vs_peak_and_diesel() -> None:
    orderbook = MicrogridOrderbook()

    asks = [Order(order_id="A1", trader_id="S1", wh_amount=2000, price_ghs_per_kwh=1.20)]
    bids = [Order(order_id="B1", trader_id="B1", wh_amount=2000, price_ghs_per_kwh=1.40)]

    summary = orderbook.match_orders(asks, bids)

    assert summary.total_buyer_savings_vs_peak_ghs > 0.0
    assert summary.total_buyer_savings_vs_diesel_ghs > summary.total_buyer_savings_vs_peak_ghs


def test_p2p_matcher_protocol_fee_cap_revert() -> None:
    with pytest.raises(ValueError, match="cannot exceed 500 bps"):
        MicrogridOrderbook(protocol_fee_bps=501)


def test_p2p_matcher_no_trade_when_spread_is_crossed_negatively() -> None:
    orderbook = MicrogridOrderbook()

    asks = [Order(order_id="A1", trader_id="S1", wh_amount=2000, price_ghs_per_kwh=1.90)]
    bids = [Order(order_id="B1", trader_id="B1", wh_amount=2000, price_ghs_per_kwh=1.10)]

    summary = orderbook.match_orders(asks, bids)
    assert len(summary.trades) == 0
    assert summary.total_wh_matched == 0


# =====================================================================
# 4. CLI Subcommand Integration Tests
# =====================================================================


def test_cli_simulate_accra_feeder_json_output() -> None:
    cmd = [sys.executable, "-m", "gridpulse.cli", "simulate-accra-feeder", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)

    assert "feeder_capacity_kw" in data
    assert "hourly_forecast_kw" in data
    assert len(data["hourly_forecast_kw"]) == 24
    assert "training_metrics" in data
    assert data["training_metrics"]["r2"] >= 0.90


def test_cli_optimize_home_json_output() -> None:
    cmd = [
        sys.executable,
        "-m",
        "gridpulse.cli",
        "optimize-home",
        "--budget-watts",
        "1200",
        "--solar-watts",
        "850",
        "--battery-soc",
        "0.65",
        "--outage",
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)

    assert data["grid_status"] == "DUMSOR_OUTAGE"
    assert data["switchover_mode"] == "BATTERY_BACKUP_ISLAND"
    assert "Medical/Fridge" in data["active_appliances"]
    assert 0.0 <= data["next_battery_soc_pct"] <= 100.0


def test_cli_match_p2p_json_output() -> None:
    cmd = [sys.executable, "-m", "gridpulse.cli", "match-p2p", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)

    assert "total_kwh_matched" in data
    assert data["total_kwh_matched"] > 0
    assert data["diesel_co2_kg_avoided"] > 0
    assert len(data["trades"]) > 0
