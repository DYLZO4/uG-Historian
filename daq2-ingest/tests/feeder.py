"""Small commercial three-phase board (~5-9 kW): a flat base load plus appliances that switch.

Each load has its own phase, power, power factor, harmonic current and motor inrush, and the
currents sum per phase, so PF and distortion change with the load mix as they do on a real board.
The feeder impedance sags each phase's voltage with its current; the grid behind it drifts slowly.
"""
import math

import numpy as np

V_OPEN = np.array([232.1, 230.6, 231.5])     # volts RMS behind the feeder impedance
R_OHM, X_OHM = 0.11, 0.05                    # feeder resistance and reactance per phase
SHIFT = 2 * math.pi * np.arange(3) / 3

# name, phases, kW per phase, PF, harmonics {order: (fraction, angle)}, inrush (x, tau s),
# on/off durations (s), first switch time (s)
LOADS = [
    ("air-con", [0], 1.85, 0.93, {5: (0.04, 0.6)}, (3.5, 0.35), ((300, 540), (150, 300)), 60),
    ("fridge", [1], 0.22, 0.78, {3: (0.05, 0.2)}, (4.0, 0.25), ((420, 660), (600, 900)), 30),
    ("urn", [2], 2.0, 1.0, {}, None, ((150, 240), (700, 1500)), 420),
    ("pump", [0, 1, 2], 0.75, 0.83, {5: (0.03, 0.8), 7: (0.015, 1.4)}, (5.5, 0.45),
     ((180, 300), (420, 900)), 150),
    ("printer", [1], 0.95, 0.97, {3: (0.08, 0.3)}, None, ((25, 50), (240, 600)), 470),
]
# Per-load power wander (correlation s, std fraction), in LOADS order
WANDER = [(25, 0.02), (40, 0.012), (60, 0.004), (15, 0.01), (3, 0.06)]
# Air-con compressor power climbs as head pressure builds over a run
AIRCON_CREEP, AIRCON_CREEP_S = 0.10, 150
# Lighting and IT base load per phase: switch-mode supplies give a strong 3rd and 5th
BASE_KW = np.array([1.55, 1.25, 1.45])
BASE_PF = 0.95
BASE_HARM = {3: (0.16, 0.25), 5: (0.09, 0.9), 7: (0.04, 1.6)}


def ou(rng, n, tau, sigma):
    """Ornstein-Uhlenbeck random walk sampled once a second: mean 0, std sigma, correlation tau."""
    a = math.exp(-1 / tau)
    out = np.empty(n)
    x = 0.0
    for k in range(n):
        x = a * x + sigma * math.sqrt(1 - a * a) * rng.normal()
        out[k] = x
    return out


class Feeder:
    def __init__(self, seconds, seed=11, steady=None):
        """steady: names of loads to run continuously (no switching, no inrush) instead of cycling."""
        self.rng = r = np.random.default_rng(seed)
        n = int(seconds) + 2
        self.steady = steady
        # On intervals per load: alternate on/off with random durations
        self.on = []
        for name, _, _, _, _, _, (on_d, off_d), first in LOADS:
            if steady is not None:
                self.on.append([(-1.0, float(n))] if name in steady else [])
                continue
            spans, t = [], first
            while t < n:
                d = r.uniform(*on_d)
                spans.append((t, t + d))
                t += d + r.uniform(*off_d)
            self.on.append(spans)
        # The base load barely moves over minutes; the grid voltage and frequency drift slowly
        calm = 0.4 if steady is not None else 1.0       # a steady demo also has a quieter grid
        self.base = 1.0 + ou(r, n, 600, 0.015 * calm) + ou(r, n, 4, 0.012 * calm)
        # Appliances aren't constant either: compressors and pumps vary with pressure, the
        # printer's fuser cycles
        self.wander = [ou(r, n, tau, sd * calm) for tau, sd in WANDER]
        self.v_grid = ou(r, n, 400, 0.6 * calm)
        self.hz = 50.0 + ou(r, n, 60, 0.015)

    @staticmethod
    def at(arr, t):
        k = int(t)
        f = t - k
        return arr[k] * (1 - f) + arr[k + 1] * f

    def components(self, t):
        """Per-phase list of (amps RMS at 230 V, lag angle, harmonics) for the loads on at time t."""
        out = [[] for _ in range(3)]
        base = self.at(self.base, t)
        for k in range(3):
            out[k].append((BASE_KW[k] * base * 1000 / (230 * BASE_PF), math.acos(BASE_PF),
                           BASE_HARM))
        for (name, phases, kw, pf, harm, inrush, _, _), spans, wander in zip(LOADS, self.on, self.wander):
            for start, stop in spans:
                if start <= t < stop:
                    kw = kw * (1 + self.at(wander, t))
                    if name == "air-con" and self.steady is None:
                        kw *= 1 + AIRCON_CREEP * (1 - math.exp(-(t - start) / AIRCON_CREEP_S))
                    mult = 1.0
                    if inrush and self.steady is None:
                        x, tau = inrush
                        mult += (x - 1) * math.exp(-(t - start) / tau)
                    # A starting motor's current is mostly reactive: PF falls towards 0.35
                    lag = math.acos(pf)
                    if inrush and self.steady is None:
                        share = (mult - 1) / (x - 1)
                        lag = math.acos(pf + (0.35 - pf) * share)
                    for k in phases:
                        out[k].append((kw * mult * 1000 / (230 * pf), lag, harm))
                    break
                if start > t:
                    break
        return out

    def phase_currents(self, t):
        """Fundamental phasor current per phase as (in-phase, quadrature) amps RMS."""
        comps = self.components(t)
        return [(sum(a * math.cos(g) for a, g, _ in c), sum(a * math.sin(g) for a, g, _ in c))
                for c in comps]

    def voltage_rms(self, t):
        ip_iq = self.phase_currents(t)
        return np.array([V_OPEN[k] + self.at(self.v_grid, t) - (R_OHM * ip + X_OHM * iq)
                         for k, (ip, iq) in enumerate(ip_iq)])

    def power_kw(self, t):
        v = self.voltage_rms(t)
        return sum(v[k] * ip for k, (ip, _) in enumerate(self.phase_currents(t))) / 1000
