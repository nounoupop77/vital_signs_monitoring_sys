#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MQTT subscriber: receive CSI data -> filter -> FFT -> display HR/RR.

Rate-adaptive version: the signal-processing math no longer assumes the
ESP32 delivers a fixed 40 packets/sec. Every packet is timestamped, and
each analysis window is resampled onto a uniform 40 Hz grid before
filtering/FFT. This keeps HR/RR accurate whether packets arrive at
28/s, 36/s, 40/s, or anywhere in between.

Based on the ME41004 lab spec (PDF page 6):
  1. Parse CSI JSON, compute amplitude per subcarrier pair: |H| = sqrt(I^2 + Q^2)
  2. Average across subcarriers -> one amplitude sample per packet
  3. Rolling buffer (10 s window)
  4. Bandpass filter (Butterworth 4th order)
       Heart rate band : 0.8 - 2.17 Hz  (48 - 130 bpm)
       Respiration band: 0.1  - 0.5  Hz (6  - 30  br/min)
  5. Savitzky-Golay smoothing (window=11, order=3)
  6. Hanning window -> FFT -> peak detection -> bpm / brpm

Usage:
    py csi_subscriber.py
    py csi_subscriber.py --broker localhost --topic me41004/csi --participant P001

Dependencies:
    py -m pip install paho-mqtt numpy scipy
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

try:
    import numpy as np
    from scipy import signal as sig
    import paho.mqtt.client as mqtt
except ImportError as e:
    print("Missing dependency:", e)
    print("Run:  py -m pip install paho-mqtt numpy scipy")
    sys.exit(1)


# ---- Signal processing parameters (from lab spec) ----
# FS is now the PROCESSING rate (uniform grid we resample onto), NOT an
# assumption about the ESP32 publish rate. The actual packet rate is
# measured from timestamps and can be anything ~20-50 Hz.
FS = 40            # Processing/resampling grid frequency (Hz)
BUFFER_SEC = 10    # Process data in 10-second windows
BUF_SIZE = FS * BUFFER_SEC   # 400 points on the uniform grid

# Bandpass filter coefficients (Butterworth, 4th order), designed once at FS
b_hr, a_hr     = sig.butter(4, [0.8, 2.17], btype="band", fs=FS)   # heart rate
b_resp, a_resp = sig.butter(4, [0.1, 0.5],  btype="band", fs=FS)   # respiration

# === ADDED FOR C# GUI: topic where computed vitals are published ===
VITALS_TOPIC = "me41004/vitals"
# === END ADDED ===


def extract_amplitudes(csi_payload):
    """Parse CSI JSON -> average amplitude across all subcarrier pairs."""
    subcarriers = csi_payload.get("subcarriers", [])
    if isinstance(subcarriers, str):
        subcarriers = json.loads(subcarriers)
    amps = []
    for pair in subcarriers:
        i, q = pair[0], pair[1]
        amps.append(np.sqrt(i**2 + q**2))   # |H| = sqrt(I^2 + Q^2)
    return float(np.mean(amps)) if amps else 0.0


def resample_window(target_fs=FS, duration=BUFFER_SEC):
    """Build a uniformly-sampled amplitude window from the most recent
    `duration` seconds of real data.

    This is the core of the rate-adaptive design. CSI packets are stamped
    with wall-clock time when they arrive. We take every packet inside the
    last `duration` seconds and linearly interpolate its amplitudes onto a
    uniform grid of (target_fs * duration) points. Because the grid is
    always uniform at FS Hz, the downstream Butterworth filters and FFT are
    always given correctly-spaced samples regardless of whether packets
    actually arrived at 28/s, 36/s, or 40/s.

    Returns a numpy array of length BUF_SIZE, or None if there is not yet
    enough data in the window."""
    if len(CSI_BUF_TS) < 4:
        return None
    ts = np.asarray(CSI_BUF_TS, dtype=float)
    amp = np.asarray(CSI_BUF, dtype=float)
    # Keep only the most recent `duration` seconds (by real time, not count).
    t_end = ts[-1]
    t_start = t_end - duration
    mask = ts >= t_start
    ts_w = ts[mask]
    amp_w = amp[mask]
    if len(ts_w) < 4:
        return None
    # np.interp requires strictly increasing x. Drop duplicate timestamps.
    ok = np.concatenate(([True], np.diff(ts_w) > 1e-6))
    ts_w = ts_w[ok]
    amp_w = amp_w[ok]
    if len(ts_w) < 4:
        return None
    # Shift so the window starts at t=0.
    ts_rel = ts_w - ts_w[0]
    n = target_fs * duration          # always 400 points
    t_uniform = np.linspace(0, duration, n)
    return np.interp(t_uniform, ts_rel, amp_w)


# ---- compute_rate: now operates on the resampled uniform window ----
def compute_rate(b, a, band_lo, band_hi):
    """Filter -> smooth -> Hanning -> FFT -> peak in band -> bpm/brpm.

    Uses resample_window() internally so the result is correct at any
    real packet arrival rate."""
    x = resample_window()
    if x is None or len(x) < BUF_SIZE:
        return None
    x_filt = sig.filtfilt(b, a, x)
    x_smooth = sig.savgol_filter(x_filt, 11, 3)
    window = np.hanning(len(x_smooth))
    x_w = x_smooth * window
    fft = np.fft.fft(x_w, n=2048)
    freq = np.fft.fftfreq(2048, 1 / FS)[:1024]
    mag = np.abs(fft[:1024])
    idx_range = np.where((freq >= band_lo) & (freq <= band_hi))[0]
    if len(idx_range) == 0:
        return None
    peak_idx = idx_range[np.argmax(mag[idx_range])]
    rate_hz = freq[peak_idx]
    return round(rate_hz * 60.0, 1)


def compute_heart_rate():
    return compute_rate(b_hr, a_hr, 0.8, 2.17)


def compute_resp_rate():
    return compute_rate(b_resp, a_resp, 0.1, 0.5)


# === ADDED FOR C# GUI: same as compute_rate but also returns waveform + FFT ===
def compute_rate_full(b, a, band_lo, band_hi):
    """Like compute_rate, but also returns filtered waveform and FFT spectrum
    so the C# GUI can plot them. Also rate-adaptive via resample_window()."""
    x = resample_window()
    if x is None or len(x) < BUF_SIZE:
        return None
    x_filt = sig.filtfilt(b, a, x)
    x_smooth = sig.savgol_filter(x_filt, 11, 3)

    window = np.hanning(len(x_smooth))
    x_w = x_smooth * window
    fft = np.fft.fft(x_w, n=2048)
    freq = np.fft.fftfreq(2048, 1 / FS)[:1024]
    mag = np.abs(fft[:1024])

    idx_range = np.where((freq >= band_lo) & (freq <= band_hi))[0]
    if len(idx_range) == 0:
        return None
    peak_idx = idx_range[np.argmax(mag[idx_range])]
    rate_hz = freq[peak_idx]

    # Time axis: 0 to BUFFER_SEC seconds
    t_axis = np.linspace(0, BUFFER_SEC, len(x_smooth))

    # Trim FFT to 0-3 Hz for display (keeps the MQTT payload small)
    show_mask = freq <= 3.0

    return {
        "rate": round(rate_hz * 60.0, 1),
        "waveform": np.round(x_smooth, 4).tolist(),
        "time_axis": np.round(t_axis, 3).tolist(),
        "fft_freq": np.round(freq[show_mask], 4).tolist(),
        "fft_mag": np.round(mag[show_mask], 4).tolist(),
    }


def publish_vitals(client, hr_data, rr_data, rssi):
    """Package the computed results into a JSON message and publish to the
    vitals topic so the C# GUI can subscribe and plot them."""
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "hr": hr_data["rate"],
        "rr": rr_data["rate"] if rr_data else 0.0,
        "rssi": rssi,
        # Respiration-band waveform for the time-domain plot
        "time_axis": rr_data["time_axis"] if rr_data else [0.0],
        "time_wave": rr_data["waveform"] if rr_data else [0.0],
        # Heart-rate-band FFT spectrum for the FFT plot
        "fft_freq": hr_data["fft_freq"],
        "fft_mag": hr_data["fft_mag"],
    }
    client.publish(VITALS_TOPIC, json.dumps(payload))
# === END ADDED ===


# ---- MQTT plumbing ----
CSI_BUF = []        # rolling buffer of CSI amplitudes
CSI_BUF_TS = []     # wall-clock timestamps aligned with CSI_BUF (rate-adaptive)
raw_file = None
csv_writer = None
msg_count = 0
start_time = None
participant_id = "P001"


def now():
    return datetime.now().strftime("%H:%M:%S")


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
    """Called every time a CSI packet arrives."""
    global msg_count, start_time
    if start_time is None:
        start_time = time.time()
    msg_count += 1

    # Parse + amplitude (guard against malformed packets)
    try:
        payload = json.loads(msg.payload.decode())
        amp = extract_amplitudes(payload)
    except Exception as e:
        if msg_count % 200 == 1:
            print(f"[{now()}] skip malformed packet #{msg_count}: {e}")
        return

    rssi = payload.get("rssi", 0)

    # raw dump (one line per packet) - keeps backpressure low
    if raw_file:
        raw_file.write(json.dumps(payload) + "\n")
        raw_file.flush()

    # Append amplitude AND its real arrival timestamp.
    # The timestamp is what makes the analysis independent of packet rate.
    CSI_BUF.append(amp)
    CSI_BUF_TS.append(time.time())
    # Keep enough history to always cover BUFFER_SEC even at low rates.
    # BUF_SIZE*2 = 800 pts ~ 20s @ 40Hz or ~28s @ 28Hz, plenty of slack.
    MAX_BUF = BUF_SIZE * 2
    if len(CSI_BUF) > MAX_BUF:
        CSI_BUF.pop(0)
        CSI_BUF_TS.pop(0)

    # === ORIGINAL: compute rates for console + CSV (now rate-adaptive) ===
    hr = compute_heart_rate()
    rr = compute_resp_rate()

    if hr is not None:
        print(f"[{now()}] Heart Rate: {hr:.1f} bpm   Resp: {rr if rr else '-'} br/min   (buf={len(CSI_BUF)})")
        if csv_writer:
            csv_writer.writerow([datetime.now().isoformat(timespec='seconds'), hr, rr if rr else "", participant_id])

    # === ADDED FOR C# GUI: compute full results and publish ===
    if hr is not None:
        hr_full = compute_rate_full(b_hr, a_hr, 0.8, 2.17)
        rr_full = compute_rate_full(b_resp, a_resp, 0.1, 0.5)
        if hr_full:
            publish_vitals(client, hr_full, rr_full, rssi)
            print(f"[{now()}] -> published to {VITALS_TOPIC}: HR={hr_full['rate']} RR={rr_full['rate'] if rr_full else '-'}")
    # === END ADDED ===

    if msg_count % 100 == 0:
        elapsed = time.time() - start_time
        rate = msg_count / elapsed if elapsed > 0 else 0
        # Also report the instantaneous rate over the last window so you can
        # see what the network is actually delivering.
        inst = 0.0
        if len(CSI_BUF_TS) >= 2:
            span = CSI_BUF_TS[-1] - CSI_BUF_TS[0]
            inst = (len(CSI_BUF_TS) / span) if span > 0 else 0.0
        print(f"[{now()}] received {msg_count} packets, avg {rate:.1f} pkt/s, "
              f"buf={len(CSI_BUF)} (~{inst:.1f} pkt/s in window)")


def main():
    global raw_file, csv_writer, participant_id

    parser = argparse.ArgumentParser(description="ESP32 CSI MQTT subscriber + HR/RR estimator")
    parser.add_argument("--broker", default="xg-6.frp.one")
    parser.add_argument("--port", type=int, default=63992)
    parser.add_argument("--topic", default="me41004/csi")
    parser.add_argument("--participant", default="P001")
    args = parser.parse_args()
    participant_id = args.participant

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    RAW_PATH = os.path.join(SCRIPT_DIR, "csi_raw.ndjson")
    CSV_PATH = os.path.join(SCRIPT_DIR, "csi_results.csv")
    raw_file = open(RAW_PATH, "a", encoding="utf-8")
    csv_file = open(CSV_PATH, "a", newline="", encoding="utf-8")
    csv_writer = csv.writer(csv_file)
    if csv_file.tell() == 0:
        csv_writer.writerow(["timestamp", "hr", "resp_rate", "participant_id"])

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="csi-subscriber",
        userdata={"topic": args.topic},
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=10)

    print(f"[{now()}] connecting to {args.broker}:{args.port} topic='{args.topic}'")
    print(f"[{now()}] FS={FS}Hz (processing grid)  window={BUFFER_SEC}s  buf_size={BUF_SIZE}")
    print(f"[{now()}] rate-adaptive: actual packet rate is measured, not assumed")
    print(f"[{now()}] raw  -> {RAW_PATH}")
    print(f"[{now()}] csv  -> {CSV_PATH}")
    print(f"[{now()}] vitals -> MQTT topic '{VITALS_TOPIC}' (for C# GUI)")
    print(f"[{now()}] press Ctrl+C to stop\n")

    client.connect(args.broker, args.port, keepalive=60)
    client.loop_forever(retry_first_connection=True)

    raw_file.close()
    csv_file.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{now()}] stopped, total packets: {msg_count}")
        if raw_file:
            raw_file.close()
