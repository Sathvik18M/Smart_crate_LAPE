"""
Smart Crate - Python Processing Engine + Simple UI
---------------------------------------------------
Architecture:
  ESP32  ->  RAW,millis,temp,hum,gas,ax,ay,az,lat,lon  ->  Python (via Serial)
  Python ->  compute risk (using produce-specific thresholds), detect fall/shock
  Python ->  if trigger: send STORE,... back to ESP32  ->  ESP32 writes to SD card
  Python ->  Streamlit UI shows current readings and trigger log

Serial format from ESP32:
  RAW,<millis>,<temp_c>,<hum_pct>,<gas_adc>,<ax>,<ay>,<az>,<lat>,<lon>

Serial command sent to ESP32 on trigger:
  STORE,<millis>,<temp>,<hum>,<gas>,<accel_g>,<fall>,<lat>,<lon>,<risk>,<status>

Run: streamlit run app.py
"""

import math
import time
import os
from datetime import datetime

import streamlit as st
import serial
import pandas as pd

# ─────────────────────── Default serial config ────────────────
SERIAL_PORT  = "COM5"
BAUD_RATE    = 115200
HISTORY_SIZE = 100

# ─────────────────────── Produce profiles ─────────────────────
# Each produce has its own safe/danger limits for temp, humidity, and gas.
# Risk = 0 at or below safe limit, 100 at or above danger limit, linear in between.
# Humidity limits: above the safe limit = excess moisture -> mold risk.
# Gas limits: MQ sensor ADC value indicating VOC / ethylene accumulation.

PRODUCE_PROFILES = {
    "Tomato": {
        "info":        "Best stored at 10-15 C. Sensitive to ethylene.",
        "temp_safe":   15.0,  "temp_danger":  28.0,
        "hum_safe":    88.0,  "hum_danger":   96.0,
        "gas_safe":    80.0,  "gas_danger":  250.0,
    },
    "Onion": {
        "info":        "Prefers cool and dry. Excess humidity causes rot.",
        "temp_safe":    8.0,  "temp_danger":  22.0,
        "hum_safe":    70.0,  "hum_danger":   85.0,
        "gas_safe":   100.0,  "gas_danger":  300.0,
    },
    "Strawberry / Blueberry": {
        "info":        "Highly perishable. Keep near 0-4 C, high humidity.",
        "temp_safe":    4.0,  "temp_danger":  12.0,
        "hum_safe":    88.0,  "hum_danger":   97.0,
        "gas_safe":    60.0,  "gas_danger":  180.0,
    },
    "Gobi / Cabbage": {
        "info":        "Cool and moist. Spoils quickly above 15 C.",
        "temp_safe":    5.0,  "temp_danger":  18.0,
        "hum_safe":    88.0,  "hum_danger":   96.0,
        "gas_safe":    80.0,  "gas_danger":  250.0,
    },
    "Mango": {
        "info":        "Tropical. Keep 13-15 C. Chilling injury below 10 C.",
        "temp_safe":   15.0,  "temp_danger":  30.0,
        "hum_safe":    85.0,  "hum_danger":   95.0,
        "gas_safe":    80.0,  "gas_danger":  250.0,
    },
    "Apple": {
        "info":        "Best near 0-4 C. Ethylene producer - keep isolated.",
        "temp_safe":    5.0,  "temp_danger":  20.0,
        "hum_safe":    88.0,  "hum_danger":   96.0,
        "gas_safe":    70.0,  "gas_danger":  220.0,
    },
    "Banana": {
        "info":        "Ripens quickly above 20 C. Avoid cold below 12 C.",
        "temp_safe":   16.0,  "temp_danger":  26.0,
        "hum_safe":    85.0,  "hum_danger":   95.0,
        "gas_safe":    80.0,  "gas_danger":  260.0,
    },
}

PRODUCE_LIST = list(PRODUCE_PROFILES.keys())

# ─────────────────────── Risk model constants ─────────────────
W_TEMP, W_HUM, W_GAS = 0.40, 0.35, 0.25
RISK_ALERT   = 70
RISK_CAUTION = 40
FREEFALL_G   = 0.35
SHOCK_G      = 2.50

# ─────────────────────── Processing functions ─────────────────

def _risk_component(value, safe_max, danger_max):
    if value <= safe_max:   return 0.0
    if value >= danger_max: return 100.0
    return (value - safe_max) / (danger_max - safe_max) * 100.0

def compute_risk(temp, hum, gas, profile):
    r = (W_TEMP * _risk_component(temp, profile["temp_safe"], profile["temp_danger"])
       + W_HUM  * _risk_component(hum,  profile["hum_safe"],  profile["hum_danger"])
       + W_GAS  * _risk_component(gas,  profile["gas_safe"],  profile["gas_danger"]))
    return max(0, min(100, round(r)))

def status_label(risk):
    if risk >= RISK_ALERT:   return "ALERT"
    if risk >= RISK_CAUTION: return "CAUTION"
    return "SAFE"

def accel_g(ax, ay, az):
    return math.sqrt(ax**2 + ay**2 + az**2) / 9.81

def is_fall_or_shock(g):
    return g <= FREEFALL_G or g >= SHOCK_G

# ─────────────────────── Serial parsing ───────────────────────

def parse_raw(line: str):
    """Parse  RAW,millis,temp,hum,gas,ax,ay,az,lat,lon  into a dict."""
    parts = line.strip().split(",")
    if len(parts) < 10 or parts[0] != "RAW":
        return None
    try:
        return dict(
            millis = int(parts[1]),
            temp   = float(parts[2]),
            hum    = float(parts[3]),
            gas    = float(parts[4]),
            ax     = float(parts[5]),
            ay     = float(parts[6]),
            az     = float(parts[7]),
            lat    = float(parts[8]),
            lon    = float(parts[9]),
        )
    except (ValueError, IndexError):
        return None

def build_store_cmd(d, g, fall, risk, status_str):
    return (
        f"STORE,{d['millis']},{d['temp']:.1f},{d['hum']:.1f},"
        f"{d['gas']:.0f},{g:.2f},{int(fall)},"
        f"{d['lat']:.6f},{d['lon']:.6f},{risk},{status_str}"
    )

# ─────────────────────── Local log helpers ────────────────────

def log_file_for(produce: str) -> str:
    safe_name = produce.replace(" ", "_").replace("/", "-")
    return f"triggers_{safe_name}.csv"

def ensure_log(log_file: str):
    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            f.write("timestamp,produce,millis,temp_c,humidity_pct,gas_raw,"
                    "accel_g,fall,lat,lon,risk,status,trigger\n")

def log_trigger(log_file, ts, produce, d, g, fall, risk, status_str, reason):
    with open(log_file, "a") as f:
        f.write(f"{ts},{produce},{d['millis']},{d['temp']:.1f},{d['hum']:.1f},"
                f"{d['gas']:.0f},{g:.2f},{int(fall)},"
                f"{d['lat']:.6f},{d['lon']:.6f},{risk},{status_str},{reason}\n")

# ─────────────────────── Streamlit UI ─────────────────────────

st.set_page_config(page_title="Smart Crate", layout="wide")
st.title("Smart Crate — Live Monitor")

# ── Sidebar ──
with st.sidebar:
    st.header("Produce Selection")
    produce = st.selectbox("Crate contents", PRODUCE_LIST, index=0)
    profile = PRODUCE_PROFILES[produce]
    st.caption(profile["info"])

    st.divider()

    st.header("Connection")
    sim_mode = st.checkbox("Simulation mode", value=True)
    if not sim_mode:
        port = st.text_input("Serial port", SERIAL_PORT)
        baud = st.number_input("Baud rate", value=BAUD_RATE, step=100)
    else:
        st.caption("Using simulated data.")
        sim_temp  = st.slider("Temp (C)",     0.0, 50.0, float(profile["temp_safe"]) + 2)
        sim_hum   = st.slider("Humidity (%)", 0.0, 100.0, float(profile["hum_safe"]) - 5)
        sim_gas   = st.slider("Gas ADC",      0.0, 500.0, float(profile["gas_safe"]) + 10)
        sim_shock = st.checkbox("Simulate shock / drop")

    run = st.toggle("Connect and run", value=False)

    st.divider()

    st.caption(f"Thresholds for {produce}")
    st.caption(
        f"  Temp:     safe <= {profile['temp_safe']} C  |  danger >= {profile['temp_danger']} C\n"
        f"  Humidity: safe <= {profile['hum_safe']} %  |  danger >= {profile['hum_danger']} %\n"
        f"  Gas:      safe <= {profile['gas_safe']}    |  danger >= {profile['gas_danger']}\n"
        f"  Shock:    g <= {FREEFALL_G} (freefall) or g >= {SHOCK_G} (impact)"
    )
    st.caption("Triggered events are stored to ESP32 SD card.")

# ── Log file per produce ──
log_file = log_file_for(produce)
ensure_log(log_file)

# ── Session state ──
if "history"        not in st.session_state: st.session_state.history  = []
if "triggers"       not in st.session_state: st.session_state.triggers = []
if "last_produce"   not in st.session_state: st.session_state.last_produce = produce
if "ser"            not in st.session_state: st.session_state.ser = None

# Reset history if produce changed
if st.session_state.last_produce != produce:
    st.session_state.history  = []
    st.session_state.triggers = []
    st.session_state.last_produce = produce

# ── Placeholders ──
status_ph   = st.empty()
readings_ph = st.empty()
trigger_ph  = st.empty()

if not run:
    if st.session_state.ser:
        try: st.session_state.ser.close()
        except: pass
        st.session_state.ser = None

    st.info(f"Selected produce: {produce}. Enable 'Connect and run' to start monitoring.")

    # Show existing log for selected produce
    if os.path.exists(log_file):
        df_log = pd.read_csv(log_file)
        if len(df_log):
            st.subheader(f"Stored trigger log — {produce}")
            st.dataframe(df_log, use_container_width=True, hide_index=True)
            with open(log_file, "r") as f:
                st.download_button(
                    label=f"Export {produce} logs as CSV",
                    data=f.read(),
                    file_name=log_file,
                    mime="text/csv"
                )
    st.stop()

# ── Open serial (real mode only) ──
if not sim_mode:
    if st.session_state.ser is None:
        try:
            st.session_state.ser = serial.Serial(port, baud, timeout=1)
        except Exception as e:
            st.error(f"Cannot open {port}: {e}")
            st.stop()
    ser = st.session_state.ser
else:
    ser = None

import random

last_sim_t = 0.0

# ── Main loop ──
while True:
    d = None

    if sim_mode:
        now = time.time()
        if now - last_sim_t < 1.0:
            time.sleep(0.05)
            continue
        last_sim_t = now

        if sim_shock:
            ax, ay, az = 25.0, 20.0, 15.0
        else:
            ax = random.uniform(-0.1, 0.1)
            ay = random.uniform(-0.1, 0.1)
            az = 9.81 + random.uniform(-0.05, 0.05)

        d = dict(
            millis = int(now * 1000) % 100_000_000,
            temp   = sim_temp + random.uniform(-0.3, 0.3),
            hum    = sim_hum  + random.uniform(-0.5, 0.5),
            gas    = max(0, sim_gas + random.uniform(-2, 2)),
            ax=ax, ay=ay, az=az,
            lat = 12.9716 + random.uniform(-0.0003, 0.0003),
            lon = 77.5946 + random.uniform(-0.0003, 0.0003),
        )
    else:
        try:
            if ser.in_waiting:
                raw = ser.readline().decode("utf-8", errors="ignore")
                d = parse_raw(raw)
        except Exception as e:
            st.error(f"Serial error: {e}")
            st.session_state.ser = None
            break
        if d is None:
            time.sleep(0.05)
            continue

    # ── Process ──
    g    = accel_g(d["ax"], d["ay"], d["az"])
    fall = is_fall_or_shock(g)
    risk = compute_risk(d["temp"], d["hum"], d["gas"], profile)
    s    = status_label(risk)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Trigger detection ──
    trigger_reason = None
    if fall:
        trigger_reason = "shock/fall"
    elif risk >= RISK_ALERT:
        trigger_reason = "risk_alert"
    elif risk >= RISK_CAUTION:
        trigger_reason = "risk_caution"

    if trigger_reason:
        cmd = build_store_cmd(d, g, fall, risk, s)
        if ser:
            try:
                ser.write((cmd + "\n").encode())
            except Exception:
                pass
        log_trigger(log_file, ts, produce, d, g, fall, risk, s, trigger_reason)
        st.session_state.triggers.append({
            "time": ts,
            "produce": produce,
            "temp": round(d["temp"], 1),
            "hum":  round(d["hum"],  1),
            "gas":  round(d["gas"],  0),
            "accel_g": round(g, 2),
            "fall": fall,
            "risk": risk,
            "status": s,
            "reason": trigger_reason,
        })
        st.session_state.triggers = st.session_state.triggers[-200:]

    # ── History ──
    st.session_state.history.append({
        "time":    ts,
        "produce": produce,
        "temp":    round(d["temp"], 1),
        "hum":     round(d["hum"],  1),
        "gas":     round(d["gas"],  0),
        "accel_g": round(g, 2),
        "fall":    fall,
        "risk":    risk,
        "status":  s,
    })
    st.session_state.history = st.session_state.history[-HISTORY_SIZE:]

    # ── Render ──
    with status_ph.container():
        st.markdown(f"**Produce: {produce}**   —   {profile['info']}")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Temperature",  f"{d['temp']:.1f} C",  f"safe <= {profile['temp_safe']} C")
        c2.metric("Humidity",     f"{d['hum']:.1f} %",   f"safe <= {profile['hum_safe']} %")
        c3.metric("Gas",          f"{d['gas']:.0f}",     f"safe <= {profile['gas_safe']}")
        c4.metric("Accel",        f"{g:.2f} g")
        c5.metric("Risk / Status", f"{risk}  {s}")
        if fall:
            st.warning(f"SHOCK / FALL detected  —  {g:.2f} g")
        if d["lat"] != 0.0:
            st.caption(f"GPS: {d['lat']:.5f}, {d['lon']:.5f}")

    with readings_ph.container():
        st.subheader(f"Live readings — {produce} (last {HISTORY_SIZE})")
        if st.session_state.history:
            st.dataframe(
                pd.DataFrame(st.session_state.history[::-1]),
                use_container_width=True, hide_index=True
            )

    with trigger_ph.container():
        if st.session_state.triggers:
            st.subheader(f"Triggered events stored on SD card — {produce} ({len(st.session_state.triggers)})")
            st.dataframe(
                pd.DataFrame(st.session_state.triggers[::-1]),
                use_container_width=True, hide_index=True
            )
