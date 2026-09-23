"""
SLF3S-1300F live flow reader  (self-contained, no numpy needed)
================================================================
Correct SHDLC commands baked in (verified from the sensirion_slf3s library
source). Needs ONLY the sensirion-shdlc-driver you already installed.

For the SCC1-USB (RS485-USB) cable. Your cable is on COM10.

Run:
    python read_flow_final.py            (uses COM10)
    python read_flow_final.py COM7       (other port)

Prints flow in ml/min. Ctrl+C to stop.
"""

import sys
import time
from struct import unpack

from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection, ShdlcDeviceBase
from sensirion_shdlc_driver.command import ShdlcCommand

PORT = "COM10"
if len(sys.argv) > 1:
    PORT = sys.argv[1]


class GetScaleFactor(ShdlcCommand):
    def __init__(self):
        super().__init__(id=0x53, data=b"\x36\x08", max_response_time=0.2)

    def interpret_response(self, data):
        scale_factor = unpack('>h', data[0:2])[0]
        if data[2:5] == b'\x08E\x00':      # units (ml/min)^-1 -> convert to (ul/min)^-1
            scale_factor = scale_factor * 1e-3
        elif data[2:5] == b'\x08D\x00':    # already (ul/min)^-1
            pass
        return scale_factor


class StartContinuous(ShdlcCommand):
    def __init__(self, water=True, sampling_ms=20):
        sbytes = int.to_bytes(sampling_ms, length=2, byteorder='big')
        data = sbytes + (b"\x36\x08" if water else b"\x36\x15")
        super().__init__(id=0x33, data=data, max_response_time=0.05)

    def interpret_response(self, data):
        return 0


class StopContinuous(ShdlcCommand):
    def __init__(self):
        super().__init__(id=0x34, data=b'', max_response_time=0.05)

    def interpret_response(self, data):
        return 0


class GetLast(ShdlcCommand):
    def __init__(self, flow_scale_factor):
        super().__init__(id=0x35, data=b"\x22", max_response_time=0.2)
        self.flow_scale_factor = flow_scale_factor

    def interpret_response(self, data):
        flow = unpack('>h', data[0:2])[0] / self.flow_scale_factor   # ul/min
        temp = unpack('>h', data[2:4])[0] / 200.0                    # deg C
        flag = unpack('>h', data[4:6])[0]                            # air-in-line
        return flow, temp, flag


class FlowSensor(ShdlcDeviceBase):
    def read_scale_factor(self):
        return float(self.execute(GetScaleFactor()))

    def start(self, water=True, sampling_ms=20):
        self.execute(StartContinuous(water=water, sampling_ms=sampling_ms))

    def stop(self):
        self.execute(StopContinuous())

    def get_last(self, sf):
        return self.execute(GetLast(sf))


def main():
    print(f"Opening {PORT} ...")
    with ShdlcSerialPort(port=PORT, baudrate=115200) as port:
        sensor = FlowSensor(ShdlcConnection(port), slave_address=0)

        sf = sensor.read_scale_factor()
        print(f"Connected. Scale factor = {sf} (per ul/min)")

        print("Starting measurement (water)...")
        sensor.start(water=True, sampling_ms=20)
        time.sleep(0.2)

        print("Reading flow. Run the pump and watch the number. Ctrl+C to stop.\n")
        try:
            while True:
                try:
                    flow_ul_min, temp_c, bubble = sensor.get_last(sf)
                    flow_ml_min = flow_ul_min / 1000.0
                    bub = " [AIR/BUBBLE]" if bubble else ""
                    print(f"  Flow = {flow_ml_min:8.3f} ml/min   ({temp_c:.1f} C){bub}")
                except Exception as e:
                    print(f"  read error: {e}")
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            try:
                sensor.stop()
            except Exception:
                pass
    print("Closed.")


if __name__ == "__main__":
    main()
