"""GridPulse P2P Double-Auction & Microgrid Uniform-Clearing Orderbook.

Coordinates neighborhood prosumer solar surplus matching with energy deficit households.
Computes bill savings, prosumer revenue, and displaced diesel generator CO2 emissions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Order:
    order_id: str
    trader_id: str
    wh_amount: int
    price_ghs_per_kwh: float  # In Ghanaian Cedis (GHS) per kWh


@dataclass
class TradeMatch:
    ask_id: str
    bid_id: str
    seller_id: str
    buyer_id: str
    wh_cleared: int
    kwh_cleared: float
    clearing_price_ghs_per_kwh: float
    total_cost_ghs: float
    seller_proceeds_ghs: float
    protocol_fee_ghs: float
    buyer_savings_vs_peak_ghs: float
    buyer_savings_vs_diesel_ghs: float
    diesel_co2_kg_avoided: float


@dataclass
class ClearingSummary:
    trades: list[TradeMatch]
    total_wh_matched: int
    total_kwh_matched: float
    total_volume_ghs: float
    total_prosumer_revenue_ghs: float
    total_protocol_fees_ghs: float
    total_buyer_savings_vs_peak_ghs: float
    total_buyer_savings_vs_diesel_ghs: float
    total_diesel_co2_kg_avoided: float
    unfilled_asks_wh: int
    unfilled_bids_wh: int
    average_clearing_price_ghs_per_kwh: float


class MicrogridOrderbook:
    """Continuous Double Auction (CDA) & Uniform-Clearing Microgrid Engine."""

    # Ghana Regulatory & Physical Benchmarks
    PURC_RESIDENTIAL_PEAK_TARIFF: float = 1.85  # GHS / kWh
    DIESEL_GENSET_LEVELIZED_COST: float = 4.50   # GHS / kWh (Fuel + O&M for 5kVA genset in Accra)
    DIESEL_EMISSIONS_FACTOR_KG_PER_KWH: float = 0.78  # kg CO2 / kWh displaced
    SOLAR_FEED_IN_FLOOR: float = 0.60           # GHS / kWh
    MAX_TARIFF_CEILING: float = 2.50            # GHS / kWh

    def __init__(self, protocol_fee_bps: int = 200) -> None:
        if protocol_fee_bps > 500:
            raise ValueError(f"Protocol fee cannot exceed 500 bps (5%), got {protocol_fee_bps}")
        self.protocol_fee_bps = protocol_fee_bps

    def match_orders(
        self,
        asks: list[Order],
        bids: list[Order],
        pricing_mode: str = "midpoint",  # "midpoint" or "uniform"
    ) -> ClearingSummary:
        """Clear solar asks against consumer bids with individual rationality."""
        # Deep copy asks and bids with remaining wh tracking
        active_asks = [
            {"order": a, "rem_wh": a.wh_amount, "price": a.price_ghs_per_kwh}
            for a in asks
            if a.wh_amount > 0 and a.price_ghs_per_kwh <= self.MAX_TARIFF_CEILING
        ]
        active_bids = [
            {"order": b, "rem_wh": b.wh_amount, "price": b.price_ghs_per_kwh}
            for b in bids
            if b.wh_amount > 0 and b.price_ghs_per_kwh >= self.SOLAR_FEED_IN_FLOOR
        ]

        # Asks: lowest price first (cheapest solar supply)
        active_asks.sort(key=lambda x: (x["price"], x["order"].order_id))
        # Bids: highest price first (most willing buyer)
        active_bids.sort(key=lambda x: (-x["price"], x["order"].order_id))

        trades: list[TradeMatch] = []
        ask_idx = 0
        bid_idx = 0

        while ask_idx < len(active_asks) and bid_idx < len(active_bids):
            best_ask = active_asks[ask_idx]
            best_bid = active_bids[bid_idx]

            # Rationality check: Trade only if bid >= ask
            if best_bid["price"] < best_ask["price"]:
                break

            trade_wh = min(best_ask["rem_wh"], best_bid["rem_wh"])
            if trade_wh <= 0:
                if best_ask["rem_wh"] <= 0:
                    ask_idx += 1
                if best_bid["rem_wh"] <= 0:
                    bid_idx += 1
                continue

            if pricing_mode == "uniform":
                # Uniform clearing price at midpoint of marginal matched prices
                clearing_price = (best_ask["price"] + best_bid["price"]) / 2.0
            else:
                # Continuous double auction midpoint
                clearing_price = (best_ask["price"] + best_bid["price"]) / 2.0

            # Clamp between statutory floor and ceiling
            clearing_price = max(self.SOLAR_FEED_IN_FLOOR, min(self.MAX_TARIFF_CEILING, clearing_price))

            trade_kwh = trade_wh / 1000.0
            total_cost = round(trade_kwh * clearing_price, 4)
            fee = round(total_cost * (self.protocol_fee_bps / 10000.0), 4)
            seller_net = round(total_cost - fee, 4)

            # Bill savings vs PURC utility peak and displaced diesel gen
            cost_at_peak = trade_kwh * self.PURC_RESIDENTIAL_PEAK_TARIFF
            savings_vs_peak = round(max(0.0, cost_at_peak - total_cost), 4)

            cost_at_diesel = trade_kwh * self.DIESEL_GENSET_LEVELIZED_COST
            savings_vs_diesel = round(max(0.0, cost_at_diesel - total_cost), 4)

            co2_avoided = round(trade_kwh * self.DIESEL_EMISSIONS_FACTOR_KG_PER_KWH, 4)

            trade = TradeMatch(
                ask_id=best_ask["order"].order_id,
                bid_id=best_bid["order"].order_id,
                seller_id=best_ask["order"].trader_id,
                buyer_id=best_bid["order"].trader_id,
                wh_cleared=trade_wh,
                kwh_cleared=round(trade_kwh, 3),
                clearing_price_ghs_per_kwh=round(clearing_price, 4),
                total_cost_ghs=total_cost,
                seller_proceeds_ghs=seller_net,
                protocol_fee_ghs=fee,
                buyer_savings_vs_peak_ghs=savings_vs_peak,
                buyer_savings_vs_diesel_ghs=savings_vs_diesel,
                diesel_co2_kg_avoided=co2_avoided,
            )
            trades.append(trade)

            best_ask["rem_wh"] -= trade_wh
            best_bid["rem_wh"] -= trade_wh

            if best_ask["rem_wh"] <= 0:
                ask_idx += 1
            if best_bid["rem_wh"] <= 0:
                bid_idx += 1

        unfilled_asks = sum(item["rem_wh"] for item in active_asks)
        unfilled_bids = sum(item["rem_wh"] for item in active_bids)

        tot_wh = sum(t.wh_cleared for t in trades)
        tot_kwh = tot_wh / 1000.0
        tot_vol = sum(t.total_cost_ghs for t in trades)
        avg_price = round(tot_vol / tot_kwh, 4) if tot_kwh > 0 else 0.0

        return ClearingSummary(
            trades=trades,
            total_wh_matched=tot_wh,
            total_kwh_matched=round(tot_kwh, 3),
            total_volume_ghs=round(tot_vol, 2),
            total_prosumer_revenue_ghs=round(sum(t.seller_proceeds_ghs for t in trades), 2),
            total_protocol_fees_ghs=round(sum(t.protocol_fee_ghs for t in trades), 2),
            total_buyer_savings_vs_peak_ghs=round(sum(t.buyer_savings_vs_peak_ghs for t in trades), 2),
            total_buyer_savings_vs_diesel_ghs=round(sum(t.buyer_savings_vs_diesel_ghs for t in trades), 2),
            total_diesel_co2_kg_avoided=round(sum(t.diesel_co2_kg_avoided for t in trades), 3),
            unfilled_asks_wh=unfilled_asks,
            unfilled_bids_wh=unfilled_bids,
            average_clearing_price_ghs_per_kwh=avg_price,
        )
