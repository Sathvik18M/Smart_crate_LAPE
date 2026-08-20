/*
  Smart Crate Firmware
  --------------------
  Role: Data extraction + local visual feedback.

  Every 20 seconds, reads DHT11 (temp/humidity), MQ gas sensor, MPU6050
  (raw accelerometer x/y/z), and NEO-6M GPS, then sends one CSV line:

    RAW,<millis>,<temp>,<hum>,<gas>,<ax>,<ay>,<az>,<lat>,<lon>

  Also listens for a STORE command from Python (sent when a trigger fires):

    STORE,<millis>,<temp>,<hum>,<gas>,<accel_g>,<fall>,<lat>,<lon>,<risk>,<status>

  On receiving STORE:
    - Writes the record to /log.csv on the SD card
    - Updates the OLED display and LEDs based on the risk status

  OLED (SSD1306 128x64, I2C 0x3C):
    Shared I2C bus with MPU6050 (MPU6050 is at 0x68)
    SDA -> GPIO 21,  SCL -> GPIO 22

  LEDs:
    Green LED -> GPIO 25  (SAFE)
    Red LED   -> GPIO 26  (CAUTION solid / ALERT blink)
*/

#include <Arduino.h>
#include <DHT.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <TinyGPS++.h>

// ---------- Pins ----------
#define DHT_PIN     4
#define DHT_TYPE    DHT11
#define GAS_PIN     34
#define SD_CS       5
#define GREEN_LED   25
#define RED_LED     26

// GPS on Serial2 (hardware UART)
#define GPS_RX      16
#define GPS_TX      17

// OLED (SSD1306 128x64)
#define OLED_WIDTH  128
#define OLED_HEIGHT  64
#define OLED_RESET   -1
#define OLED_ADDR   0x3C

// Timing
#define READ_INTERVAL_MS  20000UL  // 20 seconds

// ---------- Globals ----------
DHT dht(DHT_PIN, DHT_TYPE);
Adafruit_MPU6050 mpu;
Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);
TinyGPSPlus gps;
HardwareSerial GPSSerial(2);

bool mpuOK  = false;
bool sdOK   = false;
bool oledOK = false;

// Last known state (updated when Python sends back STORE with status)
String lastStatus = "WAITING";
int    lastRisk   = -1;
bool   lastFall   = false;

unsigned long prevReadMs = 0;

// ---------- OLED helpers ----------
void oledShow(const String &line1, const String &line2 = "",
              const String &line3 = "", const String &line4 = "") {
  if (!oledOK) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);  oled.println(line1);
  oled.setCursor(0, 16); oled.println(line2);
  oled.setCursor(0, 32); oled.println(line3);
  oled.setCursor(0, 48); oled.println(line4);
  oled.display();
}

// ---------- LED helpers ----------
void setLeds(const String &status) {
  // ALERT  -> Red solid, Green off
  // CAUTION-> Red on, Green off
  // SAFE   -> Green on, Red off
  // Other  -> both off
  if (status == "ALERT") {
    digitalWrite(GREEN_LED, LOW);
    digitalWrite(RED_LED,   HIGH);
  } else if (status == "CAUTION") {
    digitalWrite(GREEN_LED, LOW);
    digitalWrite(RED_LED,   HIGH);
  } else if (status == "SAFE") {
    digitalWrite(GREEN_LED, HIGH);
    digitalWrite(RED_LED,   LOW);
  } else {
    digitalWrite(GREEN_LED, LOW);
    digitalWrite(RED_LED,   LOW);
  }
}

// ---------- SD helper ----------
void sdLog(const String &line) {
  if (!sdOK) return;
  File f = SD.open("/log.csv", FILE_APPEND);
  if (f) {
    f.println(line);
    f.close();
  }
}

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(GAS_PIN,   INPUT);
  pinMode(GREEN_LED, OUTPUT);
  pinMode(RED_LED,   OUTPUT);
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(RED_LED,   LOW);

  dht.begin();
  Wire.begin(21, 22);

  // OLED
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("MSG,OLED not found");
  } else {
    oledOK = true;
    oled.clearDisplay();
    oled.display();
    oledShow("Smart Crate", "Initializing...");
  }

  // MPU6050
  if (!mpu.begin()) {
    Serial.println("MSG,MPU6050 not found");
    if (oledOK) oledShow("Smart Crate", "MPU6050: MISSING");
  } else {
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    mpuOK = true;
    Serial.println("MSG,MPU6050 ready");
  }

  // GPS
  GPSSerial.begin(9600, SERIAL_8N1, GPS_RX, GPS_TX);

  // SD card
  SPI.begin(18, 19, 23, SD_CS);
  if (!SD.begin(SD_CS, SPI, 1000000)) {
    Serial.println("MSG,SD card not found");
    if (oledOK) oledShow("Smart Crate", "SD: MISSING");
  } else {
    sdOK = true;
    if (!SD.exists("/log.csv")) {
      File f = SD.open("/log.csv", FILE_WRITE);
      if (f) {
        f.println("millis,temp_c,humidity_pct,gas_raw,accel_g,fall,lat,lon,risk,status");
        f.close();
      }
    }
    Serial.println("MSG,SD card ready");
  }

  delay(1000);
  oledShow("Smart Crate", "Ready", sdOK ? "SD: OK" : "SD: MISSING",
           mpuOK ? "MPU: OK" : "MPU: MISSING");
  prevReadMs = millis() - READ_INTERVAL_MS; // fire first reading immediately
}

// ---------- Loop ----------
void loop() {
  // ── GPS: keep draining UART regardless of timing ──
  while (GPSSerial.available() > 0) {
    gps.encode(GPSSerial.read());
  }

  // ── Check for STORE command from Python ──
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.startsWith("STORE,")) {
      String payload = cmd.substring(6); // strip "STORE," prefix
      sdLog(payload);

      // Parse status from the last field of the payload
      // payload: millis,temp,hum,gas,accel_g,fall,lat,lon,risk,status
      int lastComma = payload.lastIndexOf(',');
      if (lastComma >= 0) {
        lastStatus = payload.substring(lastComma + 1);
        lastStatus.trim();
      }
      // Parse risk (second-to-last field)
      String trimmed = payload.substring(0, lastComma);
      int prevComma = trimmed.lastIndexOf(',');
      if (prevComma >= 0) {
        lastRisk = trimmed.substring(prevComma + 1).toInt();
      }

      // Update LEDs based on status received from Python
      setLeds(lastStatus);

      // Parse fall flag (field index 5, 0-based from payload start)
      // Fields: millis(0),temp(1),hum(2),gas(3),accel_g(4),fall(5),lat(6),lon(7),risk(8),status(9)
      String tmp = payload;
      int commaCount = 0;
      int startIdx = 0;
      while (commaCount < 5 && startIdx < (int)tmp.length()) {
        int c = tmp.indexOf(',', startIdx);
        if (c < 0) break;
        startIdx = c + 1;
        commaCount++;
      }
      if (commaCount == 5) {
        int endIdx = tmp.indexOf(',', startIdx);
        String fallStr = (endIdx >= 0) ? tmp.substring(startIdx, endIdx) : tmp.substring(startIdx);
        lastFall = (fallStr.toInt() == 1);
      }

      // Update OLED with trigger info
      String riskStr = "Risk: " + String(lastRisk) + " " + lastStatus;
      String fallStr = lastFall ? "SHOCK DETECTED" : "";
      oledShow("** TRIGGER **", riskStr, fallStr, "Logged to SD");
    }
  }

  // ── Timed sensor read (every 20 seconds) ──
  unsigned long now = millis();
  if (now - prevReadMs < READ_INTERVAL_MS) return;
  prevReadMs = now;

  // Read sensors
  float temperature = dht.readTemperature();
  float humidity    = dht.readHumidity();
  int   gasValue    = analogRead(GAS_PIN);

  float ax = 0.0, ay = 0.0, az = 0.0;
  if (mpuOK) {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    ax = a.acceleration.x;
    ay = a.acceleration.y;
    az = a.acceleration.z;
  }

  double lat = 0.0, lon = 0.0;
  if (gps.location.isValid()) {
    lat = gps.location.lat();
    lon = gps.location.lng();
  }

  // Update OLED with current raw readings
  char line1[21], line2[21], line3[21], line4[21];
  snprintf(line1, sizeof(line1), "T:%.1fC  H:%.1f%%",
           isnan(temperature) ? 0.0f : temperature,
           isnan(humidity)    ? 0.0f : humidity);
  snprintf(line2, sizeof(line2), "Gas: %d", gasValue);
  snprintf(line3, sizeof(line3), "Ax%.2f Ay%.2f Az%.2f", ax, ay, az);
  snprintf(line4, sizeof(line4), lastStatus == "WAITING" ? "Waiting..." :
           ("Sts: " + lastStatus).c_str());
  oledShow(String(line1), String(line2), String(line3), String(line4));

  // Emit RAW line to Python
  Serial.print("RAW,");
  Serial.print(now);                                             Serial.print(",");
  Serial.print(isnan(temperature) ? 0.0f : temperature, 1);     Serial.print(",");
  Serial.print(isnan(humidity)    ? 0.0f : humidity,    1);     Serial.print(",");
  Serial.print(gasValue);                                        Serial.print(",");
  Serial.print(ax, 3);                                           Serial.print(",");
  Serial.print(ay, 3);                                           Serial.print(",");
  Serial.print(az, 3);                                           Serial.print(",");
  Serial.print(lat, 6);                                          Serial.print(",");
  Serial.println(lon, 6);
}
