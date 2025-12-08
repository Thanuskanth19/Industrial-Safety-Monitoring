// ===================== Industrial Safety Node (Arduino) =====================
// DHT22 VERSION (temperature from DHT22, gas & vibration as raw ADC)
// Sends: tempC,gasLevel,vibLevel  (comma-separated)
// ============================================================================

#include <DHT.h>

// ---------- DHT22 CONFIG ----------
#define DHTPIN 2        // DHT22 data pin
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

// ---------- PINS ----------
const int GAS_PIN  = A1;   // Gas sensor raw
const int VIB_PIN  = A0;   // Vibration sensor raw

const int TEMP_LED_G = 4;
const int TEMP_LED_O = 5;
const int TEMP_LED_R = 6;

const int GAS_LED_G  = 7;
const int GAS_LED_O  = 8;
const int GAS_LED_R  = 9;

const int BUZZER_PIN = 10;
const int RELAY_FAN  = 11;   // Relay input pin (ACTIVE-LOW module)

// ---- thresholds (in °C) ----
const float TEMP_WARN      = 32.0;
const float TEMP_CRITICAL  = 38.0;
const float TEMP_RELAY_ON  = 35.0;
const float TEMP_RELAY_OFF = 33.0;

// Gas thresholds (RAW ADC)
const int GAS_SAFE_MAX     = 300;
const int GAS_MODERATE_MAX = 600;   // moderate up to 600, above = dangerous (alarm)

// Vibration threshold (RAW ADC)
const int VIB_THRESHOLD    = 900;

// ---------- integer averaging ----------
int readAnalogAvgInt(int pin, int samples = 10) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delayMicroseconds(800);
  }
  return (int)(sum / samples);
}

// ---------- LED control ----------
void set3Leds(int pinG, int pinO, int pinR, int s) {
  // s = 0 -> GREEN, 1 -> ORANGE, 2 -> RED
  digitalWrite(pinG, (s == 0) ? HIGH : LOW);
  digitalWrite(pinO, (s == 1) ? HIGH : LOW);
  digitalWrite(pinR, (s == 2) ? HIGH : LOW);
}

void setup() {
  Serial.begin(9600);

  dht.begin();   // Start DHT22

  pinMode(TEMP_LED_G, OUTPUT);
  pinMode(TEMP_LED_O, OUTPUT);
  pinMode(TEMP_LED_R, OUTPUT);

  pinMode(GAS_LED_G, OUTPUT);
  pinMode(GAS_LED_O, OUTPUT);
  pinMode(GAS_LED_R, OUTPUT);

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(RELAY_FAN,  OUTPUT);

  digitalWrite(BUZZER_PIN, LOW);

  // For ACTIVE-LOW relay module:
  // HIGH = OFF, LOW = ON
  digitalWrite(RELAY_FAN, HIGH);   // Start with fan OFF
}

void loop() {
  // ---------------- TEMP from DHT22 ----------------
  static float lastTempC = 25.0;   // fallback value
  float tempC = dht.readTemperature();  // °C

  if (isnan(tempC)) {
    // If reading fails, keep last valid temperature
    tempC = lastTempC;
  } else {
    lastTempC = tempC;
  }

  // ---------------- RAW SENSOR READINGS (Gas, Vibration) ----------------
  int gasRaw = readAnalogAvgInt(GAS_PIN, 10);
  int vibRaw = readAnalogAvgInt(VIB_PIN, 10);
  int vibLevel = vibRaw;   // 0–1023

  // ---------------- DETERMINE TEMP & GAS STATE ----------------
  // 0 = green (safe), 1 = orange (warning), 2 = red (high)
  int tempState;
  if (tempC < TEMP_WARN) {
    tempState = 0;
  } else if (tempC < TEMP_CRITICAL) {
    tempState = 1;
  } else {
    tempState = 2;
  }

  int gasState;
  if (gasRaw <= GAS_SAFE_MAX) {
    gasState = 0;
  } else if (gasRaw <= GAS_MODERATE_MAX) {
    gasState = 1;
  } else {
    gasState = 2;
  }

  // First set steady LEDs for both groups according to their state
  set3Leds(TEMP_LED_G, TEMP_LED_O, TEMP_LED_R, tempState);
  set3Leds(GAS_LED_G,  GAS_LED_O,  GAS_LED_R,  gasState);

  // ---------------- Fan relay (TEMP + GAS control) ----------------
  // 1) Temperature-based hysteresis
  static bool fanOnTemp = false;

  if (!fanOnTemp && tempC >= TEMP_RELAY_ON) {
    fanOnTemp = true;    // Turn fan ON at or above 35 °C
  }
  if (fanOnTemp && tempC <= TEMP_RELAY_OFF) {
    fanOnTemp = false;   // Turn fan OFF at or below 33 °C
  }

  // 2) Gas-based control: fan ON if gas is dangerous (> 600)
  bool fanOnGas = (gasRaw > GAS_MODERATE_MAX);   // dangerous zone

  // Final fan command: ON if either temperature OR gas requires it
  bool fanOn = fanOnTemp || fanOnGas;

  // ACTIVE-LOW relay: LOW = ON, HIGH = OFF
  digitalWrite(RELAY_FAN, fanOn ? LOW : HIGH);

  // ---------------- BUZZER / ALARM LOGIC ----------------
  // Alarm when:
  //  - Temperature critical (red), OR
  //  - Gas dangerous (red), OR
  //  - Vibration level below threshold
  bool alarm =
      (tempState == 2) ||
      (gasState  == 2) ||
      (vibLevel < VIB_THRESHOLD);

  static bool buzzerState = false;
  static unsigned long lastToggleTime = 0;
  const unsigned long BEEP_PERIOD_MS = 300;  // beep speed

  if (alarm) {
    // Beep pattern: toggle buzzer every 300 ms
    unsigned long now = millis();
    if (now - lastToggleTime >= BEEP_PERIOD_MS) {
      buzzerState = !buzzerState;
      digitalWrite(BUZZER_PIN, buzzerState ? HIGH : LOW);
      lastToggleTime = now;
    }
  } else {
    // No alarm: buzzer fully OFF
    buzzerState = false;
    digitalWrite(BUZZER_PIN, LOW);
  }

  // ---------------- LED BLINK INDICATION (ONLY FOR THAT SENSOR) ----------------
  // If temperature is in RED -> blink only TEMP red LED
  // If gas is in RED         -> blink only GAS red LED
  // If both are RED          -> both red LEDs blink
  static bool blinkState = false;
  static unsigned long lastBlinkTime = 0;
  const unsigned long BLINK_MS = 100;  // 100 ms per toggle

  bool anyCritical = (tempState == 2) || (gasState == 2);

  if (anyCritical) {
    unsigned long now2 = millis();
    if (now2 - lastBlinkTime >= BLINK_MS) {
      blinkState = !blinkState;
      lastBlinkTime = now2;
    }

    // ----- Temperature LEDs -----
    if (tempState == 2) {
      // blink RED for temp
      if (blinkState) {
        digitalWrite(TEMP_LED_G, LOW);
        digitalWrite(TEMP_LED_O, LOW);
        digitalWrite(TEMP_LED_R, HIGH);
      } else {
        digitalWrite(TEMP_LED_G, LOW);
        digitalWrite(TEMP_LED_O, LOW);
        digitalWrite(TEMP_LED_R, LOW);
      }
    } // if tempState is not 2, we keep steady from earlier set3Leds()

    // ----- Gas LEDs -----
    if (gasState == 2) {
      // blink RED for gas
      if (blinkState) {
        digitalWrite(GAS_LED_G, LOW);
        digitalWrite(GAS_LED_O, LOW);
        digitalWrite(GAS_LED_R, HIGH);
      } else {
        digitalWrite(GAS_LED_G, LOW);
        digitalWrite(GAS_LED_O, LOW);
        digitalWrite(GAS_LED_R, LOW);
      }
    } // if gasState is not 2, we keep steady from earlier set3Leds()
  } else {
    // no critical temp/gas -> no blinking; steady LEDs from earlier
    blinkState = false;
  }

  // ---------------- SEND DATA (for Python GUI / Serial Plotter) ----------------
  // Format: tempC,gasRaw,vibLevel
  Serial.print(tempC, 1);  // temperature in °C (1 decimal)
  Serial.print(',');
  Serial.print(gasRaw);
  Serial.print(',');
  Serial.println(vibLevel);  // send vibration raw value

  delay(200);
}
