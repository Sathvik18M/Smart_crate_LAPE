/*
  Smart Crate Firmware
  --------------------
  Role: Data extraction only.

  Every second, reads DHT11 (temp/humidity), MQ gas sensor, MPU6050
  (raw accelerometer x/y/z), and NEO-6M GPS, then sends one CSV line:

    RAW,<millis>,<temp>,<hum>,<gas>,<ax>,<ay>,<az>,<lat>,<lon>

  Also listens for a STORE command from Python (sent when a trigger fires):

    STORE,<millis>,<temp>,<hum>,<gas>,<accel_g>,<fall>,<lat>,<lon>,<risk>,<status>

  When a STORE command is received, the firmware writes that line to the SD card.
*/

#include <Arduino.h>
#include <DHT.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <TinyGPS++.h>

// ---------- Pins ----------
#define DHT_PIN    4
#define DHT_TYPE   DHT11
#define GAS_PIN    34
#define SD_CS      5

// GPS on Serial2 (hardware UART)
#define GPS_RX     16
#define GPS_TX     17

DHT dht(DHT_PIN, DHT_TYPE);
Adafruit_MPU6050 mpu;
TinyGPSPlus gps;
HardwareSerial GPSSerial(2);

bool mpuOK = false;
bool sdOK  = false;

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

  pinMode(GAS_PIN, INPUT);

  dht.begin();
  Wire.begin(21, 22);

  // MPU6050
  if (!mpu.begin()) {
    Serial.println("MSG,MPU6050 not found");
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
  } else {
    sdOK = true;
    // Write header if file is new
    if (!SD.exists("/log.csv")) {
      File f = SD.open("/log.csv", FILE_WRITE);
      if (f) {
        f.println("millis,temp_c,humidity_pct,gas_raw,accel_g,fall,lat,lon,risk,status");
        f.close();
      }
    }
    Serial.println("MSG,SD card ready");
  }

  delay(500);
}

// ---------- Loop ----------
void loop() {
  // --- Check for incoming STORE command from Python ---
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.startsWith("STORE,")) {
      // Strip the prefix and log the data portion directly
      String payload = cmd.substring(6); // everything after "STORE,"
      sdLog(payload);
    }
  }

  // --- Read sensors ---
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

  // GPS — drain UART buffer
  while (GPSSerial.available() > 0) {
    gps.encode(GPSSerial.read());
  }
  double lat = 0.0, lon = 0.0;
  if (gps.location.isValid()) {
    lat = gps.location.lat();
    lon = gps.location.lng();
  }

  // --- Emit RAW line ---
  // RAW,millis,temp,hum,gas,ax,ay,az,lat,lon
  Serial.print("RAW,");
  Serial.print(millis());              Serial.print(",");
  Serial.print(isnan(temperature) ? 0.0f : temperature, 1); Serial.print(",");
  Serial.print(isnan(humidity)    ? 0.0f : humidity,    1); Serial.print(",");
  Serial.print(gasValue);             Serial.print(",");
  Serial.print(ax, 3);                Serial.print(",");
  Serial.print(ay, 3);                Serial.print(",");
  Serial.print(az, 3);                Serial.print(",");
  Serial.print(lat, 6);               Serial.print(",");
  Serial.println(lon, 6);

  delay(1000);
}
