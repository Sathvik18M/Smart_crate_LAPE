"""
Smart Crate - Python Processing Engine + Simple UI
---------------------------------------------------
Architecture:
  ESP32  ->  RAW,millis,temp,hum,gas,ax,ay,az,lat,lon  ->  Python (via Serial)
  Python ->  compute risk, detect fall/shock
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

# ─────────────────────── Configuration ───────────────────────
SERIAL_PORT  = "COM5"
BAUD_RATE    = 115200
LOG_FILE     = "triggers.csv"   # local copy of triggered events (mirrors SD card)
HISTORY_SIZE = 100              # how many readings to keep in the live feed table

# ─────────────────────── Risk thresholds ─────────────────────
TEMP_SAFE,  TEMP_DANGER  = 20.0,  30.0
HUM_SAFE,   HUM_DANGER   = 51.0,  75.0
GAS_SAFE,   GAS_DANGER   = 84.0, 300.0
W_TEMP, W_HUM, W_GAS     = 0.40, 0.35, 0.25

RISK_ALERT   = 70
RISK_CAUTION = 40

FREEFALL_G   = 0.35
SHOCK_G      = 2.50

# ─────────────────────── Processing functions ─────────────────

def _risk_component(value, safe_max, danger_max):
    if value <= safe_max:   return 0.0
    if value >= danger_max: return 100.0
    return (value - safe_max) / (danger_max - safe_max) * 100.0

def compute_risk(temp, hum, gas):
    r = (W_TEMP * _risk_component(temp, TEMP_SAFE,  TEMP_DANGER)
       + W_HUM  * _risk_component(hum,  HUM_SAFE,   HUM_DANGER)
       + W_GAS  * _risk_component(gas,  GAS_SAFE,   GAS_DANGER))
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
    """Build the STORE command string to send back to ESP32."""
    return (
        f"STORE,{d['millis']},{d['temp']:.1f},{d['hum']:.1f},"
        f"{d['gas']:.0f},{g:.2f},{int(fall)},"
        f"{d['lat']:.6f},{d['lon']:.6f},{risk},{status_str}"
    )

# ─────────────────────── Local log (mirrors SD) ───────────────

def ensure_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            f.write("timestamp,millis,temp_c,humidity_pct,gas_raw,"
                    "accel_g,fall,lat,lon,risk,status,trigger\n")

def log_trigger(ts, d, g, fall, risk, status_str, trigger_reason):
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts},{d['millis']},{d['temp']:.1f},{d['hum']:.1f},"
                f"{d['gas']:.0f},{g:.2f},{int(fall)},"
                f"{d['lat']:.6f},{d['lon']:.6f},{risk},{status_str},{trigger_reason}\n")

# ─────────────────────── Streamlit UI ─────────────────────────

st.set_page_config(page_title="Smart Crate", layout="wide")
st.title("Smart Crate — Live Monitor")

# Sidebar
with st.sidebar:
    st.header("Connection")
    sim_mode = st.checkbox("Simulation mode", value=True)
    if not sim_mode:
        port = st.text_input("Serial port", SERIAL_PORT)
        baud = st.number_input("Baud rate", value=BAUD_RATE, step=100)
    else:
        st.caption("Using simulated data.")
        sim_temp = st.slider("Temp (C)",  0.0,  50.0, 24.0)
        sim_hum  = st.slider("Humidity (%)", 0.0, 100.0, 55.0)
        sim_gas  = st.slider("Gas ADC",   0.0, 500.0, 100.0)
        sim_shock = st.checkbox("Simulate shock")

    run = st.toggle("Connect and run", value=False)

    st.divider()
    st.caption("Trigger conditions")
    st.caption(f"  Risk >= {RISK_CAUTION} (CAUTION) or >= {RISK_ALERT} (ALERT)")
    st.caption(f"  Shock: g <= {FREEFALL_G} or g >= {SHOCK_G}")
    st.caption("Triggered readings are stored on the ESP32 SD card.")

ensure_log()

# Session state
if "history"  not in st.session_state: st.session_state.history  = []
if "triggers" not in st.session_state: st.session_state.triggers = []
if "ser"      not in st.session_state: st.session_state.ser      = None

# Placeholders
status_ph   = st.empty()
readings_ph = st.empty()
trigger_ph  = st.empty()

if not run:
    # Close serial if open
    if st.session_state.ser:
        try: st.session_state.ser.close()
        except: pass
        st.session_state.ser = None

    st.info("Enable 'Connect and run' in the sidebar to start.")
    if st.session_state.triggers:
        st.subheader("Stored trigger log")
        st.dataframe(pd.DataFrame(st.session_state.triggers), use_container_width=True)
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

import random, math as _math

# ── Main loop ──
last_sim_t = 0.0

while True:
    # ── Acquire one reading ──
    d = None

    if sim_mode:
        now = time.time()
        if now - last_sim_t < 1.0:
            time.sleep(0.05)
            continue
        last_sim_t = now

        if sim_shock:
            ax, ay, az = 25.0, 20.0, 15.0  # ~3.8 g impact
        else:
            ax, ay, az = (random.uniform(-0.1, 0.1),
                          random.uniform(-0.1, 0.1),
                          9.81 + random.uniform(-0.05, 0.05))

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
    risk = compute_risk(d["temp"], d["hum"], d["gas"])
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
        log_trigger(ts, d, g, fall, risk, s, trigger_reason)
        st.session_state.triggers.append({
            "time": ts, "temp": round(d["temp"], 1), "hum": round(d["hum"], 1),
            "gas": round(d["gas"], 0), "accel_g": round(g, 2),
            "fall": fall, "risk": risk, "status": s, "reason": trigger_reason,
        })
        st.session_state.triggers = st.session_state.triggers[-200:]

    # ── History (live feed) ──
    st.session_state.history.append({
        "time": ts,
        "temp": round(d["temp"], 1),
        "hum":  round(d["hum"],  1),
        "gas":  round(d["gas"],  0),
        "accel_g": round(g, 2),
        "fall": fall,
        "risk": risk,
        "status": s,
    })
    st.session_state.history = st.session_state.history[-HISTORY_SIZE:]

    # ── Render ──
    with status_ph.container():
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Temperature",  f"{d['temp']:.1f} C")
        c2.metric("Humidity",     f"{d['hum']:.1f} %")
        c3.metric("Gas",          f"{d['gas']:.0f}")
        c4.metric("Accel",        f"{g:.2f} g")
        c5.metric("Risk / Status", f"{risk}  {s}")
        if fall:
            st.warning(f"SHOCK / FALL detected  —  {g:.2f} g")
        if d["lat"] != 0.0:
            st.caption(f"GPS: {d['lat']:.5f}, {d['lon']:.5f}")

    with readings_ph.container():
        st.subheader("Live readings (last 100)")
        if st.session_state.history:
            st.dataframe(
                pd.DataFrame(st.session_state.history[::-1]),
                use_container_width=True, hide_index=True
            )

    with trigger_ph.container():
        if st.session_state.triggers:
            st.subheader(f"Triggered events stored on SD card ({len(st.session_state.triggers)})")
            st.dataframe(
                pd.DataFrame(st.session_state.triggers[::-1]),
                use_container_width=True, hide_index=True
            )
