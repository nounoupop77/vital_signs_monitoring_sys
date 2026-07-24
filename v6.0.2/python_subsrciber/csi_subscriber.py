#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MQTT subscriber: receive CSI data -> filter -> FFT -> display HR/RR.

Based on the ME41004 lab spec (PDF page 6):
  1. Parse CSI JSON, compute amplitude per subcarrier pair: |H| = sqrt(I^2 + Q^2)
  2. Average across subcarriers -> one amplitude sample per packet
  3. Rolling buffer (10 s window)
  4. Bandpass filter (Butterworth 4th order)
       Heart rate band : 0.8 - 2.17 Hz  (48 - 130 bpm)
       Respiration band: 0.1  - 0.5  Hz (6  - 30  br/min)
  5. Savitzky-Golay smoothing (window=11, order=3)
  6. Hanning window -> FFT -> peak detection -> bpm / brpm

MODIFIED VERSION: also publishes results to topic me41004/vitals so the
C# WPF GUI can subscribe and display them in real time.

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
FS = 40            # Sampling frequency (Hz) - the ESP publish rate target
BUFFER_SEC = 10      # Process data in 10-second windows
BUF_SIZE = FS * BUFFER_SEC

# Bandpass filter coefficients (Butterworth, 4th order)
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


# ---- ORIGINAL compute_rate (unchanged, kept for CSV output) ----
def compute_rate(amplitudes, b, a, band_lo, band_hi):
    """Filter -> smooth -> Hanning -> FFT -> peak in band -> bpm/brpm."""
    if len(amplitudes) < BUF_SIZE:
        return None
    x = np.array(amplitudes[-BUF_SIZE:])
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


def compute_heart_rate(amplitudes):
    return compute_rate(amplitudes, b_hr, a_hr, 0.8, 2.17)


def compute_resp_rate(amplitudes):
    return compute_rate(amplitudes, b_resp, a_resp, 0.1, 0.5)


# === ADDED FOR C# GUI: same as compute_rate but also returns waveform + FFT ===
def compute_rate_full(amplitudes, b, a, band_lo, band_hi):
    """Like compute_rate, but also returns filtered waveform and FFT spectrum
    so the C# GUI can plot them."""
    if len(amplitudes) < BUF_SIZE:
        return None
    x = np.array(amplitudes[-BUF_SIZE:])
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
CSI_BUF = []   # rolling buffer of CSI amplitudes
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

    CSI_BUF.append(amp)
    if len(CSI_BUF) > BUF_SIZE * 2:
        CSI_BUF.pop(0)

    # === ORIGINAL: compute rates for console + CSV ===
    hr = compute_heart_rate(CSI_BUF)
    rr = compute_resp_rate(CSI_BUF)

    if hr is not None:
        print(f"[{now()}] Heart Rate: {hr:.1f} bpm   Resp: {rr if rr else '-'} br/min   (buf={len(CSI_BUF)})")
        if csv_writer:
            csv_writer.writerow([datetime.now().isoformat(timespec='seconds'), hr, rr if rr else "", participant_id])

    # === ADDED FOR C# GUI: compute full results and publish ===
    if hr is not None:
        hr_full = compute_rate_full(CSI_BUF, b_hr, a_hr, 0.8, 2.17)
        rr_full = compute_rate_full(CSI_BUF, b_resp, a_resp, 0.1, 0.5)
        if hr_full:
            publish_vitals(client, hr_full, rr_full, rssi)
            print(f"[{now()}] -> published to {VITALS_TOPIC}: HR={hr_full['rate']} RR={rr_full['rate'] if rr_full else '-'}")
    # === END ADDED ===

    if msg_count % 100 == 0:
        elapsed = time.time() - start_time
        rate = msg_count / elapsed if elapsed > 0 else 0
        print(f"[{now()}] received {msg_count} packets, {rate:.1f} pkt/s, buf={len(CSI_BUF)}")


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
    print(f"[{now()}] FS={FS}Hz  window={BUFFER_SEC}s  buf_size={BUF_SIZE}")
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
