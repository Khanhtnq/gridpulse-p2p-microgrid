"""GridPulse AI Peak-Load Forecaster for Accra/Kumasi Distribution Feeders.

Models multi-harmonic Fourier diurnal cycles (24h, 12h, 8h) with cooling-degree
temperature sensitivity. Provides ordinary least-squares / ridge regression in
pure Python, estimating 24-hour feeder demand, Dumsor brownout risk, and backtest metrics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ForecastResult:
    predicted_demand_kw: list[float]
    overload_probability: list[float]
    peak_hours_window: tuple[int, int]
    max_demand_kw: float
    feeder_capacity_kw: float
    dumsor_alert: bool
    metrics: dict[str, float]


class FeederLoadForecaster:
    """Fourier diurnal multi-harmonic load forecaster with cooling sensitivity."""

    def __init__(self, feeder_capacity_kw: float = 800.0, l2_reg: float = 1e-3) -> None:
        self.feeder_capacity_kw = float(feeder_capacity_kw)
        self.l2_reg = float(l2_reg)
        self.weights: list[float] = []
        self.is_trained: bool = False

    @staticmethod
    def extract_features(hour: float, temp_c: float) -> list[float]:
        """Extract 8-dimensional harmonic diurnal and cooling features.

        Basis: [1.0, cos(w1*t), sin(w1*t), cos(w2*t), sin(w2*t), cos(w3*t), sin(w3*t), cooling_deg]
        where w1 = 2pi/24, w2 = 4pi/24, w3 = 6pi/24.
        """
        w1 = 2.0 * math.pi * (hour % 24.0) / 24.0
        w2 = 4.0 * math.pi * (hour % 24.0) / 24.0
        w3 = 6.0 * math.pi * (hour % 24.0) / 24.0

        cooling_deg = max(0.0, temp_c - 26.0)

        return [
            1.0,
            math.cos(w1),
            math.sin(w1),
            math.cos(w2),
            math.sin(w2),
            math.cos(w3),
            math.sin(w3),
            cooling_deg,
        ]

    def train(self, hours: list[float], temps_c: list[float], actual_kw: list[float]) -> dict[str, float]:
        """Fit Ridge Regression weights via regularized normal equations."""
        n_samples = len(hours)
        if n_samples < 8:
            raise ValueError(f"Need at least 8 samples to fit model, got {n_samples}")

        # Construct Design Matrix X (n_samples x 8)
        X: list[list[float]] = [self.extract_features(h, t) for h, t in zip(hours, temps_c)]
        p = 8

        # Compute X^T * X and X^T * y
        XtX: list[list[float]] = [[0.0] * p for _ in range(p)]
        Xty: list[float] = [0.0] * p

        for row, y_val in zip(X, actual_kw):
            for i in range(p):
                Xty[i] += row[i] * y_val
                for j in range(p):
                    XtX[i][j] += row[i] * row[j]

        # Add L2 Ridge regularization: XtX[i][i] += l2_reg (skip bias index 0)
        for i in range(1, p):
            XtX[i][i] += self.l2_reg

        # Solve (XtX) * w = Xty using Gaussian elimination with partial pivoting
        self.weights = self._solve_linear_system(XtX, Xty)
        self.is_trained = True

        # Compute backtest performance metrics
        preds = [self.predict_single(h, t) for h, t in zip(hours, temps_c)]
        mae = sum(abs(p - a) for p, a in zip(preds, actual_kw)) / n_samples
        rmse = math.sqrt(sum((p - a) ** 2 for p, a in zip(preds, actual_kw)) / n_samples)

        mean_act = sum(actual_kw) / n_samples
        ss_tot = sum((a - mean_act) ** 2 for a in actual_kw)
        ss_res = sum((a - p) ** 2 for a, p in zip(actual_kw, preds))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 1.0

        return {"mae": round(mae, 3), "rmse": round(rmse, 3), "r2": round(r2, 4)}

    def predict_single(self, hour: float, temp_c: float) -> float:
        """Predict feeder demand in kW for a specific hour and ambient temperature."""
        if not self.is_trained or not self.weights:
            raise RuntimeError("Model must be trained before predicting")
        feats = self.extract_features(hour, temp_c)
        pred = sum(w * f for w, f in zip(self.weights, feats))
        return max(0.0, pred)

    def predict_24h(self, temps_c_24h: list[float]) -> ForecastResult:
        """Generate full 24-hour feeder demand profile and Dumsor overload risk."""
        if len(temps_c_24h) != 24:
            raise ValueError(f"Expected 24 temperature readings, got {len(temps_c_24h)}")

        preds: list[float] = []
        risks: list[float] = []

        for h in range(24):
            val = self.predict_single(float(h), temps_c_24h[h])
            preds.append(round(val, 2))

            # Overload probability: Sigmoid centered at 95% capacity
            # P = 1 / (1 + exp(-k * (demand / capacity - 0.95)))
            cap = self.feeder_capacity_kw
            ratio = val / cap
            z = (ratio - 0.95) / 0.05
            prob = 1.0 / (1.0 + math.exp(-max(-15.0, min(15.0, z))))
            risks.append(round(prob, 4))

        max_demand = max(preds)
        # Peak hours window detection (hours where demand >= 90% of max)
        threshold = max_demand * 0.90
        peak_indices = [i for i, v in enumerate(preds) if v >= threshold]
        start_peak = peak_indices[0] if peak_indices else 18
        end_peak = peak_indices[-1] if peak_indices else 22

        dumsor_alert = any(r >= 0.50 for r in risks) or (max_demand >= self.feeder_capacity_kw * 0.95)

        return ForecastResult(
            predicted_demand_kw=preds,
            overload_probability=risks,
            peak_hours_window=(start_peak, end_peak),
            max_demand_kw=round(max_demand, 2),
            feeder_capacity_kw=self.feeder_capacity_kw,
            dumsor_alert=dumsor_alert,
            metrics={"max_utilization_pct": round((max_demand / self.feeder_capacity_kw) * 100.0, 1)},
        )

    @staticmethod
    def generate_accra_telemetry(days: int = 14, seed: int = 42) -> tuple[list[float], list[float], list[float]]:
        """Synthesize deterministic, physics-based feeder telemetry for Accra/Kumasi."""
        # Simple deterministic LCG pseudo-random for reproducible telemetry
        state = seed

        def next_rand() -> float:
            nonlocal state
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            return state / 4294967296.0

        hours: list[float] = []
        temps: list[float] = []
        kw: list[float] = []

        total_hours = days * 24
        for step in range(total_hours):
            h = float(step % 24)
            # Diurnal temperature profile: Min 24.5C at 05:00, Max 33.5C at 14:00
            temp_variation = math.sin((h - 8.0) * math.pi / 12.0)
            ambient_temp = 28.5 + 4.5 * temp_variation + (next_rand() - 0.5) * 1.0

            # Base feeder demand cycle
            # Morning ramp (07:00), afternoon commercial cooling, evening residential peak (19:00-21:00)
            base = 400.0
            diurnal_1 = -100.0 * math.cos(2.0 * math.pi * h / 24.0)
            diurnal_2 = -80.0 * math.cos(4.0 * math.pi * (h - 2.0) / 24.0)
            evening_spike = 180.0 * math.exp(-0.5 * ((h - 19.5) / 1.8) ** 2)
            cooling_demand = max(0.0, ambient_temp - 26.0) * 18.0
            noise = (next_rand() - 0.5) * 15.0

            demand = base + diurnal_1 + diurnal_2 + evening_spike + cooling_demand + noise

            hours.append(h)
            temps.append(round(ambient_temp, 2))
            kw.append(round(demand, 2))

        return hours, temps, kw

    @staticmethod
    def _solve_linear_system(A: list[list[float]], b: list[float]) -> list[float]:
        """Gaussian elimination with partial pivoting."""
        n = len(b)
        # Augment matrix M = [A | b]
        M = [row[:] + [b[i]] for i, row in enumerate(A)]

        for i in range(n):
            # Pivot selection
            max_row = i
            max_val = abs(M[i][i])
            for k in range(i + 1, n):
                if abs(M[k][i]) > max_val:
                    max_val = abs(M[k][i])
                    max_row = k

            if max_val < 1e-12:
                # Singular or ill-conditioned, add damping
                M[i][i] += 1e-4

            if max_row != i:
                M[i], M[max_row] = M[max_row], M[i]

            pivot = M[i][i]
            for col in range(i, n + 1):
                M[i][col] /= pivot

            for row in range(n):
                if row != i:
                    factor = M[row][i]
                    for col in range(i, n + 1):
                        M[row][col] -= factor * M[i][col]

        return [M[i][n] for i in range(n)]
