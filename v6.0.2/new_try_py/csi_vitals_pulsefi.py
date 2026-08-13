#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP32-S3 CSI -> heart rate + respiration via the Pulse-Fi 5-step pipeline.

This is a faithful implementation of the Pulse-Fi methodology, adapted for a
single-antenna ESP32-S3 (where phase differences are unavailable, so amplitude
is the only viable carrier of vital-signs information).

THE FIVE STEPS (exactly as in the Pulse-Fi paper)
--------------------------------------------------
  Step 1  Amplitude Conversion      |H_k| = sqrt(I^2 + Q^2), discard phase
  Step 2  Stationary Noise Removal  subtract the mean (remove DC component)
  Step 3  Pulse Extraction          3rd-order Butterworth bandpass
                                     HR: 0.8-2.17 Hz (48-130 bpm)
                                     RR: 0.1-0.5  Hz (6 -30 br/min)
  Step 4  Pulse Shaping             Savitzky-Golay, window=15, order=3
  Step 5  Segmentation + Normalize  overlapping windows -> normalize -> rate

The paper feeds Step-5 windows into an LSTM. Since no trained LSTM is bundled
here, Step 5 estimates the rate with an FFT peak (+ SNR gate + temporal
median smoothing) so the number is stable. The windowing/normalization is
written so that dropping in an LSTM later is a one-function change.

OFFLINE TUNING (no ESP32 needed)
--------------------------------
    py csi_vitals_pulsefi.py --replay csi_raw.ndjson --replay-rate 40
LIVE
----
    py csi_vitals_pulsefi.py --broker 172.20.10.5 --port 1883

Dependencies: paho-mqtt numpy scipy
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import deque
from datetime import datetime

try:
    import numpy as np
    from scipy import signal as sig
except ImportError as e:
    print("Missing dependency:", e)
    print("Run:  py -m pip install numpy scipy")
    sys.exit(1)


# ======================================================================
#  PULSE-FI PARAMETERS  (paper-specified values are commented "Pulse-Fi")
# ======================================================================
FS = 40                       # processing / resampling grid (Hz)

HR_BAND = (0.80, 2.17)        # Pulse-Fi: 48-130 bpm
RR_BAND = (0.10, 0.50)        # respiration: 6-30 br/min
BUTTER_ORDER = 3              # Pulse-Fi: 3rd-order Butterworth
SG_WINDOW = 15                # Pulse-Fi: Savitzky-Golay window length = 15
SG_POLY = 3                   # Pulse-Fi: polynomial order = 3
WIN_PACKETS = 100             # Pulse-Fi: overlapping window of 100 packets

RR_HARMONICS_MAX = 20         # cancel up to Nth harmonic of breathing in HR band

ANALYSIS_SEC = 20             # length of the analysis window (s) -> 0.05 Hz resolution
NFFT = 2048                   # FFT zero-pad length
SNR_MIN = 2.5                 # reject an estimate whose peak SNR is below this
RATE_SMOOTH = 7               # temporal median over this many estimates (more for stability)

# precompute the Step-3 bandpass filters once
b_hr, a_hr = sig.butter(BUTTER_ORDER, list(HR_BAND), btype="band", fs=FS)
b_rr, a_rr = sig.butter(BUTTER_ORDER, list(RR_BAND), btype="band", fs=FS)

VITALS_TOPIC = "me41004/vitals"

# ---- rolling buffers: one amplitude sample per packet + its timestamp ----
AMP_BUF = deque()             # float amplitude (Step-1 output) per packet
AMP_BUF_TS = deque()          # wall-clock timestamp per packet
MAX_BUF = FS * ANALYSIS_SEC * 2 + 64

_recent = {"hr": deque(maxlen=RATE_SMOOTH), "rr": deque(maxlen=RATE_SMOOTH)}

raw_file = csv_writer = None
msg_count = 0
start_time = None
participant_id = "P001"
target_mac = None          # only process packets from this MAC (auto-set if None)
mac_stats = {}             # {mac: {"count": int, "rssi_sum": float}}
MAC_LOCK_MIN = 40          # packets before we commit to the dominant MAC


def now():
    return datetime.now().strftime("%H:%M:%S")


# ======================================================================
#  MAC FILTER -- isolate a single transmitter
# ======================================================================
def accept_mac(payload):
    """Return True if this packet should enter the pipeline.

    Your ESP32 topic carries CSI from MANY devices (the channel is shared).
    Mixing transmitters with different positions/multipath/antenna patterns
    into one buffer creates huge amplitude steps that look like strong
    vital-signs signal but are pure artefact. We keep only the dominant MAC.
    """
    global target_mac
    mac = payload.get("mac", "")
    rssi = payload.get("rssi", 0)
    if target_mac is None:
        # accumulation phase: tally MACs until we've seen enough to decide
        s = mac_stats.setdefault(mac, {"count": 0, "rssi_sum": 0.0})
        s["count"] += 1
        s["rssi_sum"] += rssi
        total = sum(v["count"] for v in mac_stats.values())
        if total >= MAC_LOCK_MIN:
            target_mac = max(mac_stats, key=lambda m: mac_stats[m]["count"])
            print(f"[{now()}] MAC LOCKED -> {target_mac} "
                  f"({mac_stats[target_mac]['count']}/{total} pkts)")
        return True                 # during accumulation, accept everything
    return mac == target_mac


# ======================================================================
#  STEP 1 -- AMPLITUDE CONVERSION
# ======================================================================
LLTF_MAX_INDEX = 63         # subcarriers 0-63 = L-LTF (stable across packet types)

def step1_amplitude(csi_payload):
    """|H_k| = sqrt(I_k^2 + Q_k^2) for L-LTF subcarriers (0-63), then average.

    Phase is discarded. Only the L-LTF subcarriers (indices 0-63) are used
    because the HT-LTF subcarriers (64+) have ~2x amplitude on HT-modulated
    packets vs legacy packets; mixing them produces the sharp amplitude
    spikes that dominated the FFT. Null / guard subcarriers are dropped.
    Returns a single float (one amplitude sample per packet) or None."""
    sc = csi_payload.get("subcarriers", [])
    if isinstance(sc, str):
        sc = json.loads(sc)
    try:
        arr = np.asarray(sc, dtype=np.float64)
    except Exception:
        return None
    if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] == 0:
        return None
    amps = np.sqrt(arr[:, 0] ** 2 + arr[:, 1] ** 2)
    lltf = amps[:min(LLTF_MAX_INDEX + 1, len(amps))]   # L-LTF subcarriers only
    active = lltf[lltf > 0]                        # drop null subcarriers
    return float(np.mean(active)) if active.size else None


# ======================================================================
#  STEP 2 -- STATIONARY NOISE REMOVAL
# ======================================================================
def step2_remove_dc(x):
    """Subtract the mean (remove the DC component).

    Static reflectors (walls, furniture) and hardware offsets produce a
    constant baseline. Removing it leaves only the time-varying part that
    carries the vital signs (Pulse-Fi Step 2)."""
    return x - np.mean(x)


# ======================================================================
#  STEP 3 -- PULSE EXTRACTION  (3rd-order Butterworth bandpass)
# ======================================================================
def step3_bandpass(x, b, a):
    """Zero-phase 3rd-order Butterworth bandpass filter.

    HR band 0.8-2.17 Hz isolates the heartbeat; everything below (breathing,
    drift) and above (high-frequency hardware noise) is cut. Third order is
    the efficiency/flatness sweet spot found by the paper (no passband
    ripple). filtfilt gives zero phase distortion (Pulse-Fi Step 3)."""
    return sig.filtfilt(b, a, x)


# ======================================================================
#  STEP 4 -- PULSE SHAPING  (Savitzky-Golay)
# ======================================================================
def step4_savgol(x):
    """Savitzky-Golay smoothing, window=15, polyorder=3.

    A local cubic-polynomial fit over a 15-sample window removes the
    high-frequency jaggedness left by the bandpass while preserving the true
    shape of each pulse (Pulse-Fi Step 4)."""
    w = SG_WINDOW
    # window must be odd and larger than the polynomial order
    if w >= len(x):
        w = len(x) - 1 if (len(x) - 1) % 2 == 0 else len(x) - 2
    if w <= SG_POLY + 1:
        return x
    if w % 2 == 0:
        w -= 1
    return sig.savgol_filter(x, w, SG_POLY)


# ======================================================================
#  STEP 5 -- SEGMENTATION + NORMALIZATION + RATE ESTIMATE
# ======================================================================
def step5_estimate(x, band_lo, band_hi, fs=FS):
    """Normalize the cleaned window, then find the dominant frequency.

    Normalization (z-score) matches the paper's Step 5 preparation for the
    neural network; for FFT-based estimation it equalizes the spectrum scale.
    Rate is read from a Welch periodogram (low-variance, so the spectrum
    shows few spurious peaks) with an SNR gate and parabolic sub-bin
    interpolation. Returns (rate_bpm, snr, freq_list, mag_list) or None."""
    std = np.std(x)
    xn = (x - np.mean(x)) / (std + 1e-9) if std > 1e-9 else x - np.mean(x)

    nper = min(len(xn), 512)
    if nper < 16:
        return None
    f, P = sig.welch(xn, fs=fs, nperseg=nper, nfft=NFFT, scaling="spectrum")

    # --- robust peak within the band ---
    in_band = (f >= band_lo) & (f <= band_hi)
    fb, Pb = f[in_band], P[in_band]
    if fb.size < 5:
        return None
    Ps = np.convolve(Pb, np.ones(3) / 3.0, mode="same")   # light smoothing
    i = int(np.argmax(Ps))
    # local noise floor = median of band excluding peak neighbourhood
    nbr = np.ones(fb.size, bool)
    nbr[max(0, i - 3):min(fb.size, i + 4)] = False
    floor = np.median(Ps[nbr]) if nbr.any() else 1e-12
    snr = Ps[i] / (floor + 1e-12)
    # parabolic interpolation -> sub-bin frequency
    denom = (Ps[i - 1] - 2 * Ps[i] + Ps[i + 1]) if 0 < i < Ps.size - 1 else 0.0
    delta = 0.5 * (Ps[i - 1] - Ps[i + 1]) / denom if denom != 0 else 0.0
    df = fb[1] - fb[0]
    f0 = fb[i] + max(-0.5, min(0.5, delta)) * df
    rate = round(f0 * 60.0, 1)

    show = f <= 3.0
    return rate, round(snr, 1), f[show].tolist(), np.sqrt(P[show]).tolist()


def step5_full(x, band_lo, band_hi, fs=FS):
    """Like step5_estimate but also returns the peak frequency (Hz) and index,
    so the analysis log can mark the chosen peak on the spectrum."""
    res = step5_estimate(x, band_lo, band_hi, fs)
    if res is None:
        return None
    rate, snr, freq, mag = res
    f0 = round(rate / 60.0, 4)
    # find the index in freq[] closest to f0 (for plotting the marker)
    peak_i = int(min(range(len(freq)), key=lambda j: abs(freq[j] - f0)))
    return rate, snr, freq, mag, f0, peak_i


def _smoothed(key, rate):
    """Temporal median of recent estimates so the printed number is stable."""
    q = _recent[key]
    q.append(rate)
    return float(np.median(q))


# ======================================================================
#  RATE-ADAPTIVE RESAMPLING  (keeps the pipeline correct at any pkt rate)
# ======================================================================
def build_uniform_window(duration=ANALYSIS_SEC, fs=FS):
    """Interpolate the most recent `duration` s of amplitudes onto a uniform
    fs grid. Returns a numpy array of length fs*duration, or None."""
    if len(AMP_BUF_TS) < 8:
        return None
    ts = np.fromiter(AMP_BUF_TS, dtype=float)
    amp = np.fromiter(AMP_BUF, dtype=float)
    t_end = ts[-1]
    mask = ts >= (t_end - duration)
    ts_w, amp_w = ts[mask], amp[mask]
    if ts_w.size < 8:
        return None
    ok = np.concatenate(([True], np.diff(ts_w) > 1e-6))
    ts_w, amp_w = ts_w[ok], amp_w[ok]
    if ts_w.size < 8:
        return None
    ts_rel = ts_w - ts_w[0]
    N = int(fs * duration)
    tu = np.linspace(0, duration, N)
    return np.interp(tu, ts_rel, amp_w)


def cancel_breathing_harmonics(x, f_rr, fs=FS):
    """Comb-notch filter: remove all harmonics of the breathing frequency.

    Breathing is not a perfect sine; its harmonics extend well into the
    heart-rate band (the 6th-7th harmonic of ~0.14 Hz lands at ~0.9 Hz,
    which the HR bandpass mistake for heartbeat). This cascades narrow
    notch filters at f_rr, 2*f_rr, ..., 20*f_rr so only non-harmonic
    content remains for the HR channel.
    """
    xn = x.copy()
    for n in range(1, RR_HARMONICS_MAX + 1):
        f_notch = f_rr * n
        if f_notch >= fs / 2:
            break
        Q = max(10.0, f_notch / max(0.01, fs / NFFT))   # narrow notch
        bn, an = sig.iirnotch(f_notch, Q, fs=fs)
        xn = sig.filtfilt(bn, an, xn)
    return xn


def estimate():
    """Run Steps 2-5 on the current window; return a rich dict (used by both
    MQTT publish and the analysis log), or None."""
    x = build_uniform_window()
    if x is None:
        return None

    out = {
        # raw Step-1 amplitude on the uniform grid, BEFORE any filtering.
        # This is the ground-truth input to the whole pipeline.
        "raw_amp": np.round(x, 4).tolist(),
        "time_axis": np.round(np.linspace(0, ANALYSIS_SEC, len(x)), 3).tolist(),
    }

    # Step 2: remove static-path DC over the whole window
    x = step2_remove_dc(x)

    # ---- RR first: we need the breathing frequency for HR harmonic cancellation ----
    xf_rr = step3_bandpass(x, b_rr, a_rr)
    xs_rr = step4_savgol(xf_rr)
    res_rr = step5_full(xs_rr, RR_BAND[0], RR_BAND[1])

    # ---- HR: cancel breathing harmonics before bandpass ----
    x_hr = x
    if res_rr is not None:
        f_rr = res_rr[4]  # f0 from step5_full
        if f_rr and f_rr > 0.01:
            x_hr = cancel_breathing_harmonics(x, f_rr)

    xf_hr = step3_bandpass(x_hr, b_hr, a_hr)
    xs_hr = step4_savgol(xf_hr)
    res_hr = step5_full(xs_hr, HR_BAND[0], HR_BAND[1])

    for key, res in (("rr", res_rr), ("hr", res_hr)):
        xs = xs_rr if key == "rr" else xs_hr
        if res is None:
            out[key] = None
            out["snr_" + key] = None
            continue
        rate, snr, freq, mag, f0, peak_i = res
        ok = snr is not None and snr >= SNR_MIN
        out[key] = _smoothed(key, rate) if ok else None
        out["snr_" + key] = snr
        out[key + "_wave"] = np.round(xs, 4).tolist()
        out[key + "_fft_freq"] = np.round(freq, 4).tolist()
        out[key + "_fft_mag"] = np.round(mag, 4).tolist()
        out[key + "_peak_hz"] = round(f0, 4)
    return out


# ======================================================================
#  MQTT / LOGGING PLUMBING
# ======================================================================
def publish_vitals(client, est, rssi):
    if est is None or est.get("hr") is None:
        return
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "hr": est["hr"],
        "rr": est["rr"] if est.get("rr") is not None else 0.0,
        "rssi": rssi,
        "time_axis": est.get("time_axis", [0.0]),
        "time_wave": est.get("rr_wave", [0.0]),
        "fft_freq": est.get("hr_fft_freq", []),
        "fft_mag": est.get("hr_fft_mag", []),
    }
    client.publish(VITALS_TOPIC, json.dumps(payload))


# ---- analysis log: write every snapshot to one NDJSON file ----
analysis_log = None
log_every = 1
ANALYSIS_LOG_PATH = ""


def write_analysis(est, rssi, when=None):
    """Append one complete snapshot (RSSI, HR, RR, SNR, raw amplitude,
    both waveforms, both spectra, peak markers) as a single JSON line."""
    if analysis_log is None or est is None:
        return
    rec = {
        "idx": msg_count,
        "ts": (when or datetime.now()).isoformat(timespec="seconds"),
        "rssi": rssi,
        "fs": FS,
        "window_sec": ANALYSIS_SEC,
        "hr": est.get("hr"),
        "snr_hr": est.get("snr_hr"),
        "rr": est.get("rr"),
        "snr_rr": est.get("snr_rr"),
        "hr_peak_hz": est.get("hr_peak_hz"),
        "rr_peak_hz": est.get("rr_peak_hz"),
        "hr_band": list(HR_BAND),
        "rr_band": list(RR_BAND),
        "time_axis": est.get("time_axis", []),
        "raw_amp": est.get("raw_amp", []),
        "hr_wave": est.get("hr_wave", []),
        "rr_wave": est.get("rr_wave", []),
        "hr_fft_freq": est.get("hr_fft_freq", []),
        "hr_fft_mag": est.get("hr_fft_mag", []),
        "rr_fft_freq": est.get("rr_fft_freq", []),
        "rr_fft_mag": est.get("rr_fft_mag", []),
    }
    analysis_log.write(json.dumps(rec) + "\n")
    analysis_log.flush()


def handle_packet(payload, mqtt_client=None):
    """Shared by live MQTT and offline replay."""
    global msg_count, start_time
    if start_time is None:
        start_time = time.time()
    msg_count += 1

    # MAC filter: only keep the dominant transmitter's packets.
    if not accept_mac(payload):
        return

    # Step 1 happens here: amplitude extraction, one float per packet.
    amp = step1_amplitude(payload)
    if amp is None:
        if msg_count % 200 == 1:
            print(f"[{now()}] skip malformed packet #{msg_count}")
        return
    rssi = payload.get("rssi", 0)

    if raw_file:
        raw_file.write(json.dumps(payload) + "\n")
        raw_file.flush()

    AMP_BUF.append(amp)
    AMP_BUF_TS.append(time.time())
    while len(AMP_BUF) > MAX_BUF:
        AMP_BUF.popleft()
        AMP_BUF_TS.popleft()

    est = estimate()
    if est is not None and est.get("hr") is not None:
        print(f"[{now()}] HR={est['hr']:.1f} bpm (SNR {est['snr_hr']})  "
              f"RR={est['rr']} br/min (SNR {est['snr_rr']})  "
              f"buf={len(AMP_BUF)}")
        if csv_writer:
            csv_writer.writerow([datetime.now().isoformat(timespec='seconds'),
                                 est["hr"],
                                 est["rr"] if est["rr"] is not None else "",
                                 participant_id])
        if mqtt_client is not None:
            publish_vitals(mqtt_client, est, rssi)

    # analysis log: capture full snapshot at a reduced cadence
    if est is not None and analysis_log is not None and msg_count % log_every == 0:
        write_analysis(est, rssi)

    if msg_count % 100 == 0:
        elapsed = time.time() - start_time
        rate = msg_count / elapsed if elapsed > 0 else 0
        print(f"[{now()}] {msg_count} pkts, avg {rate:.1f} pkt/s, "
              f"buf={len(AMP_BUF)}")


def replay(path, rate):
    """Feed a captured ndjson through the pipeline with simulated timing."""
    global start_time, msg_count
    t0 = time.time()
    dt = 1.0 / rate
    start_time = t0
    print(f"[replay] {path}  assumed {rate} pkt/s  window={ANALYSIS_SEC}s")
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            # MAC filter in replay mode too
            if not accept_mac(payload):
                continue

            amp = step1_amplitude(payload)
            if amp is None:
                continue
            msg_count += 1
            AMP_BUF.append(amp)
            AMP_BUF_TS.append(t0 + msg_count * dt)
            while len(AMP_BUF) > MAX_BUF:
                AMP_BUF.popleft()
                AMP_BUF_TS.popleft()
            est = estimate()
            if est is not None and est.get("hr") is not None:
                print(f"[replay] HR={est['hr']:.1f} bpm (SNR {est['snr_hr']})  "
                      f"RR={est['rr']} br/min (SNR {est['snr_rr']})  "
                      f"buf={len(AMP_BUF)}")
            # analysis log in replay mode too
            if est is not None and analysis_log is not None and msg_count % log_every == 0:
                write_analysis(est, payload.get('rssi', 0))
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
    global raw_file, csv_writer, participant_id, analysis_log, log_every, ANALYSIS_LOG_PATH, target_mac

    p = argparse.ArgumentParser(
        description="Pulse-Fi 5-step CSI HR/RR pipeline (ESP32-S3)")
    p.add_argument("--broker", default="172.20.10.5")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--topic", default="me41004/csi")
    p.add_argument("--participant", default="P001")
    p.add_argument("--replay", default=None,
                   help="offline: replay a captured csi_raw.ndjson file")
    p.add_argument("--replay-rate", type=float, default=40.0,
                   help="assumed packet rate (pkt/s) for replay")
    p.add_argument("--log", default=None,
                   help="write full per-snapshot analysis to this NDJSON file")
    p.add_argument("--log-every", type=int, default=10,
                   help="log one snapshot every N packets (default 10)")
    p.add_argument("--mac", default=None,
                   help="only process packets from this MAC (auto-detect if omitted)")
    args = p.parse_args()
    participant_id = args.participant
    log_every = max(1, args.log_every)
    if args.mac:
        target_mac = args.mac
        print(f"[{now()}] MAC filter forced -> {target_mac}")

    base = os.path.dirname(os.path.abspath(__file__))
    RAW_PATH = os.path.join(base, "csi_raw_pulsefi.ndjson")
    CSV_PATH = os.path.join(base, "csi_results_pulsefi.csv")
    ANALYSIS_LOG_PATH = args.log or ""

    print(f"[{now()}] === Pulse-Fi pipeline ===")
    print(f"[{now()}] FS={FS}Hz  window={ANALYSIS_SEC}s")
    print(f"[{now()}] Step3: Butterworth order={BUTTER_ORDER}  "
          f"HR={HR_BAND}  RR={RR_BAND}")
    print(f"[{now()}] Step4: Savitzky-Golay window={SG_WINDOW} order={SG_POLY}")
    print(f"[{now()}] Step5: SNR_MIN={SNR_MIN}  smooth={RATE_SMOOTH}")
    if ANALYSIS_LOG_PATH:
        print(f"[{now()}] analysis log -> {ANALYSIS_LOG_PATH} "
              f"(every {log_every} pkts)")

    if args.replay:
        if ANALYSIS_LOG_PATH:
            analysis_log = open(ANALYSIS_LOG_PATH, "w", encoding="utf-8")
        replay(args.replay, args.replay_rate)
        if analysis_log:
            analysis_log.close()
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
    if ANALYSIS_LOG_PATH:
        analysis_log = open(ANALYSIS_LOG_PATH, "w", encoding="utf-8")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="csi-pulsefi",
        userdata={"topic": args.topic},
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=10)

    print(f"[{now()}] connecting to {args.broker}:{args.port} "
          f"topic='{args.topic}'")
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
