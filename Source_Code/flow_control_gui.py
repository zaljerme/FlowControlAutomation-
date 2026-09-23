import csv
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
import serial.tools.list_ports
from read_flow_final import FlowSensor, ShdlcConnection, ShdlcSerialPort

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


BAUD = 9600
BG = "#f4f6f8"
CARD = "#ffffff"
DARK = "#263238"
BLUE = "#1f77b4"
GREEN = "#168a45"
RED = "#c62828"
ORANGE = "#df7d16"
GRAY = "#667085"

F = ("Segoe UI", 10)
FB = ("Segoe UI", 10, "bold")
FT = ("Segoe UI", 12, "bold")
BIG = ("Segoe UI", 18, "bold")
MONO = ("Consolas", 9)

DEFAULT_CONFIG = {
    "hz_per_ml_min": 6.6667,
    "zero_flow_voltage": 2.874,
    "calibration_flow_voltage": 2.805,
    "calibration_flow_ml_min": 5.0,
    "invert_signal": True,
    "flow_tolerance": 0.2,
    "safe_start_command": 5,
    "kp": 1.0,
    "min_command": 0,
    "max_command": 100,
    "correction_step": 2,
    "correction_interval": 5,
    "filtration_minutes": 60,
    "break_before_backwash_minutes": 0,
    "backwash_minutes": 2,
    "backwash_flowrate_ml_min": 20.0,
    "log_interval": 60,
}


def fnum(var, default):
    try:
        return float(var.get())
    except Exception:
        return default


def inum(var, default):
    try:
        return int(float(var.get()))
    except Exception:
        return default


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def fmt_elapsed(seconds):
    seconds = int(max(0, seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class FlowControlApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Flow Control App")
        self.geometry("1320x850")
        self.minsize(1120, 720)
        self.configure(bg=BG)

        self.arduino = None
        self.flow_sensor_port_handle = None
        self.flow_sensor = None
        self.flow_sensor_scale = None
        self.arduino_reader_alive = threading.Event()
        self.flow_sensor_alive = threading.Event()
        self.arduino_q = queue.Queue()
        self.flow_sensor_q = queue.Queue()

        self.mode = "Idle"
        self.filtration_running = False
        self.trial_running = False
        self.backwash_running = False
        self.trial_started_at = None
        self.phase_started_at = None
        self.next_control_at = 0
        self.next_log_at = 0
        self.pending_after_ids = []

        self.pump_cmd = 0
        self.starting_pump_cmd = 30
        self.flow_raw = 0
        self.flow_voltage = 0.0
        self.flow_ma = 0.0
        self.flow_ml = 0.0
        self.flow_source = "Arduino"
        self.sensor_last_rx = 0
        self.sensor_read_ok = False
        self.sensor_temp_c = 0.0
        self.sensor_bubble = 0
        self.last_speed_command = "none"
        self.pump1_status = "OFF"
        self.backwash_status = "OFF"
        self.valve1_status = "OFF"
        self.valve2_status = "OFF"

        self.csv_path = ""
        self.csv_file = None
        self.csv_writer = None

        self.t_data = deque(maxlen=900)
        self.flow_data = deque(maxlen=900)

        self._build_vars()
        self.load_config()
        self._build_ui()
        self.update_calibration_status()
        self.refresh_ports()
        self.after(100, self.poll_queues)
        self.after(1000, self.periodic_tasks)
        self.after(1500, self.refresh_graphs)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_vars(self):
        self.arduino_port = tk.StringVar()
        self.flow_sensor_port = tk.StringVar(value="COM10")
        self.arduino_status = tk.StringVar(value="Arduino: disconnected")
        self.flow_sensor_status = tk.StringVar(value="Flow sensor: disconnected")

        self.target_flow = tk.StringVar(value="5.0")
        self.tolerance = tk.StringVar(value=str(DEFAULT_CONFIG["flow_tolerance"]))
        self.safe_start_cmd = tk.StringVar(value=str(DEFAULT_CONFIG["safe_start_command"]))
        self.kp = tk.StringVar(value=str(DEFAULT_CONFIG["kp"]))
        self.min_cmd = tk.StringVar(value=str(DEFAULT_CONFIG["min_command"]))
        self.max_cmd = tk.StringVar(value=str(DEFAULT_CONFIG["max_command"]))
        self.step_pct = tk.StringVar(value=str(DEFAULT_CONFIG["correction_step"]))
        self.correction_interval = tk.StringVar(value=str(DEFAULT_CONFIG["correction_interval"]))
        self.flow_control_enabled = tk.BooleanVar(value=True)

        self.zero_flow_voltage = tk.StringVar(value=str(DEFAULT_CONFIG["zero_flow_voltage"]))
        self.calibration_flow_voltage = tk.StringVar(value=str(DEFAULT_CONFIG["calibration_flow_voltage"]))
        self.calibration_flow_ml_min = tk.StringVar(value=str(DEFAULT_CONFIG["calibration_flow_ml_min"]))
        self.invert_signal = tk.BooleanVar(value=DEFAULT_CONFIG["invert_signal"])
        self.calibration_status = tk.StringVar(
            value="A0 flow calibration loaded"
        )

        self.filtration_minutes = tk.StringVar(value="60")
        self.break_before_backwash_minutes = tk.StringVar(value=str(DEFAULT_CONFIG["break_before_backwash_minutes"]))
        self.backwash_minutes = tk.StringVar(value="2")
        self.backwash_flowrate = tk.StringVar(value=str(DEFAULT_CONFIG["backwash_flowrate_ml_min"]))
        self.log_interval = tk.StringVar(value=str(DEFAULT_CONFIG["log_interval"]))
        self.csv_enabled = tk.BooleanVar(value=False)

    def config_path(self):
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "config.json")

    def load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        path = self.config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
            except Exception:
                pass

        self.zero_flow_voltage.set(str(cfg.get("zero_flow_voltage", DEFAULT_CONFIG["zero_flow_voltage"])))
        self.calibration_flow_voltage.set(str(cfg.get("calibration_flow_voltage", DEFAULT_CONFIG["calibration_flow_voltage"])))
        self.calibration_flow_ml_min.set(str(cfg.get("calibration_flow_ml_min", DEFAULT_CONFIG["calibration_flow_ml_min"])))
        self.invert_signal.set(bool(cfg.get("invert_signal", DEFAULT_CONFIG["invert_signal"])))
        self.tolerance.set(str(cfg["flow_tolerance"]))
        self.safe_start_cmd.set(str(cfg["safe_start_command"]))
        self.kp.set(str(cfg.get("kp", DEFAULT_CONFIG["kp"])))
        self.min_cmd.set(str(cfg["min_command"]))
        self.max_cmd.set(str(cfg["max_command"]))
        self.step_pct.set(str(cfg["correction_step"]))
        self.correction_interval.set(str(cfg["correction_interval"]))
        self.filtration_minutes.set(str(cfg["filtration_minutes"]))
        self.break_before_backwash_minutes.set(str(cfg.get("break_before_backwash_minutes", DEFAULT_CONFIG["break_before_backwash_minutes"])))
        self.backwash_minutes.set(str(cfg["backwash_minutes"]))
        self.backwash_flowrate.set(str(cfg.get("backwash_flowrate_ml_min", DEFAULT_CONFIG["backwash_flowrate_ml_min"])))
        self.log_interval.set(str(cfg["log_interval"]))

    def save_config(self, show_message=False):
        cfg = {
            "zero_flow_voltage": fnum(self.zero_flow_voltage, DEFAULT_CONFIG["zero_flow_voltage"]),
            "hz_per_ml_min": DEFAULT_CONFIG["hz_per_ml_min"],
            "calibration_flow_voltage": fnum(self.calibration_flow_voltage, DEFAULT_CONFIG["calibration_flow_voltage"]),
            "calibration_flow_ml_min": fnum(self.calibration_flow_ml_min, DEFAULT_CONFIG["calibration_flow_ml_min"]),
            "invert_signal": bool(self.invert_signal.get()),
            "flow_tolerance": fnum(self.tolerance, DEFAULT_CONFIG["flow_tolerance"]),
            "safe_start_command": inum(self.safe_start_cmd, DEFAULT_CONFIG["safe_start_command"]),
            "kp": fnum(self.kp, DEFAULT_CONFIG["kp"]),
            "min_command": inum(self.min_cmd, DEFAULT_CONFIG["min_command"]),
            "max_command": inum(self.max_cmd, DEFAULT_CONFIG["max_command"]),
            "correction_step": fnum(self.step_pct, DEFAULT_CONFIG["correction_step"]),
            "correction_interval": fnum(self.correction_interval, DEFAULT_CONFIG["correction_interval"]),
            "filtration_minutes": fnum(self.filtration_minutes, DEFAULT_CONFIG["filtration_minutes"]),
            "break_before_backwash_minutes": clamp(fnum(self.break_before_backwash_minutes, DEFAULT_CONFIG["break_before_backwash_minutes"]), 0, 30),
            "backwash_minutes": fnum(self.backwash_minutes, DEFAULT_CONFIG["backwash_minutes"]),
            "backwash_flowrate_ml_min": fnum(self.backwash_flowrate, DEFAULT_CONFIG["backwash_flowrate_ml_min"]),
            "log_interval": fnum(self.log_interval, DEFAULT_CONFIG["log_interval"]),
        }
        try:
            with open(self.config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            self.update_calibration_status()
            if show_message:
                messagebox.showinfo("Setup Saved", f"Saved setup to:\n{self.config_path()}")
        except Exception as e:
            if show_message:
                messagebox.showerror("Save Failed", f"Could not save config.json:\n{e}")

    def _build_ui(self):
        top = tk.Frame(self, bg=DARK, padx=10, pady=8)
        top.pack(fill="x")
        self._build_connection_bar(top)

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        left_wrap = tk.Frame(main, bg=BG, width=430)
        left_wrap.pack(side="left", fill="y", padx=(0, 10))
        left_wrap.pack_propagate(False)

        left_canvas = tk.Canvas(left_wrap, bg=BG, highlightthickness=0, width=410)
        left_scroll = ttk.Scrollbar(left_wrap, orient="vertical", command=left_canvas.yview)
        left = tk.Frame(left_canvas, bg=BG)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")

        left_canvas.configure(yscrollcommand=left_scroll.set)
        left_canvas.pack(side="left", fill="both", expand=True)
        left_scroll.pack(side="right", fill="y")

        def update_scroll_region(_event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def fit_left_width(event):
            left_canvas.itemconfigure(left_window, width=event.width)

        def on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        left.bind("<Configure>", update_scroll_region)
        left_canvas.bind("<Configure>", fit_left_width)
        left_canvas.bind("<Enter>", lambda _e: left_canvas.bind_all("<MouseWheel>", on_mousewheel))
        left_canvas.bind("<Leave>", lambda _e: left_canvas.unbind_all("<MouseWheel>"))

        right = tk.Frame(main, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_controls(left)
        self._build_live_area(right)

    def _card(self, parent, title):
        frame = tk.LabelFrame(parent, text=title, font=FT, bg=CARD, fg=DARK, padx=10, pady=8)
        frame.pack(fill="x", pady=(0, 10))
        return frame

    def _collapsible_card(self, parent, title):
        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="x", pady=(0, 10))
        body = tk.LabelFrame(outer, text=title, font=FT, bg=CARD, fg=DARK, padx=10, pady=8)
        open_state = tk.BooleanVar(value=False)

        def toggle():
            if open_state.get():
                body.pack_forget()
                btn.config(text=f"Show {title}")
                open_state.set(False)
            else:
                body.pack(fill="x")
                btn.config(text=f"Hide {title}")
                open_state.set(True)

        btn = tk.Button(outer, text=f"Show {title}", command=toggle, font=FB)
        btn.pack(fill="x")
        return body

    def _row_entry(self, parent, label, var, unit="", width=8):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=F, bg=CARD, anchor="w").pack(side="left", fill="x", expand=True)
        tk.Entry(row, textvariable=var, font=F, width=width, justify="right").pack(side="left")
        if unit:
            tk.Label(row, text=unit, font=F, bg=CARD, fg=GRAY, width=10, anchor="w").pack(side="left", padx=(5, 0))
        return row

    def _build_connection_bar(self, parent):
        tk.Label(parent, text="Arduino COM Port", fg="white", bg=DARK, font=FB).pack(side="left")
        self.arduino_combo = ttk.Combobox(parent, textvariable=self.arduino_port, width=12, state="readonly")
        self.arduino_combo.pack(side="left", padx=(6, 10))
        tk.Button(parent, text="Connect Arduino", command=self.connect_arduino, font=FB).pack(side="left", padx=3)
        tk.Button(parent, text="Disconnect", command=self.disconnect_arduino, font=F).pack(side="left", padx=3)

        tk.Label(parent, text="   Flow Sensor", fg="white", bg=DARK, font=FB).pack(side="left")
        self.flow_sensor_combo = ttk.Combobox(parent, textvariable=self.flow_sensor_port, width=10)
        self.flow_sensor_combo.pack(side="left", padx=(6, 6))
        tk.Button(parent, text="Connect Flow", command=self.connect_flow_sensor, font=FB).pack(side="left", padx=3)
        tk.Button(parent, text="Disconnect", command=self.disconnect_flow_sensor, font=F).pack(side="left", padx=3)

        tk.Button(parent, text="Refresh Ports", command=self.refresh_ports, font=F).pack(side="left", padx=10)
        tk.Button(parent, text="ALL OFF", command=self.all_off, bg=RED, fg="white",
                  activebackground="#a61f1f", font=("Segoe UI", 15, "bold"), padx=22).pack(side="right")

    def _build_controls(self, parent):
        c = self._card(parent, "Target Flow Control")
        self._row_entry(c, "Target filtration flowrate", self.target_flow, "mL/min")
        tk.Label(c, text="Pump 1 starts at a safe low command, then automatically ramps to target flow.",
                 bg=CARD, fg=GRAY, font=F, justify="left").pack(anchor="w", pady=(3, 0))
        btns = tk.Frame(c, bg=CARD)
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(btns, text="Start Filtration", command=self.start_filtration, bg=GREEN, fg="white",
                  font=FB, padx=8, pady=5).pack(side="left", fill="x", expand=True, padx=(0, 5))
        tk.Button(btns, text="Stop Filtration", command=self.stop_filtration, bg=ORANGE, fg="white",
                  font=FB, padx=8, pady=5).pack(side="left", fill="x", expand=True)

        bw_main = self._card(parent, "Backwash Settings")
        self._row_entry(bw_main, "Filtration time", self.filtration_minutes, "minutes")
        self._row_entry(bw_main, "Break before backwash", self.break_before_backwash_minutes, "minutes")
        self._row_entry(bw_main, "Backwash time", self.backwash_minutes, "minutes")
        self._row_entry(bw_main, "Backwash pump flowrate", self.backwash_flowrate, "mL/min")
        tk.Label(bw_main, text="Pump 2 is currently relay ON/OFF; this flowrate is saved and logged.",
                 bg=CARD, fg=GRAY, font=F, justify="left", wraplength=360).pack(anchor="w", pady=(4, 0))
        tk.Button(bw_main, text="Run Backwash Now", command=self.run_backwash_now, bg=BLUE, fg="white",
                  font=FB, pady=5).pack(fill="x", pady=(8, 0))

        csv_card = self._card(parent, "CSV Flow Logging")
        self._row_entry(csv_card, "Flow CSV interval", self.log_interval, "seconds")
        tk.Label(csv_card, text="Default is 60 seconds, so one flow reading is written every minute.",
                 bg=CARD, fg=GRAY, font=F, wraplength=360, justify="left").pack(anchor="w", pady=(2, 6))
        tk.Button(csv_card, text="Choose CSV File", command=self.choose_csv, font=FB,
                  pady=4).pack(fill="x", pady=(2, 4))
        self.csv_label = tk.Label(csv_card, text="No CSV file selected", bg=CARD, fg=GRAY, font=F, anchor="w")
        self.csv_label.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(csv_card, text="Enable CSV logging", variable=self.csv_enabled, command=self.toggle_csv,
                       bg=CARD, font=F).pack(anchor="w", pady=(4, 0))

        adv = self._collapsible_card(parent, "Advanced / Setup")
        tk.Label(adv, text="For setup and troubleshooting only. Normal users only set target flow and press Start.",
                 bg=CARD, fg=GRAY, font=F, justify="left", wraplength=360).pack(anchor="w", pady=(0, 4))
        tk.Label(adv, text="SONOTEC 4-20 mA converter reading:", bg=CARD, fg=DARK,
                 font=FB).pack(anchor="w", pady=(4, 2))
        self._row_entry(adv, "Zero flow voltage", self.zero_flow_voltage, "V")
        self._row_entry(adv, "Calibration voltage", self.calibration_flow_voltage, "V")
        self._row_entry(adv, "Calibration flow", self.calibration_flow_ml_min, "mL/min")
        tk.Checkbutton(adv, text="Invert signal / voltage drops when flow increases", variable=self.invert_signal,
                       bg=CARD, font=F).pack(anchor="w", pady=(4, 0))
        tk.Label(adv, text="Current firmware sends FLOW_RAW, FLOW_V, FLOW_MA, and FLOW_ML_MIN. Converter voltage scaling is set in the Arduino constants.",
                 bg=CARD, fg=GRAY, font=F, wraplength=360, justify="left").pack(anchor="w", pady=(4, 4))
        tk.Label(adv, textvariable=self.calibration_status,
                 bg=CARD, fg=GRAY, font=F, wraplength=360, justify="left").pack(anchor="w", pady=(4, 8))

        tk.Label(adv, text="Pump feedback tuning:", bg=CARD, fg=DARK, font=FB).pack(anchor="w", pady=(4, 2))
        self._row_entry(adv, "Flow tolerance", self.tolerance, "mL/min")
        self._row_entry(adv, "Proportional gain Kp", self.kp, "% per mL/min")
        self._row_entry(adv, "Correction step", self.step_pct, "%")
        self._row_entry(adv, "Correction interval", self.correction_interval, "seconds")
        self._row_entry(adv, "Safe start command", self.safe_start_cmd, "%")
        self._row_entry(adv, "Pump 1 min command", self.min_cmd, "%")
        self._row_entry(adv, "Pump 1 max command", self.max_cmd, "%")

        tk.Label(adv, text="Backwash logic:", bg=CARD, fg=DARK, font=FB).pack(anchor="w", pady=(8, 2))
        tk.Label(adv, text="Filtration: Valve 1 OPEN, Valve 2 CLOSED\nBackwash: Valve 1 CLOSED, Valve 2 OPEN",
                 bg="#edf7ed", fg="#205723", justify="left", font=F, padx=8, pady=6).pack(fill="x", pady=(6, 0))
        tk.Label(adv, text="Pump 2 / backwash pump is ON/OFF only with the current relay hardware.",
                 bg=CARD, fg=GRAY, justify="left", font=F).pack(anchor="w", pady=(6, 0))

        tk.Button(adv, text="Save Advanced Setup", command=lambda: self.save_config(show_message=True),
                  font=FB).pack(fill="x", pady=(8, 0))

        trial = self._card(parent, "Automatic Trial")
        br = tk.Frame(trial, bg=CARD)
        br.pack(fill="x")
        tk.Button(br, text="Start Automatic Trial", command=self.start_trial, bg=GREEN, fg="white",
                  font=FB, pady=5).pack(side="left", fill="x", expand=True, padx=(0, 5))
        tk.Button(br, text="Stop Trial", command=self.stop_trial, bg=RED, fg="white",
                  font=FB, pady=5).pack(side="left", fill="x", expand=True)

    def _stat_card(self, parent, title, value, sub=""):
        frame = tk.Frame(parent, bg=CARD, padx=10, pady=8, highlightthickness=1, highlightbackground="#d0d5dd")
        tk.Label(frame, text=title, bg=CARD, fg=GRAY, font=F).pack(anchor="w")
        lbl = tk.Label(frame, text=value, bg=CARD, fg=DARK, font=BIG)
        lbl.pack(anchor="w")
        sublbl = tk.Label(frame, text=sub, bg=CARD, fg=GRAY, font=F)
        sublbl.pack(anchor="w")
        return frame, lbl, sublbl

    def _build_live_area(self, parent):
        status = tk.Frame(parent, bg=BG)
        status.pack(fill="x")
        cards = []
        for title, value in [
            ("Measured Flow", "0.00 mL/min"),
            ("A0 Voltage / Current", "0.000 V"),
            ("Target Flow", "5.00 mL/min"),
            ("Pump 1 Command", "0%"),
            ("Mode", "Idle"),
        ]:
            frame, lbl, sub = self._stat_card(status, title, value)
            frame.pack(side="left", fill="x", expand=True, padx=(0, 8))
            cards.append((lbl, sub))
        (self.flow_lbl, self.flow_sub), (self.voltage_lbl, self.voltage_sub), (self.target_lbl, self.target_sub), \
            (self.pump_lbl, self.pump_sub), (self.mode_lbl, self.mode_sub) = cards

        graph_frame = tk.Frame(parent, bg=CARD, padx=8, pady=8)
        graph_frame.pack(fill="both", expand=True, pady=(10, 10))
        self.fig = Figure(figsize=(7, 4), dpi=100)
        self.ax_flow = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(fill="x")
        serial_debug = tk.LabelFrame(bottom, text="Arduino Serial Log", font=FB, bg=CARD, fg=DARK, padx=6, pady=4)
        serial_debug.pack(side="left", fill="both", expand=True)
        self.serial_text = tk.Text(serial_debug, height=5, font=MONO, wrap="word")
        self.serial_text.pack(fill="both", expand=True)

        self.status_line = tk.Label(self, textvariable=self.arduino_status, bg=BG, fg=GRAY, font=F, anchor="w")
        self.status_line.pack(fill="x", padx=10, pady=(0, 2))
        self.flow_sensor_status_line = tk.Label(self, textvariable=self.flow_sensor_status, bg=BG, fg=GRAY, font=F, anchor="w")
        self.flow_sensor_status_line.pack(fill="x", padx=10, pady=(0, 6))

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.arduino_combo["values"] = ports
        self.flow_sensor_combo["values"] = ports
        if not self.arduino_port.get() and ports:
            self.arduino_port.set(ports[0])
        if "COM10" in ports:
            self.flow_sensor_port.set("COM10")
        elif not self.flow_sensor_port.get() and ports:
            self.flow_sensor_port.set(ports[0])

    def connect_arduino(self):
        port = self.arduino_port.get().strip()
        if not port:
            messagebox.showerror("Arduino Port", "Select the Arduino COM port first.")
            return
        try:
            self.disconnect_arduino()
            self.arduino = serial.Serial(port, BAUD, timeout=0.2)
            time.sleep(1.8)
            self.arduino_reader_alive.set()
            threading.Thread(target=self.arduino_reader, daemon=True).start()
            self.arduino_status.set(f"Arduino: connected on {port}")
            self.send_arduino("STATUS")
        except Exception as e:
            self.arduino_status.set("Arduino: connection failed")
            messagebox.showerror("Could not connect Arduino",
                                 f"Could not connect to Arduino on {port}.\n\n"
                                 "Make sure Arduino Serial Monitor is closed.\n\n"
                                 f"Error: {e}")

    def disconnect_arduino(self):
        self.arduino_reader_alive.clear()
        if self.arduino:
            try:
                self.arduino.close()
            except Exception:
                pass
        self.arduino = None
        self.arduino_status.set("Arduino: disconnected")

    def connect_flow_sensor(self):
        port = self.flow_sensor_port.get().strip() or "COM10"
        self.disconnect_flow_sensor()
        self.flow_sensor_status.set(f"Flow sensor: connecting on {port}...")
        self.flow_sensor_alive.set()
        threading.Thread(target=self.flow_sensor_reader, args=(port,), daemon=True).start()

    def disconnect_flow_sensor(self):
        self.flow_sensor_alive.clear()
        if self.flow_sensor:
            try:
                self.flow_sensor.stop()
            except Exception:
                pass
        if self.flow_sensor_port_handle:
            try:
                self.flow_sensor_port_handle.close()
            except Exception:
                pass
        self.flow_sensor = None
        self.flow_sensor_port_handle = None
        self.flow_sensor_scale = None
        self.sensor_read_ok = False
        if self.flow_source == "SHDLC":
            self.flow_source = "Arduino"
        self.flow_sensor_status.set("Flow sensor: disconnected")

    def arduino_reader(self):
        while self.arduino_reader_alive.is_set() and self.arduino:
            try:
                raw = self.arduino.readline()
                if raw:
                    self.arduino_q.put(raw.decode(errors="replace").strip())
            except Exception as e:
                self.arduino_q.put(("ERROR", str(e)))
                break

    def flow_sensor_reader(self, port_name):
        try:
            port = ShdlcSerialPort(port=port_name, baudrate=115200)
            self.flow_sensor_port_handle = port
            sensor = FlowSensor(ShdlcConnection(port), slave_address=0)
            self.flow_sensor = sensor
            scale = sensor.read_scale_factor()
            self.flow_sensor_scale = scale
            sensor.start(water=True, sampling_ms=20)
            time.sleep(0.2)
            self.flow_sensor_q.put(("CONNECTED", f"{port_name}, scale {scale}"))

            while self.flow_sensor_alive.is_set():
                try:
                    flow_ul_min, temp_c, bubble = sensor.get_last(scale)
                    flow_ml_min = flow_ul_min / 1000.0
                    self.flow_sensor_q.put(("FLOW", flow_ml_min, temp_c, bubble))
                except Exception as e:
                    self.flow_sensor_q.put(("READ_ERROR", str(e)))
                time.sleep(0.5)
        except Exception as e:
            self.flow_sensor_q.put(("CONNECT_ERROR", str(e)))
        finally:
            try:
                if self.flow_sensor:
                    self.flow_sensor.stop()
            except Exception:
                pass
            try:
                if self.flow_sensor_port_handle:
                    self.flow_sensor_port_handle.close()
            except Exception:
                pass
            self.flow_sensor = None
            self.flow_sensor_port_handle = None
            self.flow_sensor_alive.clear()

    def send_arduino(self, cmd):
        if not self.arduino or not self.arduino.is_open:
            return False
        try:
            self.arduino.write((cmd + "\n").encode())
            self.log_arduino(f"<< {cmd}")
            if cmd.startswith("SPEED "):
                self.last_speed_command = cmd
            return True
        except Exception as e:
            self.handle_arduino_disconnect(str(e))
            return False

    def poll_queues(self):
        while not self.arduino_q.empty():
            item = self.arduino_q.get_nowait()
            if isinstance(item, tuple):
                self.handle_arduino_disconnect(item[1])
            else:
                self.log_arduino(f">> {item}")
                if item.startswith("STATUS,"):
                    self.parse_arduino_status(item)

        while not self.flow_sensor_q.empty():
            item = self.flow_sensor_q.get_nowait()
            kind = item[0]
            if kind == "CONNECTED":
                self.sensor_read_ok = True
                self.flow_source = "SHDLC"
                self.sensor_last_rx = time.monotonic()
                self.flow_sensor_status.set(f"Flow sensor: connected ({item[1]})")
                self.log_arduino(f"[FLOW] connected: {item[1]}")
            elif kind == "FLOW":
                _kind, flow_ml_min, temp_c, bubble = item
                self.flow_ml = max(0.0, float(flow_ml_min))
                self.sensor_temp_c = float(temp_c)
                self.sensor_bubble = int(bubble)
                self.sensor_read_ok = True
                self.flow_source = "SHDLC"
                self.sensor_last_rx = time.monotonic()
                suffix = " [AIR/BUBBLE]" if self.sensor_bubble else ""
                self.flow_sensor_status.set(
                    f"Flow sensor: live {self.flow_ml:.3f} mL/min, {self.sensor_temp_c:.1f} C{suffix}"
                )
                self.record_data_point()
            elif kind == "READ_ERROR":
                self.sensor_read_ok = False
                self.flow_sensor_status.set(f"Flow sensor: read error, holding pump speed ({item[1]})")
                self.log_arduino(f"[FLOW] read error: {item[1]}")
            elif kind == "CONNECT_ERROR":
                self.sensor_read_ok = False
                if self.flow_source == "SHDLC":
                    self.flow_source = "Arduino"
                self.flow_sensor_status.set(f"Flow sensor: connection failed ({item[1]})")
                self.log_arduino(f"[FLOW] connection failed: {item[1]}")

        if self.flow_source == "SHDLC" and self.sensor_read_ok and (time.monotonic() - self.sensor_last_rx) > 2.5:
            self.sensor_read_ok = False
            self.flow_sensor_status.set("Flow sensor: stale reading, holding pump speed")

        self.update_cards()
        self.after(120, self.poll_queues)

    def parse_arduino_status(self, line):
        parts = {}
        for chunk in line.split(",")[1:]:
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                parts[k.strip()] = v.strip()
        self.pump1_status = parts.get("PUMP1", self.pump1_status)
        self.backwash_status = parts.get("BACKWASH", self.backwash_status)
        self.valve1_status = parts.get("VALVE1", self.valve1_status)
        self.valve2_status = parts.get("VALVE2", self.valve2_status)
        if "SPEED" in parts:
            self.pump_cmd = inum(tk.StringVar(value=parts["SPEED"]), self.pump_cmd)
        if "FLOW_RAW" in parts:
            self.flow_raw = inum(tk.StringVar(value=parts["FLOW_RAW"]), self.flow_raw)
        if "FLOW_V" in parts:
            try:
                self.flow_voltage = float(parts["FLOW_V"])
            except Exception:
                pass
        if "FLOW_MA" in parts:
            try:
                self.flow_ma = float(parts["FLOW_MA"])
            except Exception:
                pass
        if "FLOW_ML_MIN" in parts:
            try:
                arduino_flow = float(parts["FLOW_ML_MIN"])
                if self.flow_source != "SHDLC":
                    self.flow_ml = arduino_flow
            except Exception:
                pass
        elif "FLOW_V" in parts:
            if self.flow_source != "SHDLC":
                self.flow_ml = self.flow_voltage_to_ml(self.flow_voltage)
        if self.flow_ml < 0:
            self.flow_ml = 0.0
        self.record_data_point()

    def record_data_point(self):
        now = time.monotonic()
        self.t_data.append(now)
        self.flow_data.append(self.flow_ml)

    def flow_voltage_to_ml(self, voltage):
        zero_v = fnum(self.zero_flow_voltage, DEFAULT_CONFIG["zero_flow_voltage"])
        cal_v = fnum(self.calibration_flow_voltage, DEFAULT_CONFIG["calibration_flow_voltage"])
        cal_flow = fnum(self.calibration_flow_ml_min, DEFAULT_CONFIG["calibration_flow_ml_min"])
        denom = cal_v - zero_v
        if abs(denom) < 0.000001:
            return 0.0
        return (voltage - zero_v) * cal_flow / denom

    def update_calibration_status(self):
        target = fnum(self.target_flow, 5.0)
        self.calibration_status.set(
            f"Debug: FLOW_RAW={self.flow_raw}, FLOW_V={self.flow_voltage:.3f} V, "
            f"FLOW_MA={self.flow_ma:.3f} mA, FLOW_ML_MIN={self.flow_ml:.2f}, "
            f"zero={fnum(self.zero_flow_voltage, DEFAULT_CONFIG['zero_flow_voltage']):.3f} V, "
            f"cal_v={fnum(self.calibration_flow_voltage, DEFAULT_CONFIG['calibration_flow_voltage']):.3f} V, "
            f"cal_flow={fnum(self.calibration_flow_ml_min, DEFAULT_CONFIG['calibration_flow_ml_min']):.2f} mL/min, "
            f"source={self.flow_source}, measured={self.flow_ml:.2f} mL/min, target={target:.2f} mL/min, "
            f"pump={self.pump_cmd}%, last speed={self.last_speed_command}"
        )

    def set_filtration_valves(self):
        self.send_arduino("BACKWASH OFF")
        self.send_arduino("VALVE2 OFF")
        self.send_arduino("VALVE1 ON")

    def set_backwash_valves(self):
        self.send_arduino("PUMP1 OFF")
        self.send_arduino("SPEED 0")
        self.send_arduino("VALVE1 OFF")
        self.send_arduino("VALVE2 ON")

    def start_filtration(self):
        if not self.ensure_arduino():
            return
        self.cancel_scheduled_steps()
        self.starting_pump_cmd = clamp(inum(self.safe_start_cmd, 5), 0, 100)
        self.pump_cmd = self.starting_pump_cmd
        self.mode = "Filtration"
        self.filtration_running = True
        self.backwash_running = False
        self.trial_started_at = time.monotonic()
        self.phase_started_at = time.monotonic()
        self.next_control_at = time.monotonic()
        self.set_filtration_valves()
        self.send_arduino(f"SPEED {self.pump_cmd}")
        self.send_arduino("PUMP1 ON")

    def stop_filtration(self):
        self.filtration_running = False
        if not self.trial_running:
            self.mode = "Idle"
        self.send_arduino("PUMP1 OFF")
        self.send_arduino("SPEED 0")
        self.pump_cmd = 0

    def start_trial(self):
        if not self.ensure_arduino():
            return
        if not messagebox.askyesno(
            "Start Automatic Trial?",
            "Start automatic filtration/backwash cycling?\n\n"
            f"Target flow: {fnum(self.target_flow, 5):.2f} mL/min\n"
            f"Break before backwash: {clamp(fnum(self.break_before_backwash_minutes, 0), 0, 30):.1f} minutes\n"
            f"Backwash time: {fnum(self.backwash_minutes, 2):.1f} minutes\n"
            f"Backwash pump flowrate setting: {fnum(self.backwash_flowrate, 20):.2f} mL/min\n\n"
            "Valve 1 will open during filtration. Valve 2 will open during backwash.",
        ):
            return
        self.trial_running = True
        self.start_filtration()
        self.mode = "Trial Filtration"

    def stop_trial(self):
        self.trial_running = False
        self.backwash_running = False
        self.filtration_running = False
        self.mode = "Idle"
        self.cancel_scheduled_steps()
        self.all_off(confirm=False)

    def run_backwash_now(self):
        if not self.ensure_arduino():
            return
        self.start_backwash_sequence(resume_after=self.filtration_running or self.trial_running)

    def start_backwash_sequence(self, resume_after=True):
        self.cancel_scheduled_steps()
        self.backwash_running = True
        self.filtration_running = False
        self.mode = "Backwash"
        self.set_backwash_valves()
        self.send_arduino("BACKWASH ON")
        duration_ms = int(max(0, fnum(self.backwash_minutes, 2)) * 60_000)
        aid = self.after(duration_ms, lambda: self.finish_backwash(resume_after))
        self.pending_after_ids.append(aid)

    def start_break_before_backwash(self):
        self.cancel_scheduled_steps()
        self.filtration_running = False
        self.backwash_running = False
        self.mode = "Break Before Backwash"
        self.send_arduino("PUMP1 OFF")
        self.send_arduino("SPEED 0")
        self.pump_cmd = 0
        break_minutes = clamp(fnum(self.break_before_backwash_minutes, 0), 0, 30)
        if break_minutes <= 0:
            self.start_backwash_sequence(resume_after=True)
            return
        aid = self.after(int(break_minutes * 60_000), lambda: self.start_backwash_sequence(resume_after=True))
        self.pending_after_ids.append(aid)

    def finish_backwash(self, resume_after):
        self.send_arduino("BACKWASH OFF")
        self.send_arduino("VALVE2 OFF")
        self.send_arduino("VALVE1 ON")
        self.backwash_running = False
        if resume_after and self.trial_running:
            self.pump_cmd = clamp(inum(self.safe_start_cmd, 5), inum(self.min_cmd, 0), inum(self.max_cmd, 100))
            self.send_arduino(f"SPEED {self.pump_cmd}")
            self.send_arduino("PUMP1 ON")
            self.mode = "Trial Filtration"
            self.filtration_running = True
            self.phase_started_at = time.monotonic()
            self.next_control_at = time.monotonic() + max(1, fnum(self.correction_interval, 5))
        elif resume_after:
            self.start_filtration()
        else:
            self.mode = "Idle"

    def periodic_tasks(self):
        now = time.monotonic()
        if self.filtration_running and self.flow_control_enabled.get() and now >= self.next_control_at:
            self.apply_flow_control()
            self.next_control_at = now + max(1, fnum(self.correction_interval, 5))

        if self.trial_running and self.filtration_running and self.phase_started_at:
            filtration_s = max(0, fnum(self.filtration_minutes, 60)) * 60
            if filtration_s > 0 and now - self.phase_started_at >= filtration_s:
                self.start_break_before_backwash()

        if self.csv_enabled.get():
            self.maybe_log_csv()

        if self.arduino and self.arduino.is_open:
            self.send_arduino("STATUS")
        self.after(1000, self.periodic_tasks)

    def apply_flow_control(self):
        if self.flow_source == "SHDLC" and not self.sensor_read_ok:
            return
        target = fnum(self.target_flow, 5.0)
        tol = max(0, fnum(self.tolerance, 0.2))
        max_step = max(0, fnum(self.step_pct, 2))
        kp = max(0, fnum(self.kp, DEFAULT_CONFIG["kp"]))
        lo = clamp(inum(self.min_cmd, 0), 0, 100)
        hi = clamp(inum(self.max_cmd, 100), 0, 100)
        if lo > hi:
            lo, hi = hi, lo
        new_cmd = self.pump_cmd
        error = target - self.flow_ml
        if abs(error) > tol:
            correction = kp * error
            if max_step > 0:
                correction = clamp(correction, -max_step, max_step)
            new_cmd = self.pump_cmd + correction
        new_cmd = int(round(clamp(new_cmd, lo, hi)))
        if new_cmd != self.pump_cmd:
            self.pump_cmd = new_cmd
            self.send_arduino(f"SPEED {self.pump_cmd}")

    def all_off(self, confirm=True):
        if confirm and not messagebox.askyesno("ALL OFF", "Stop pumps, close both valves, and set Pump 1 command to 0%?"):
            return
        self.trial_running = False
        self.filtration_running = False
        self.backwash_running = False
        self.mode = "Idle"
        self.cancel_scheduled_steps()
        self.send_arduino("ALL OFF")
        self.pump_cmd = 0

    def ensure_arduino(self):
        if not self.arduino or not self.arduino.is_open:
            messagebox.showerror("Arduino Not Connected", "Connect the Arduino COM port first.")
            return False
        return True

    def handle_arduino_disconnect(self, error):
        was_running = self.trial_running or self.filtration_running or self.backwash_running
        self.disconnect_arduino()
        self.trial_running = False
        self.filtration_running = False
        self.backwash_running = False
        self.mode = "Arduino disconnected"
        if was_running:
            messagebox.showerror("Arduino Disconnected",
                                 "Arduino connection was lost. Automatic control has stopped.\n\n"
                                 "Use the physical power switch or emergency stop if the system is still running.\n\n"
                                 f"Error: {error}")

    def cancel_scheduled_steps(self):
        for aid in self.pending_after_ids:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self.pending_after_ids.clear()

    def update_cards(self):
        target = fnum(self.target_flow, 5)
        err = self.flow_ml - target
        self.flow_lbl.config(text=f"{self.flow_ml:.2f} mL/min")
        if self.flow_source == "SHDLC":
            self.flow_sub.config(text="From SHDLC sensor on COM10")
            self.voltage_lbl.config(text="SHDLC")
            self.voltage_sub.config(text=f"{self.sensor_temp_c:.1f} C" + (" AIR/BUBBLE" if self.sensor_bubble else ""))
        else:
            self.flow_sub.config(text=f"Arduino fallback FLOW_RAW {self.flow_raw}")
            self.voltage_lbl.config(text=f"{self.flow_voltage:.3f} V")
            self.voltage_sub.config(text=f"{self.flow_ma:.3f} mA from converter")
        self.target_lbl.config(text=f"{target:.2f} mL/min")
        self.target_sub.config(text=f"Error: {err:+.2f} mL/min")
        self.pump_lbl.config(text=f"{self.pump_cmd}%")
        self.pump_sub.config(text=f"Pump 1: {self.pump1_status}")
        self.mode_lbl.config(text=self.mode)
        elapsed = 0 if not self.trial_started_at else time.monotonic() - self.trial_started_at
        self.mode_sub.config(text=f"{fmt_elapsed(elapsed)} | V1 {self.valve1_status} / V2 {self.valve2_status} | BW {self.backwash_status} @ {fnum(self.backwash_flowrate, 20):.1f} mL/min")
        self.update_calibration_status()

    def refresh_graphs(self):
        self.ax_flow.clear()
        if self.t_data:
            t0 = self.t_data[0]
            x = [(t - t0) / 60 for t in self.t_data]
            self.ax_flow.plot(x, list(self.flow_data), color=BLUE, label="Measured flow")
        else:
            x = [0]
        target = fnum(self.target_flow, 5)
        self.ax_flow.axhline(target, color=GREEN, linestyle="--", label="Target flow")
        self.ax_flow.set_ylabel("mL/min")
        self.ax_flow.set_title("Flowrate vs Time")
        self.ax_flow.grid(True, alpha=0.25)
        self.ax_flow.legend(loc="upper right")
        self.ax_flow.set_xlabel("Time (minutes)")
        self.ax_flow.set_ylim(0, 20)
        self.fig.tight_layout()
        self.canvas.draw_idle()
        self.after(1500, self.refresh_graphs)

    def choose_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if path:
            self.csv_path = path
            self.csv_label.config(text=os.path.basename(path), fg=DARK)

    def toggle_csv(self):
        if self.csv_enabled.get():
            if not self.csv_path:
                messagebox.showwarning("CSV File", "Choose a CSV file first.")
                self.csv_enabled.set(False)
                return
            new_file = not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0
            self.csv_file = open(self.csv_path, "a", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            if new_file:
                self.csv_writer.writerow([
                    "timestamp",
                    "elapsed_time",
                    "mode",
                    "target_flow_mL_min",
                    "measured_flow_mL_min",
                    "flow_source",
                    "flow_raw",
                    "flow_voltage",
                    "flow_mA",
                    "pump1_command_percent",
                    "valve1_status",
                    "valve2_status",
                    "pump1_status",
                    "backwash_pump_status",
                ])
            self.next_log_at = 0
        else:
            if self.csv_file:
                self.csv_file.close()
            self.csv_file = None
            self.csv_writer = None

    def maybe_log_csv(self):
        if not self.csv_writer:
            return
        now = time.monotonic()
        if now < self.next_log_at:
            return
        self.next_log_at = now + max(1, fnum(self.log_interval, 60))
        elapsed = 0 if not self.trial_started_at else now - self.trial_started_at
        target = fnum(self.target_flow, 5)
        self.csv_writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fmt_elapsed(elapsed),
            self.mode,
            f"{target:.3f}",
            f"{self.flow_ml:.3f}",
            self.flow_source,
            self.flow_raw,
            f"{self.flow_voltage:.3f}",
            f"{self.flow_ma:.3f}",
            self.pump_cmd,
            self.valve1_status,
            self.valve2_status,
            self.pump1_status,
            self.backwash_status,
        ])
        self.csv_file.flush()

    def log_arduino(self, text):
        self.serial_text.insert("end", text + "\n")
        self.serial_text.see("end")
        while int(self.serial_text.index("end-1c").split(".")[0]) > 120:
            self.serial_text.delete("1.0", "2.0")

    def on_close(self):
        if self.trial_running or self.filtration_running or self.backwash_running:
            if not messagebox.askyesno("Exit", "System is running. Send ALL OFF and exit?"):
                return
            self.all_off(confirm=False)
        self.save_config(show_message=False)
        self.disconnect_arduino()
        self.disconnect_flow_sensor()
        if self.csv_file:
            self.csv_file.close()
        self.destroy()


if __name__ == "__main__":
    FlowControlApp().mainloop()
