#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP32-S3 CSI -> heart rate + respiration rate (improved pipeline).

Drop-in replacement for the DSP core of csi_subscriber.py. Keeps the good
parts of the original (rate-adaptive resampling onto a 40 Hz grid, MQTT
publish, CSV/raw logging) but fixes the signal model that produced noisy,
multi-peak FFTs.

WHY THE ORIGINAL WAS NOISY
--------------------------
The original took, per packet, mean( sqrt(I^2+Q^2) over all 192 subcarriers )
as the single vital-signs sample. That feature is dominated by the static
direct path; chest motion is a sub-millimetre ripple on top, and the amplitude
response to a small displacement is sinusoidal across subcarrier index, so
averaging ALL subcarriers makes half of them cancel the other half. What is
left is mostly environmental noise -> the FFT peak hops randomly and the
breathing rate prints values spread evenly from 7 to 28 br/min.

WHAT CHANGED (in priority order)
--------------------------------
 1. Buffer per-subcarrier COMPLEX CSI (not the mean amplitude).
 2. Remove the static path by conjugate multiplication of adjacent samples:
        r(t) = angle( CSI(t) * conj(CSI(t-1)) )
    That phase tracks the tiny displacement directly and is far more
    sensitive to vital signs than raw amplitude.
 3. Drop null/guard subcarriers (the [0,0] entries).
 4. Select the N_SELECT subcarriers that carry the most breathing-band power
    and combine only those, instead of averaging everything.
 5. Spectrum via Welch (averaged periodogram) -> lower variance, fewer peaks.
 6. Robust peak: smoothed spectrum + local noise-floor SNR gate + parabolic
    sub-bin interpolation + temporal median smoothing of the rate.
 7. Longer 20 s analysis window -> 0.05 Hz (3 bpm) resolution.

OFFLINE TUNING (no ESP32 needed)
--------------------------------
    py csi_vitals_v2.py --replay csi_raw.ndjson --replay-rate 40
LIVE
----
    py csi_vitals_v2.py --broker xg-6.frp.one --port 63992 --topic me41004/csi

Dependencies: paho-mqtt numpy scipy
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import deque, Counter
from datetime import datetime

try:
    import numpy as np
    from scipy import signal as sig
except ImportError as e:
    print("Missing dependency:", e)
    print("Run:  py -m pip install numpy scipy")
    sys.exit(1)

# ---- signal-processing parameters ----
FS = 40                  # processing / resampling grid (Hz)
WINDOW_SEC = 20          # analysis window length (s) -> 0.05 Hz resolution
NFFT = 4096              # zero-pad for smooth display + interpolation
N_SELECT = 30            # best subcarriers to combine
SNR_MIN = 3.0            # reject estimate if peak < 3x local noise floor
RATE_SMOOTH = 5          # median over this many recent estimates

HR_BAND = (0.80, 2.00)   # 48 - 120 bpm
RR_BAND = (0.15, 0.50)   # 9  - 30  br/min (floor raised to cut low-freq drift)

b_hr, a_hr = sig.butter(4, list(HR_BAND), btype="band", fs=FS)
b_rr, a_rr = sig.butter(4, list(RR_BAND), btype="band", fs=FS)

VITALS_TOPIC = "me41004/vitals"

# ---- rolling buffer of per-packet complex CSI + wall-clock timestamps ----
CSI_BUF = deque()        # complex128 array (n_sub,) per packet
CSI_BUF_TS = deque()     # float timestamp per packet
MAX_BUF = int(FS * WINDOW_SEC * 2) + 32

# recent rate estimates for temporal smoothing
_recent = {"hr": deque(maxlen=RATE_SMOOTH), "rr": deque(maxlen=RATE_SMOOTH)}


def extract_complex(csi_payload):
    """[I,Q] pairs -> complex128 array. Nulls stay 0+0j and are masked later."""
    sc = csi_payload.get("subcarriers", [])
    if isinstance(sc, str):
        sc = json.loads(sc)
    arr = np.asarray(sc, dtype=np.float64)
    # Expect an (n_sub, 2) table of [I,Q]. Reject anything malformed so a
    # single odd packet never poisons the rolling buffer.
    if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] == 0:
        return None
    return arr[:, 0] + 1j * arr[:, 1]


def resample_matrix(duration=WINDOW_SEC, fs=FS):
    """Resample the most recent `duration` s of per-subcarrier complex CSI onto
    a uniform fs grid. Returns (tu, Xr) with Xr (N, n_sub) complex, or
    (None, None) if not enough data yet."""
    if len(CSI_BUF_TS) < 8:
        return None, None
    ts_all = np.fromiter(CSI_BUF_TS, dtype=float)
    rows = list(CSI_BUF)
    # The ESP32 occasionally emits a packet whose subcarrier count differs
    # (truncated / different device). Stack only packets matching the modal
    # count, otherwise np.asarray() raises "inhomogeneous shape".
    n_sub = Counter(r.shape[0] for r in rows).most_common(1)[0][0]
    keep = [i for i, r in enumerate(rows) if r.shape[0] == n_sub]
    if len(keep) < 8:
        return None, None
    ts = ts_all[keep]
    X = np.asarray([rows[i] for i in keep], dtype=np.complex128)  # (M, n_sub)
    t_end = ts[-1]
    mask = ts >= (t_end - duration)
    ts_w, X = ts[mask], X[mask]
    if X.shape[0] < 8:
        return None, None
    ok = np.concatenate(([True], np.diff(ts_w) > 1e-6))
    ts_w, X = ts_w[ok], X[ok]
    ts_rel = ts_w - ts_w[0]
    N = int(fs * duration)
    tu = np.linspace(0, duration, N)
    n_sub = X.shape[1]
    Xr = np.empty((N, n_sub), dtype=np.complex128)
    for k in range(n_sub):
        Xr[:, k] = (np.interp(tu, ts_rel, X[:, k].real)
                    + 1j * np.interp(tu, ts_rel, X[:, k].imag))
    return tu, Xr


def sanitize_and_select(X):
    """Conjugate-multiply static-path removal + automatic subcarrier selection.
    Returns (combined_signal, selected_indices) or (None, None)."""
    N, n_sub = X.shape
    # CSI sanitization: phase of CSI_t * conj(CSI_{t-1}) isolates motion
    conj = X[1:, :] * np.conj(X[:-1, :])
    phase = np.angle(conj)                              # (N-1, n_sub)

    # power within the respiration band per subcarrier (relevance score)
    nper = min(phase.shape[0], 256)
    f, P = sig.welch(phase, fs=FS, nperseg=nper, axis=0, scaling="spectrum")
    in_band = (f >= RR_BAND[0]) & (f <= RR_BAND[1])
    power = P[in_band, :].sum(axis=0)

    active = np.where(power > 1e-12)[0]
    if active.size == 0:
        return None, None
    k = min(N_SELECT, active.size)
    sel = active[np.argsort(power[active])[-k:]]
    combined = phase[:, sel].mean(axis=1)
    return combined, sel


def robust_peak(f, P, lo, hi):
    """Return (freq_hz, snr) of the strongest, well-isolated peak in [lo,hi]."""
    band = (f >= lo) & (f <= hi)
    fb, Pb = f[band], P[band]
    if fb.size < 5:
        return None, None
    Ps = np.convolve(Pb, np.ones(3) / 3.0, mode="same")  # light smoothing
    i = int(np.argmax(Ps))
    # local noise floor = median of band, peak neighbourhood excluded
    nbr = np.ones(fb.size, bool)
    nbr[max(0, i - 3):min(fb.size, i + 4)] = False
    floor = np.median(Ps[nbr]) if nbr.any() else 1e-12
    snr = Ps[i] / (floor + 1e-12)
    # parabolic interpolation -> sub-bin frequency
    denom = (Ps[i - 1] - 2 * Ps[i] + Ps[i + 1]) if 0 < i < Ps.size - 1 else 0.0
    delta = 0.5 * (Ps[i - 1] - Ps[i + 1]) / denom if denom != 0 else 0.0
    df = fb[1] - fb[0]
    f0 = fb[i] + max(-0.5, min(0.5, delta)) * df
    return float(f0), float(snr)


def _smoothed(key, rate):
    """Temporal median of recent estimates so the number stops jittering."""
    q = _recent[key]
    q.append(rate)
    return float(np.median(q))


def estimate():
    """Build the full vitals estimate from the current buffer, or None."""
    tu, X = resample_matrix()
    if X is None:
        return None
    combined, sel = sanitize_and_select(X)
    if combined is None:
        return None
    out = {"sel": sel.tolist()}
    for key, (b, a, band) in (("hr", (b_hr, a_hr, HR_BAND)),
                              ("rr", (b_rr, a_rr, RR_BAND))):
        xf = sig.filtfilt(b, a, sig.detrend(combined))
        nper = min(len(xf), 1024)
        f, P = sig.welch(xf, fs=FS, nperseg=nper, nfft=NFFT, scaling="spectrum")
        f0, snr = robust_peak(f, P, *band)
        rate = round(f0 * 60.0, 1) if f0 and snr >= SNR_MIN else None
        out["snr_" + key] = round(snr, 1) if snr else None
        out[key] = _smoothed(key, rate) if rate is not None else None
        if key == "rr":
            out["waveform"] = np.round(xf, 4).tolist()
            out["time_axis"] = np.round(tu[:len(xf)], 3).tolist()
        if key == "hr":
            show = f <= 3.0
            out["fft_freq"] = np.round(f[show], 4).tolist()
            out["fft_mag"] = np.round(np.sqrt(P[show]), 4).tolist()
    return out


# ---- MQTT plumbing ----
raw_file = csv_writer = None
msg_count = 0
start_time = None
participant_id = "P001"


def now():
    return datetime.now().strftime("%H:%M:%S")


def publish_vitals(client, est, rssi):
    if est is None or est.get("hr") is None:
        return
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "hr": est["hr"],
        "rr": est["rr"] if est.get("rr") is not None else 0.0,
        "rssi": rssi,
        "time_axis": est.get("time_axis", [0.0]),
        "time_wave": est.get("waveform", [0.0]),
        "fft_freq": est.get("fft_freq", []),
        "fft_mag": est.get("fft_mag", []),
    }
    client.publish(VITALS_TOPIC, json.dumps(payload))


def handle_packet(payload, mqtt_client=None):
    """Shared by live MQTT and offline replay."""
    global msg_count, start_time
    if start_time is None:
        start_time = time.time()
    msg_count += 1
    try:
        c = extract_complex(payload)
    except Exception as e:
        if msg_count % 200 == 1:
            print(f"[{now()}] skip malformed packet #{msg_count}: {e}")
        return
    if c is None:
        return
    rssi = payload.get("rssi", 0)

    if raw_file:
        raw_file.write(json.dumps(payload) + "\n")
        raw_file.flush()

    CSI_BUF.append(c)
    CSI_BUF_TS.append(time.time())
    while len(CSI_BUF) > MAX_BUF:
        CSI_BUF.popleft()
        CSI_BUF_TS.popleft()

    est = estimate()
    if est is not None and est.get("hr") is not None:
        print(f"[{now()}] HR={est['hr']:.1f} bpm (SNR {est['snr_hr']})  "
              f"RR={est['rr']} br/min (SNR {est['snr_rr']})  "
              f"sel={len(est['sel'])} buf={len(CSI_BUF)}")
        if csv_writer:
            csv_writer.writerow([datetime.now().isoformat(timespec='seconds'),
                                 est["hr"],
                                 est["rr"] if est["rr"] is not None else "",
                                 participant_id])
        if mqtt_client is not None:
            publish_vitals(mqtt_client, est, rssi)

    if msg_count % 100 == 0:
        elapsed = time.time() - start_time
        rate = msg_count / elapsed if elapsed > 0 else 0
        print(f"[{now()}] {msg_count} pkts, avg {rate:.1f} pkt/s, buf={len(CSI_BUF)}")


def replay(path, rate):
    """Feed a captured ndjson file through the pipeline with simulated timing."""
    global start_time, msg_count
    t0 = time.time()
    dt = 1.0 / rate
    start_time = t0
    print(f"[replay] {path}  assumed {rate} pkt/s  window={WINDOW_SEC}s")
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            try:
                c = extract_complex(payload)
            except Exception:
                continue
            msg_count += 1
            CSI_BUF.append(c)
            CSI_BUF_TS.append(t0 + msg_count * dt)
            while len(CSI_BUF) > MAX_BUF:
                CSI_BUF.popleft()
                CSI_BUF_TS.popleft()
            est = estimate()
            if est is not None and est.get("hr") is not None:
                print(f"[replay] HR={est['hr']:.1f} bpm (SNR {est['snr_hr']})  "
                      f"RR={est['rr']} br/min (SNR {est['snr_rr']})  "
                      f"sel={len(est['sel'])} buf={len(CSI_BUF)}")
    print(f"[replay] done, {msg_count} packets processed")


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        topic = userdata["topic"]
        print(f"[{now()}] CONNECTED, subscribing to '{topic}'")
        client.subscribe(topic)
    else:
        print(f"[{now()}] CONNECT FAILED rc={rc}")


def on_disconnect(client, userdata, rc, properties=None):
    print(f"[{now()}] DISCONNECTED rc={rc} (auto-reconnecting)")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception as e:
        print(f"[{now()}] bad packet: {e}")
        return
    handle_packet(payload, mqtt_client=client)


def main():
    global raw_file, csv_writer, participant_id

    p = argparse.ArgumentParser(description="ESP32-S3 CSI HR/RR (improved pipeline)")
    p.add_argument("--broker", default="xg-6.frp.one")
    p.add_argument("--port", type=int, default=63992)
    p.add_argument("--topic", default="me41004/csi")
    p.add_argument("--participant", default="P001")
    p.add_argument("--replay", default=None,
                   help="offline: replay a captured csi_raw.ndjson file")
    p.add_argument("--replay-rate", type=float, default=40.0,
                   help="assumed packet rate (pkt/s) for replay")
    args = p.parse_args()
    participant_id = args.participant

    base = os.path.dirname(os.path.abspath(__file__))
    RAW_PATH = os.path.join(base, "csi_raw_v2.ndjson")
    CSV_PATH = os.path.join(base, "csi_results_v2.csv")

    print(f"[{now()}] FS={FS}Hz  window={WINDOW_SEC}s  "
          f"HR={HR_BAND}  RR={RR_BAND}  N_SELECT={N_SELECT}  SNR_MIN={SNR_MIN}")

    if args.replay:
        replay(args.replay, args.replay_rate)
        return

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt not installed; run: py -m pip install paho-mqtt")
        sys.exit(1)

    raw_file = open(RAW_PATH, "a", encoding="utf-8")
    csv_file = open(CSV_PATH, "a", newline="", encoding="utf-8")
    csv_writer = csv.writer(csv_file)
    if csv_file.tell() == 0:
        csv_writer.writerow(["timestamp", "hr", "resp_rate", "participant_id"])

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="csi-vitals-v2",
        userdata={"topic": args.topic},
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=10)

    print(f"[{now()}] connecting to {args.broker}:{args.port} topic='{args.topic}'")
    print(f"[{now()}] raw  -> {RAW_PATH}")
    print(f"[{now()}] csv  -> {CSV_PATH}")
    print(f"[{now()}] vitals -> MQTT '{VITALS_TOPIC}'\n")

    client.connect(args.broker, args.port, keepalive=60)
    client.loop_forever(retry_first_connection=True)
    raw_file.close()
    csv_file.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{now()}] stopped, total packets: {msg_count}")
