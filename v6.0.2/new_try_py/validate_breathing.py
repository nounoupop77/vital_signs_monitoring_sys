#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ground-truth validation: is there a REAL breathing signal in a CSI recording?

Protocol (see chat 2026-08-26):
    Subject sits still < 1 m from the ESP32-link line, phone metronome at
    30 bpm (metronome apps rarely go lower), ONE breath per 2 ticks
    (inhale 1 tick, exhale 1 tick)  ->  15 br/min = 0.25 Hz.
    Record >= 3 minutes, then run:

        py validate_breathing.py --file csi_raw_v2.ndjson [--target-hz 0.25]

What it does:
      1. Parses the ndjson, keeps the majority subcarrier-length packets.
      2. Builds two features on the REAL ts_us timeline:
           - phase  : angle(CSI(t) * conj(CSI(t-1))) averaged over subcarriers
           - amplitude : mean |H| over subcarriers (old pipeline's feature)
      3. Welch spectrum of each (resampled onto a uniform 40 Hz grid).
      4. Checks the peak near --target-hz against the local noise floor
         (median of the breathing band EXCLUDING a +/-0.05 Hz window
         around the target), and checks peak stability across thirds.
      5. Prints a verdict and saves a spectrum plot.

Verdict rule:
      SNR >= 5.0 and peak within +/-0.03 Hz in all thirds  -> PASS
      SNR >= 3.0                                          -> BORDERLINE
      otherwise                                           -> FAIL
"""

import argparse
import json
import os
from collections import Counter

import numpy as np
from scipy import signal as sig

FS_GRID = 40.0        # uniform resampling grid (Hz)
NFFT = 8192
BAND = (0.1, 0.5)     # breathing band (Hz)
GUARD = 0.05          # +/- Hz around target excluded from noise floor


def load(path):
    lens, recs = Counter(), []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            sc = d.get("subcarriers", [])
            if not sc:
                continue
            lens[len(sc)] += 1
            recs.append((d["ts_us"] / 1e6, sc))
    mlen = lens.most_common(1)[0][0]
    ts, X = [], []
    for t, sc in recs:
        if len(sc) != mlen:
            continue
        a = np.asarray(sc, dtype=np.float64)
        ts.append(t)
        X.append(a[:, 0] + 1j * a[:, 1])
    return np.array(ts), np.array(X), mlen, len(recs)


def uniform(ts, x):
    T = ts[-1] - ts[0]
    tu = np.linspace(0, T, int(T * FS_GRID))
    return np.interp(tu, ts - ts[0], x)


def spectrum(x):
    f, P = sig.welch(x, fs=FS_GRID, nperseg=int(FS_GRID * 20),
                     nfft=NFFT, scaling="spectrum")
    return f, P


def check_target(f, P, target):
    """Peak near target vs local noise floor. Returns (f_peak, snr, at_edge)."""
    band = (f >= BAND[0]) & (f <= BAND[1])
    win = (f >= target - GUARD) & (f <= target + GUARD)
    noise_mask = band & ~win
    i = np.argmax(P[win])
    f_peak = f[win][i]
    p_peak = P[win][i]
    floor = max(np.median(P[noise_mask]), 1e-15)
    at_edge = (f_peak <= BAND[0] + 0.02) or (f_peak >= BAND[1] - 0.02)
    return f_peak, p_peak / floor, at_edge


def main():
    ap = argparse.ArgumentParser(description="CSI breathing ground-truth check")
    ap.add_argument("--file", required=True, help="captured csi_raw*.ndjson")
    ap.add_argument("--target-hz", type=float, default=0.25,
                    help="paced breathing frequency (default 0.25 = 15 br/min)")
    args = ap.parse_args()

    ts, X, mlen, total = load(args.file)
    if len(ts) < 600:
        print(f"only {len(ts)} packets, need >= 3 min at ~26 Hz")
        return
    dts = np.diff(ts) * 1000
    print(f"packets: {len(ts)}/{total} (len={mlen})   span {ts[-1]-ts[0]:.0f}s   "
          f"rate {len(ts)/(ts[-1]-ts[0]):.1f} Hz   gaps>100ms: {(dts>100).sum()}")
    if (dts > 100).sum() > len(ts) * 0.02:
        print("WARNING: many timing gaps - spectrum will be distorted")

    act = (np.abs(X) > 0).mean(axis=0) > 0.95
    Xa = X[:, act]

    conj = Xa[1:, :] * np.conj(Xa[:-1, :])
    feats = {
        "phase": np.angle(conj).mean(axis=1),
        "amplitude": np.abs(Xa).mean(axis=1),
    }
    t0 = ts[1:] if "phase" in feats else ts

    results = {}
    for name, raw in feats.items():
        t = t0 if name == "phase" else ts
        x = uniform(t, np.asarray(raw))
        f, P = spectrum(x)
        fp, snr, edge = check_target(f, P, args.target_hz)
        # stability across thirds
        n3 = len(x) // 3
        thirds = []
        for i in range(3):
            fi, Pi = spectrum(x[i * n3:(i + 1) * n3])
            fp3, snr3, _ = check_target(fi, Pi, args.target_hz)
            thirds.append(fp3)
        stable = np.all(np.abs(np.array(thirds) - args.target_hz) <= 0.03)
        results[name] = (fp, snr, edge, thirds, stable, f, P)
        print(f"\n[{name}]")
        print(f"  peak near target : {fp:.3f} Hz ({fp*60:.1f} br/min)   SNR {snr:.1f}")
        print(f"  band-edge peak?  : {'YES (leakage signature)' if edge else 'no'}")
        print(f"  peak per third   : " + ", ".join(f"{v:.3f}" for v in thirds) +
              f"   stable={stable}")

        if snr >= 5.0 and stable:
            verdict = "PASS - real breathing signal detected"
        elif snr >= 3.0:
            verdict = "BORDERLINE - weak signal, check geometry/distance"
        else:
            verdict = "FAIL - no credible signal at the paced frequency"
        print(f"  VERDICT          : {verdict}")

    # plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        for ax, (name, (_, _, _, _, _, f, P)) in zip(axes, results.items()):
            m = f <= 3.0
            ax.semilogy(f[m], P[m], lw=0.8)
            ax.axvline(args.target_hz, color="r", ls="--",
                       label=f"target {args.target_hz:.2f} Hz")
            ax.axvspan(*BAND, color="g", alpha=0.08)
            ax.set_ylabel(f"{name} PSD")
            ax.legend()
            ax.grid(alpha=0.3)
        axes[1].set_xlabel("Hz")
        out = os.path.splitext(args.file)[0] + "_validate.png"
        fig.suptitle("Paced-breathing validation")
        fig.tight_layout()
        fig.savefig(out, dpi=120)
        print(f"\nplot -> {out}")
    except ImportError:
        print("\n(matplotlib not installed, plot skipped)")


if __name__ == "__main__":
    main()
