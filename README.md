# Smart Crate — LAPE

Smart Crate is an IoT spoilage monitoring system for cargo crates.

## Architecture

```
ESP32 sensors  -->  RAW serial line  -->  Python (processing)
                                              |
                              trigger?  -->  STORE command  -->  ESP32 SD card
                                              |
                                         Streamlit UI
```

### ESP32 (`smart_crate.ino`)
- Reads: DHT11 (temp/humidity), MQ gas sensor, MPU6050 (raw accelerometer), NEO-6M GPS
- Sends every second over Serial (115200 baud):
  ```
  RAW,<millis>,<temp_c>,<hum_pct>,<gas_adc>,<ax>,<ay>,<az>,<lat>,<lon>
  ```
- Listens for `STORE,...` command from Python → writes that record to `/log.csv` on SD card

### Python (`app.py`)
- Parses `RAW` lines from ESP32 over Serial
- Computes:
  - **Composite risk score** (0–100) from temperature, humidity, gas
  - **Shock / freefall detection** from raw accelerometer magnitude
- **Trigger conditions** (stored to SD card):
  - Risk >= 40 (CAUTION)
  - Risk >= 70 (ALERT)
  - Accel <= 0.35 g (freefall) or >= 2.5 g (impact)
- On trigger: sends `STORE,...` back to ESP32 via Serial → ESP32 writes to SD card
- Simple Streamlit dashboard: live readings table + triggered events table

## Setup

### Hardware
| Component | ESP32 Pin |
|-----------|-----------|
| DHT11     | GPIO 4    |
| MQ Gas    | GPIO 34   |
| MPU6050   | SDA 21, SCL 22 |
| NEO-6M RX | GPIO 16   |
| NEO-6M TX | GPIO 17   |
| SD Card CS | GPIO 5  |
| SD Card SCK/MOSI/MISO | GPIO 18/23/19 |

### Software

1. Flash `smart_crate.ino` via Arduino IDE (install libraries: DHT, Adafruit MPU6050, TinyGPSPlus)
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set the correct serial port in `app.py` (`SERIAL_PORT = "COM5"`)
4. Run the dashboard:
   ```bash
   streamlit run app.py
   ```

## Files
| File | Description |
|------|-------------|
| `smart_crate.ino` | ESP32 firmware — sensor reading + SD card logging |
| `app.py` | Python processing engine + Streamlit dashboard |
| `requirements.txt` | Python dependencies |
