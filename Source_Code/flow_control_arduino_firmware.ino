/*
  Flow Control Arduino Firmware
  Board: Arduino Mega 2560

  Pin map:
    D4 = Relay 1 = Pump 1 start/stop
    D5 = Relay 2 = Pump 2 / backwash pump start/stop
    D6 = Relay 3 = Valve 1
    D7 = Relay 4 = Valve 2
    D9 = PWM output to 0-10V module for Pump 1 speed
    A0 = 4-20mA converter VOUT from SONOTEC current output

  Pressure is read by a separate USB serial PSI reader, not by Arduino.
*/

const int PUMP1_RELAY = 4;
const int BACKWASH_RELAY = 5;
const int VALVE1_RELAY = 6;
const int VALVE2_RELAY = 7;
const int PUMP1_PWM = 9;
const int FLOW_ANALOG_PIN = A0;

const int RELAY_ON = HIGH;
const int RELAY_OFF = LOW;

const float VREF = 5.0;
const float CONVERTER_V_AT_4MA = 0.0;   // Change here if converter outputs 1 V at 4 mA.
const float CONVERTER_V_AT_20MA = 5.0;  // Change here if converter full-scale voltage is different.
const float FLOW_MIN_ML = 0.0;    // Flow at 4 mA.
const float FLOW_MAX_ML = 3000.0; // Flow at 20 mA, sensor full scale.
const unsigned long FLOW_SAMPLE_MS = 1000;

unsigned long lastFlowSampleMs = 0;
unsigned long lastStatusMs = 0;
int latestFlowRaw = 0;
float latestFlowVoltage = 0.0;
float latestFlowMa = 0.0;
float latestFlowMlMin = 0.0;

bool pump1On = false;
bool backwashOn = false;
bool valve1Open = false;
bool valve2Open = false;
int speedPercent = 0;

String inputLine = "";

void setup() {
  pinMode(PUMP1_RELAY, OUTPUT);
  pinMode(BACKWASH_RELAY, OUTPUT);
  pinMode(VALVE1_RELAY, OUTPUT);
  pinMode(VALVE2_RELAY, OUTPUT);
  pinMode(PUMP1_PWM, OUTPUT);

  Serial.begin(9600);
  allOff();
  lastFlowSampleMs = millis() - FLOW_SAMPLE_MS;
  updateFlowMeasurement();
  sendStatus();
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      inputLine.trim();
      if (inputLine.length() > 0) {
        handleCommand(inputLine);
      }
      inputLine = "";
    } else {
      inputLine += c;
    }
  }

  updateFlowMeasurement();

  if (millis() - lastStatusMs >= 1000) {
    sendStatus();
  }
}

void updateFlowMeasurement() {
  unsigned long now = millis();
  if (now - lastFlowSampleMs < FLOW_SAMPLE_MS) {
    return;
  }

  unsigned long totalAdc = 0;
  for (int i = 0; i < 64; i++) {
    totalAdc += analogRead(FLOW_ANALOG_PIN);
  }

  float adc = totalAdc / 64.0;
  float voltage = adc * (VREF / 1023.0);
  float converterSpan = CONVERTER_V_AT_20MA - CONVERTER_V_AT_4MA;
  float mA = 4.0;
  if (converterSpan > 0.0001 || converterSpan < -0.0001) {
    mA = 4.0 + (voltage - CONVERTER_V_AT_4MA) * 16.0 / converterSpan;
  }
  float flow = (mA - 4.0) / 16.0 * (FLOW_MAX_ML - FLOW_MIN_ML) + FLOW_MIN_ML;

  latestFlowRaw = (int)(adc + 0.5);
  latestFlowVoltage = voltage;
  latestFlowMa = mA;
  latestFlowMlMin = flow < 0.0 ? 0.0 : flow;
  lastFlowSampleMs = now;
}

void setPump1(bool on) {
  pump1On = on;
  digitalWrite(PUMP1_RELAY, on ? RELAY_ON : RELAY_OFF);
}

void setBackwash(bool on) {
  backwashOn = on;
  digitalWrite(BACKWASH_RELAY, on ? RELAY_ON : RELAY_OFF);
}

void setValve1(bool open) {
  valve1Open = open;
  digitalWrite(VALVE1_RELAY, open ? RELAY_ON : RELAY_OFF);
}

void setValve2(bool open) {
  valve2Open = open;
  digitalWrite(VALVE2_RELAY, open ? RELAY_ON : RELAY_OFF);
}

void setSpeed(int percent) {
  speedPercent = constrain(percent, 0, 100);
  int pwm = map(speedPercent, 0, 100, 0, 255);
  analogWrite(PUMP1_PWM, pwm);
}

void allOff() {
  setPump1(false);
  setBackwash(false);
  setValve1(false);
  setValve2(false);
  setSpeed(0);
}

void handleCommand(String cmd) {
  cmd.toUpperCase();

  if (cmd == "PUMP1 ON") setPump1(true);
  else if (cmd == "PUMP1 OFF") setPump1(false);
  else if (cmd.startsWith("SPEED ")) setSpeed(cmd.substring(6).toInt());
  else if (cmd == "BACKWASH ON") setBackwash(true);
  else if (cmd == "BACKWASH OFF") setBackwash(false);
  else if (cmd == "VALVE1 ON") setValve1(true);
  else if (cmd == "VALVE1 OFF") setValve1(false);
  else if (cmd == "VALVE2 ON") setValve2(true);
  else if (cmd == "VALVE2 OFF") setValve2(false);
  else if (cmd == "ALL OFF") allOff();
  else if (cmd == "STATUS") sendStatus();

  sendStatus();
}

void sendStatus() {
  updateFlowMeasurement();

  Serial.print("STATUS,");
  Serial.print("PUMP1="); Serial.print(pump1On ? "ON" : "OFF");
  Serial.print(",SPEED="); Serial.print(speedPercent);
  Serial.print(",BACKWASH="); Serial.print(backwashOn ? "ON" : "OFF");
  Serial.print(",VALVE1="); Serial.print(valve1Open ? "ON" : "OFF");
  Serial.print(",VALVE2="); Serial.print(valve2Open ? "ON" : "OFF");
  Serial.print(",FLOW_RAW="); Serial.print(latestFlowRaw);
  Serial.print(",FLOW_V="); Serial.print(latestFlowVoltage, 3);
  Serial.print(",FLOW_MA="); Serial.print(latestFlowMa, 3);
  Serial.print(",FLOW_ML_MIN="); Serial.print(latestFlowMlMin, 3);
  Serial.println();

  lastStatusMs = millis();
}
