#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP32-S3 CSI vitals, v3 -- built on the validated findings of 2026-08-26.

What changed vs v2 (and why):
  1. AMPLITUDE is the only primary feature. The v2 phase feature
     (conj-multiply angle) was disproved by the controlled breathing
     experiment: amplitude found the real breath peak (0.183 Hz, SNR 7.7,
     -92% during apnea) while phase locked onto an unrelated 0.35 Hz peak.
     Single-antenna ESP32 phase is corrupted by CFO/SFO/HT-frame mixing.
  2. Honest SNR: periodogram of the DETRENDED window, nfft = nperseg (no
     zero padding), noise floor = MEDIAN of nonzero in-band bins, gate 5.0.
     The old zero-padded floor collapsed to ~0 and produced fake SNRs.
  3. RR window 60 s, HR window 90 s (noise floor drops with sqrt(N); a
     slower HR number is acceptable if it is finally TRUE).
  4. Breath-hold auto-detection: envelope of the 0.1-0.5 Hz band; when it
     collapses below 40% of its rolling reference the subject is deemed
     apneic -- breathing harmonics are gone, the best window for HR.
  5. Breathing harmonics are notched out of the HR channel ONLY when the
     RR estimate is confident (SNR>=5), never from a garbage RR.
  6. Replay mode measures the TRUE packet rate from ts_us (the old replay
     stamped a fake 40 Hz grid and scaled every frequency by 1.55x).
  7. Below the gate we report None ("no confident estimate"), never noise.

Usage:
    live  : py csi_vitals_v3.py --broker 192.168.110.69 --port 1883
    replay: py csi_vitals_v3.py --replay csi_raw_v2_20260826_161932.ndjson
"""

import argparse
import csv
import json
import os
import sys
import threading
import time
from collections import deque, Counter
from datetime import datetime

import numpy as np
from scipy import signal as sig

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # only needed for live mode

# ---------------- parameters ----------------
FS = 40.0                     # processing grid (Hz)
RR_WINDOW = 60                # s
HR_WINDOW = 90                # s
RR_BAND = (0.1, 0.5)          # Hz  (6-30 br/min)
HR_BAND = (0.8, 2.17)         # Hz  (48-130 bpm)
SNR_GATE = 5.0                # validated breathing peak measured 7.7
LLTF_MAX = 63                 # L-LTF subcarriers only (HT ones are 2x amp)
HOLD_RATIO = 0.40             # envelope below 40% of reference -> apnea
HOLD_REF_SEC = 180            # rolling reference length for the envelope
RATE_SMOOTH = 5               # temporal median of accepted estimates
ESTIMATE_EVERY = 1.0          # s between estimates

AMP_BUF = deque()             # (arrival_ts, amplitude)
ENV_BUF = deque()             # (ts, breathing-band envelope, 5 s blocks)
BUF_LOCK = threading.Lock()   # MQTT thread writes AMP_BUF, estimator reads
_rr_hist = deque(maxlen=RATE_SMOOTH)
_hr_hist = deque(maxlen=RATE_SMOOTH)


def now():
    return datetime.now().strftime("%H:%M:%S")


# ---------------- ingest ----------------
def step1_amplitude(payload):
    """|H_k| = sqrt(I^2+Q^2), L-LTF subcarriers, nulls dropped. One float."""
    sc = payload.get("subcarriers", [])
    if isinstance(sc, str):
        sc = json.loads(sc)
    try:
        arr = np.asarray(sc, dtype=np.float64)
    except Exception:
        return None
    if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] == 0:
        return None
    if arr.shape[0] != LLTF_MAX + 1 and arr.shape[0] > LLTF_MAX:
        pass  # HT packets carry 192 entries; keep the L-LTF head
    amps = np.sqrt(arr[:, 0] ** 2 + arr[:, 1] ** 2)[:LLTF_MAX + 1]
    act = amps[amps > 0]
    return float(np.mean(act)) if act.size else None


def handle_packet(payload, ts, sink):
    amp = step1_amplitude(payload)
    if amp is None:
        return
    sink.append((ts, amp))


# ---------------- DSP ----------------
def uniform_window(seconds):
    """Last `seconds` of (ts, amp) interpolated onto the FS grid."""
    if len(AMP_BUF) < 8:
        return None
    with BUF_LOCK:
        pts = list(AMP_BUF)
    ts = np.array([p[0] for p in pts])
    amp = np.array([p[1] for p in pts])
    t_end = ts[-1]
    mask = ts >= t_end - seconds
    ts_w, amp_w = ts[mask], amp[mask]
    if ts_w.size < 8 or ts_w[-1] - ts_w[0] < seconds * 0.75:
        return None                      # not enough data / big gaps
    ok = np.concatenate(([True], np.diff(ts_w) > 1e-6))
    ts_w, amp_w = ts_w[ok], amp_w[ok]
    n = int(seconds * FS)
    return np.interp(np.linspace(0, seconds, n), ts_w - ts_w[0], amp_w)


def band_peak(x, lo, hi, edge_guard=0.025):
    """Detrended periodogram peak in [lo,hi]. Returns (freq, snr) or None.
    nfft = nperseg so the median noise floor is made of real bins.
    Peaks hugging a band edge are rejected: they are the signature of
    DC/leakage skirts, not of a real oscillation (validated 2026-08-26:
    every fake RR the old pipelines produced sat within 0.03 Hz of 0.1)."""
    x = sig.detrend(x)
    f, P = sig.welch(x, fs=FS, nperseg=len(x), nfft=len(x), scaling="spectrum")
    m = (f >= lo) & (f <= hi)
    if not m.any():
        return None
    Pf = P[m]
    j = int(np.argmax(Pf))
    if f[m][j] - lo < edge_guard or hi - f[m][j] < edge_guard:
        return None                      # band-edge leakage, not a peak
    pos = Pf[Pf > 0]
    noise = float(np.median(pos)) if pos.size else 1e-15
    snr = float(Pf[j] / max(noise, 1e-15))
    # parabolic sub-bin refinement
    if 0 < j < len(Pf) - 1:
        a, b, c = Pf[j - 1], Pf[j], Pf[j + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-15:
            j_ref = j + 0.5 * (a - c) / denom
        else:
            j_ref = j
    else:
        j_ref = j
    freq = f[m][0] + (j_ref) * (f[1] - f[0])
    return float(min(max(freq, lo), hi)), snr


def envelope_block():
    """RMS of the 0.1-0.5 Hz band over the last 5 s (one scalar)."""
    x = uniform_window(10)
    if x is None:
        return None
    xb = sig.filtfilt(*sig.butter(4, RR_BAND, btype="band", fs=FS),
                      sig.detrend(x))
    return float(np.sqrt(np.mean(xb[-int(5 * FS):] ** 2)))


def update_hold_state():
    """Append one envelope sample; return True while in breath-hold.
    The dip must persist 5 s (median of the last 5 blocks) so that a
    single shallow breath does not look like apnea."""
    e = envelope_block()
    if e is None:
        return None
    t_now = REPLAY_T[0] if not LIVE else time.time()
    ENV_BUF.append((t_now, e))
    while ENV_BUF and ENV_BUF[0][0] < t_now - HOLD_REF_SEC:
        ENV_BUF.popleft()
    if len(ENV_BUF) < 12:                # need >= 1 min of reference
        return False
    ref = np.percentile([v for _, v in ENV_BUF], 75)
    if ref <= 0:
        return False
    recent = [v for _, v in list(ENV_BUF)[-5:]]
    return float(np.median(recent)) < HOLD_RATIO * ref


def notch_breathing_harmonics(x, f_rr):
    """Remove n*f_rr inside the HR band only (n*f_rr in 0.7..2.3 Hz)."""
    xn = x.copy()
    n = 1
    while f_rr * n < FS / 2:
        f0 = f_rr * n
        if f0 > 0.7:
            if f0 > 2.3:
                break
            q = max(10.0, f0 / (FS / len(x)))
            xn = sig.filtfilt(*sig.iirnotch(f0, q, fs=FS), xn)
        n += 1
    return xn


def estimate():
    """Returns dict with rr/hr (+snr) and hold flag, or None."""
    x_rr = uniform_window(RR_WINDOW)
    if x_rr is None:
        return None
    hold = update_hold_state()

    res = {"hold": bool(hold)}
    pk = band_peak(x_rr, *RR_BAND)
    if pk and pk[1] >= SNR_GATE:
        f_rr = pk[0]
        _rr_hist.append(round(f_rr * 60.0, 1))
        res["rr"] = float(np.median(_rr_hist))
        res["rr_snr"] = round(pk[1], 1)
        res["f_rr"] = f_rr
    else:
        res["rr"] = None
        res["rr_snr"] = round(pk[1], 1) if pk else None

    x_hr = uniform_window(HR_WINDOW)
    if x_hr is not None:
        # Notch ONLY with a confident, mid-band RR. A garbage f_rr combs the
        # HR band with notches, depresses the noise floor and manufactures
        # fake high-SNR peaks (seen and disproved by autocorrelation +
        # per-subcarrier coherence on the 161932 recording).
        if res.get("rr") is not None and not hold:
            x_hr = notch_breathing_harmonics(x_hr, res["f_rr"])
        pk2 = band_peak(x_hr, *HR_BAND)
        if pk2 and pk2[1] >= SNR_GATE:
            _hr_hist.append(round(pk2[0] * 60.0, 1))
            res["hr"] = float(np.median(_hr_hist))
            res["hr_snr"] = round(pk2[1], 1)
        else:
            res["hr"] = None
            res["hr_snr"] = round(pk2[1], 1) if pk2 else None
    return res


# ---------------- output ----------------
CSV_PATH = None
csv_writer = None
raw_file = None


def report(est):
    rr = f"{est['rr']:.1f}" if est.get("rr") else "--"
    hr = f"{est['hr']:.1f}" if est.get("hr") else "--"
    hold = "HOLD" if est["hold"] else "    "
    rsnr = f"{est['rr_snr']:.1f}" if est.get("rr_snr") else "--"
    hsnr = f"{est['hr_snr']:.1f}" if est.get("hr_snr") else "--"
    print(f"[{now()}] RR={rr:>5} (snr {rsnr:>5})  HR={hr:>5} (snr {hsnr:>5})"
          f"  {hold}  buf={len(AMP_BUF)}")
    if csv_writer:
        csv_writer.writerow([datetime.now().isoformat(timespec="seconds"),
                             rr if rr != "--" else "",
                             rsnr if rsnr != "--" else "",
                             hr if hr != "--" else "",
                             hsnr if hsnr != "--" else "",
                             int(est["hold"])])


# ---------------- live / replay ----------------
LIVE = True
REPLAY_T = [0.0]


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[{now()}] CONNECTED, subscribing to '{userdata['topic']}'")
        client.subscribe(userdata["topic"])
    else:
        print(f"[{now()}] CONNECT FAILED rc={rc}")


def on_message(client, userdata, msg):
    global raw_file
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        return
    with BUF_LOCK:
        handle_packet(payload, time.time(), AMP_BUF)
    if raw_file:
        try:
            raw_file.write(json.dumps(payload) + "\n")
            raw_file.flush()
        except Exception:
            pass


def main():
    global LIVE, CSV_PATH, csv_writer, raw_file
    ap = argparse.ArgumentParser(description="CSI vitals v3 (amplitude, honest)")
    ap.add_argument("--broker", default="xg-6.frp.one")
    ap.add_argument("--port", type=int, default=63992)
    ap.add_argument("--topic", default="me41004/csi")
    ap.add_argument("--replay", default=None, help="ndjson captured file")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    CSV_PATH = os.path.join(base, f"csi_results_v3_{stamp}.csv")
    csv_file = open(CSV_PATH, "w", newline="", encoding="utf-8")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["timestamp", "rr_bpm", "rr_snr", "hr_bpm",
                         "hr_snr", "hold"])

    print(f"[{now()}] v3: amplitude feature | RR {RR_WINDOW}s | HR {HR_WINDOW}s"
          f" | gate SNR>={SNR_GATE}")
    print(f"[{now()}] csv -> {CSV_PATH}")

    if args.replay:
        LIVE = False
        lens = Counter()
        recs = []
        with open(args.replay, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                lens[len(d.get("subcarriers", []))] += 1
                recs.append(d)
        mlen = lens.most_common(1)[0][0] if lens else 0
        # true rate from ts_us gaps of majority-length packets
        tts = [d["ts_us"] for d in recs
               if len(d.get("subcarriers", [])) == mlen and "ts_us" in d]
        rate = 1e6 / float(np.median(np.diff(tts))) if len(tts) > 10 else 25.9
        print(f"[{now()}] replay {args.replay}: {len(recs)} packets, "
              f"measured {rate:.1f} pkt/s (stamped onto real ts_us)")
        last_est = 0.0
        for d in recs:
            if "ts_us" not in d or len(d.get("subcarriers", [])) != mlen:
                continue
            t = d["ts_us"] / 1e6
            REPLAY_T[0] = t
            handle_packet(d, t, AMP_BUF)
            if t - last_est >= ESTIMATE_EVERY:
                last_est = t
                est = estimate()
                if est:
                    report(est)
        print(f"[{now()}] replay done")
        return

    if mqtt is None:
        print("paho-mqtt not installed; run: py -m pip install paho-mqtt")
        sys.exit(1)
    RAW_PATH = os.path.join(base, f"csi_raw_v3_{stamp}.ndjson")
    raw_file = open(RAW_PATH, "a", encoding="utf-8")
    print(f"[{now()}] raw -> {RAW_PATH}")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="csi-v3", userdata={"topic": args.topic})
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=10)
    client.connect(args.broker, args.port, keepalive=60)

    last_est = 0.0

    def tick():
        nonlocal last_est
        t = time.time()
        if t - last_est >= ESTIMATE_EVERY:
            last_est = t
            est = estimate()
            if est:
                report(est)

    # simple loop: process network + estimate once per second
    stop = threading.Event()

    def estimator():
        while not stop.is_set():
            try:
                tick()
            except Exception as e:
                print(f"[{now()}] estimate error (skipped): {e}")
            time.sleep(0.2)

    th = threading.Thread(target=estimator, daemon=True)
    th.start()
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        csv_file.close()
        if raw_file:
            raw_file.close()
        print(f"\n[{now()}] stopped, csv -> {CSV_PATH}")


if __name__ == "__main__":
    main()
