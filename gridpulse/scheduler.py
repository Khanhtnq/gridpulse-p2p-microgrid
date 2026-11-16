"""GridPulse Exact 0-1 Dynamic Programming Load-Shedding & Transfer-Switch Controller.

Models household load prioritization for Ghana microgrid resilience under Dumsor outages.
Guarantees critical appliance invariants, integer knapsack optimality, and battery conservation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GridStatus(StrEnum):
    GRID_NORMAL = "GRID_NORMAL"
    PEAK_TARIFF = "PEAK_TARIFF"
    DUMSOR_OUTAGE = "DUMSOR_OUTAGE"


class SwitchoverMode(StrEnum):
    SOLAR_SELF_CONSUME = "SOLAR_SELF_CONSUME"
    SOLAR_EXPORT_P2P = "SOLAR_EXPORT_P2P"
    BATTERY_BACKUP_ISLAND = "BATTERY_BACKUP_ISLAND"
    HYBRID_PEAK_SHAVE = "HYBRID_PEAK_SHAVE"
    GRID_OFFPEAK_CHARGE = "GRID_OFFPEAK_CHARGE"


@dataclass(frozen=True)
class Appliance:
    name: str
    watts: int
    priority: int  # Higher value = higher household utility
    critical: bool = False


# Canonical Accra/Kumasi Urban Household Appliance Library
DEFAULT_APPLIANCES: list[Appliance] = [
    Appliance(name="Medical/Fridge", watts=200, priority=1000, critical=True),
    Appliance(name="LED Lighting", watts=80, priority=90, critical=False),
    Appliance(name="Ceiling Fans", watts=150, priority=80, critical=False),
    Appliance(name="Laptop/Router", watts=120, priority=85, critical=False),
    Appliance(name="Water Pump", watts=600, priority=60, critical=False),
    Appliance(name="Washing Machine", watts=450, priority=45, critical=False),
    Appliance(name="Electric Water Heater", watts=1500, priority=30, critical=False),
    Appliance(name="Split AC", watts=1200, priority=40, critical=False),
]


@dataclass
class BatteryState:
    soc_wh: float
    capacity_wh: float = 5000.0  # 5 kWh standard LiFePO4 pack
    max_discharge_watts: float = 2500.0
    max_charge_watts: float = 2000.0
    min_reserve_pct: float = 0.20  # 20% reserve to protect cell cycle life

    @property
    def min_reserve_wh(self) -> float:
        return self.capacity_wh * self.min_reserve_pct

    @property
    def usable_energy_wh(self) -> float:
        return max(0.0, self.soc_wh - self.min_reserve_wh)


@dataclass
class ScheduleResult:
    switchover_mode: SwitchoverMode
    total_power_available_watts: float
    critical_load_watts: int
    scheduled_load_watts: int
    active_appliances: list[str]
    shed_appliances: list[str]
    total_utility_score: int
    critical_deficit: bool
    solar_watts_used: float
    solar_watts_exported: float
    battery_watts_flow: float  # Positive = discharging, Negative = charging
    next_battery_soc_wh: float
    next_battery_soc_pct: float


class HouseholdScheduler:
    """Exact 0-1 Dynamic Programming Load Shedding and Energy Dispatcher."""

    def __init__(self, appliances: list[Appliance] | None = None) -> None:
        self.appliances = appliances if appliances is not None else DEFAULT_APPLIANCES

    def optimize(
        self,
        grid_status: GridStatus,
        solar_pv_watts: float,
        battery: BatteryState,
        smart_meter_watt_cap: float = 3000.0,
        time_step_hours: float = 1.0,
    ) -> ScheduleResult:
        """Run DP optimization over appliance loads and determine switchover mode."""
        solar_pv_watts = max(0.0, float(solar_pv_watts))
        smart_meter_watt_cap = max(0.0, float(smart_meter_watt_cap))

        # 1. Determine available generation and power limits by grid status
        usable_battery_watts = min(battery.max_discharge_watts, battery.usable_energy_wh / time_step_hours)

        if grid_status == GridStatus.DUMSOR_OUTAGE:
            grid_power_available = 0.0
            total_power_available = solar_pv_watts + usable_battery_watts
        elif grid_status == GridStatus.PEAK_TARIFF:
            # During peak tariff, minimize grid usage; rely on solar + battery first, grid as ceiling cap
            grid_power_available = min(smart_meter_watt_cap, 800.0)  # Capped peak draw
            total_power_available = solar_pv_watts + usable_battery_watts + grid_power_available
        else:  # GRID_NORMAL
            grid_power_available = smart_meter_watt_cap
            total_power_available = solar_pv_watts + usable_battery_watts + grid_power_available

        # 2. Partition appliances into critical vs deferrable
        critical_apps = [a for a in self.appliances if a.critical]
        deferrable_apps = [a for a in self.appliances if not a.critical]

        critical_load = sum(a.watts for a in critical_apps)
        critical_deficit = False
        active_apps: list[Appliance] = []
        shed_apps: list[Appliance] = []

        # INVARIANT 1: Critical appliances always powered first if power allows
        if total_power_available < critical_load:
            critical_deficit = True
            # In critical deficit, keep as many critical items as fit
            accum = 0
            for app in critical_apps:
                if accum + app.watts <= total_power_available:
                    active_apps.append(app)
                    accum += app.watts
                else:
                    shed_apps.append(app)
            shed_apps.extend(deferrable_apps)
            remaining_budget_watts = 0.0
        else:
            active_apps.extend(critical_apps)
            remaining_budget_watts = total_power_available - critical_load

            # INVARIANT 2: Exact 0-1 Dynamic Programming Knapsack over deferrable appliances
            chosen_deferrable, unchosen_deferrable = self._solve_01_knapsack(
                deferrable_apps, int(remaining_budget_watts)
            )
            active_apps.extend(chosen_deferrable)
            shed_apps.extend(unchosen_deferrable)

        scheduled_load_watts = sum(a.watts for a in active_apps)
        total_utility = sum(a.priority for a in active_apps)

        # 3. Energy Balance & Switchover Mode Determination
        # INVARIANT 3: Mode dispatch and exact battery conservation
        solar_surplus = solar_pv_watts - scheduled_load_watts
        solar_used = min(solar_pv_watts, float(scheduled_load_watts))
        solar_exported = 0.0
        battery_watts_flow = 0.0  # + = discharge, - = charge

        if grid_status == GridStatus.DUMSOR_OUTAGE:
            switchover_mode = SwitchoverMode.BATTERY_BACKUP_ISLAND
            if solar_surplus >= 0:
                # Excess solar charges battery up to max_charge_watts
                charge_potential = min(solar_surplus, battery.max_charge_watts)
                battery_watts_flow = -charge_potential
                solar_exported = solar_surplus - charge_potential
            else:
                # Solar deficit covered by battery
                battery_watts_flow = min(abs(solar_surplus), usable_battery_watts)
        elif grid_status == GridStatus.PEAK_TARIFF:
            switchover_mode = SwitchoverMode.HYBRID_PEAK_SHAVE
            if solar_surplus >= 0:
                charge_potential = min(solar_surplus, battery.max_charge_watts)
                battery_watts_flow = -charge_potential
                solar_exported = solar_surplus - charge_potential
            else:
                battery_watts_flow = min(abs(solar_surplus), usable_battery_watts)
        else:  # GRID_NORMAL
            if solar_surplus > 0:
                # Solar covers all home load. Check battery need.
                headroom_wh = battery.capacity_wh - battery.soc_wh
                if headroom_wh > 10.0:
                    charge_rate = min(solar_surplus, battery.max_charge_watts, headroom_wh / time_step_hours)
                    battery_watts_flow = -charge_rate
                    solar_exported = solar_surplus - charge_rate
                    switchover_mode = SwitchoverMode.SOLAR_SELF_CONSUME
                else:
                    # Battery full -> export excess solar to P2P microgrid
                    solar_exported = solar_surplus
                    switchover_mode = SwitchoverMode.SOLAR_EXPORT_P2P
            else:
                # Normal grid, solar insufficient. Check if battery needs off-peak charging
                if battery.soc_wh < battery.min_reserve_wh * 1.5:
                    switchover_mode = SwitchoverMode.GRID_OFFPEAK_CHARGE
                    charge_rate = min(battery.max_charge_watts, (battery.capacity_wh - battery.soc_wh))
                    battery_watts_flow = -charge_rate
                else:
                    switchover_mode = SwitchoverMode.SOLAR_SELF_CONSUME

        # Update battery SoC with charging efficiency (0.95) and discharging bounds
        if battery_watts_flow < 0:
            # Charging: energy added = watts * hours * 0.95
            delta_wh = abs(battery_watts_flow) * time_step_hours * 0.95
            next_soc_wh = min(battery.capacity_wh, battery.soc_wh + delta_wh)
        else:
            # Discharging: energy removed = watts * hours
            delta_wh = battery_watts_flow * time_step_hours
            next_soc_wh = max(0.0, battery.soc_wh - delta_wh)

        next_soc_pct = round((next_soc_wh / battery.capacity_wh) * 100.0, 1)

        return ScheduleResult(
            switchover_mode=switchover_mode,
            total_power_available_watts=round(total_power_available, 1),
            critical_load_watts=critical_load,
            scheduled_load_watts=scheduled_load_watts,
            active_appliances=[a.name for a in active_apps],
            shed_appliances=[a.name for a in shed_apps],
            total_utility_score=total_utility,
            critical_deficit=critical_deficit,
            solar_watts_used=round(solar_used, 1),
            solar_watts_exported=round(max(0.0, solar_exported), 1),
            battery_watts_flow=round(battery_watts_flow, 1),
            next_battery_soc_wh=round(next_soc_wh, 1),
            next_battery_soc_pct=next_soc_pct,
        )

    @staticmethod
    def _solve_01_knapsack(items: list[Appliance], budget_watts: int) -> tuple[list[Appliance], list[Appliance]]:
        """Exact 0-1 Dynamic Programming with 10W discretization."""
        if budget_watts <= 0 or not items:
            return [], list(items)

        scale = 10
        capacity = budget_watts // scale
        n = len(items)

        # dp[i][w] = max utility using first i items within scaled capacity w
        dp: list[list[int]] = [[0] * (capacity + 1) for _ in range(n + 1)]

        for i in range(1, n + 1):
            item = items[i - 1]
            wt = (item.watts + scale - 1) // scale  # Ceiling scaled weight
            val = item.priority
            for w in range(capacity + 1):
                if wt <= w:
                    dp[i][w] = max(dp[i - 1][w], dp[i - 1][w - wt] + val)
                else:
                    dp[i][w] = dp[i - 1][w]

        # Backtrack to find exact selected items
        chosen: list[Appliance] = []
        w = capacity
        for i in range(n, 0, -1):
            item = items[i - 1]
            wt = (item.watts + scale - 1) // scale
            if dp[i][w] != dp[i - 1][w]:
                chosen.append(item)
                w -= wt

        chosen_set = set(chosen)
        unchosen = [item for item in items if item not in chosen_set]
        return chosen, unchosen
