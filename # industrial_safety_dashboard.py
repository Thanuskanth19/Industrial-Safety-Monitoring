import sys, time, threading, math, random, os
from datetime import datetime
import serial, serial.tools.list_ports
import pandas as pd
from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, jsonify
# ---------- Thresholds to match Arduino sketch ----------
MAX_ADC = 1023.0 # 10-bit ADC max
# (LM35 conversion no longer used, temp comes directly in °C from DHT22 on Arduino)
VREF = 5.0
TEMP_SCALE = VREF * 100.0 / 1023.0
# Temperature thresholds (°C) – MUST match Arduino DHT22 code
TEMP_WARN = 32.0 # < 32 -> Safe (green)
TEMP_CRITICAL = 35.0 # >=38 -> Critical (red)
TEMP_RELAY_ON = 35.0 # Fan ON
TEMP_RELAY_OFF = 33.0 # Fan OFF
# --- Gas thresholds (RAW ADC, same as Arduino) ---
GAS_SAFE_MAX = 300 # safe -> green
GAS_MODERATE_MAX = 600 # mid -> orange
# >600 = red
# --- Vibration threshold (RAW ADC, same as Arduino) ---
VIB_THRESHOLD = 900 # same as Arduino VIB_THRESHOLD
# ---------- Project details (edit these) ----------
PROJECT_INFO = {
    "course": "IA 3018 – Data Acquisition Systems Project",
    "presenter":"M. Thanuskanth",
    "regno": "2022T01357",
    "topic": "Industrial Safety Monitoring System (Temp, Gas, Vibration)",
    "note": "Real-time monitoring of temperature, gas concentration and vibration using Arduino + Python DAQ dashboard. Designed for industrial safety alerts.",
    "image_path": "logo.png",
    "image_max_w": 275,
    "image_round": 150,
}
# ---------- App icon (small embedded) ----------
_APP_ICON_B64 = ("iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAAsSAAALEgHS3X78AAAB"
                 "c0lEQVRYhe3Xv0sVURTG8Y8l1LJ0m2XkG2b2QyG0hQkqJk0iV4V1Tw1xv1C1W6zv2v7kq9w2xv0vE"
                 "3wqkqXlQwqQlq6k0QeRr7wWwP9wY6xCHQ6M/8h3y4xn9w4wL5xkH5yP3z8zkmk0k3a0o0k1m4sK2L"
                 "5m4h4g5D0mJ1r2Dq7g3NQ1mmpFxg7Uj0oB7Yc6gq2k3VhHqS0Xo5Cw3+eQ0l5bR0b2FQ5dBqY8r4m"
                 "4n7KxYxYFJbV6i7r6JrF1h4g7GJ7JY2zC4p4j9IuZrA5YtQK1gY2KkNfD8j/FK9kqz3m2LwQq7wQw"
                 "W2YcYgW7QmV1m8J0kJ8mN7xYq+WzKxk5r4qf2d0V9b0G7oC1Hqj7qXwT1Q7dKQ3j0X3F3u5mQ8JvZ"
                 "mT0yXc6mVY4VQb9n2g5Hn1aQ9kF2x7B4q0dMZtVvQk5D9q2d7W9B6G2sPpCQy8m5o7/1Qq2o8T+4S"
                 "7kG7aB0cAAAAAElFTkSuQmCC")
def app_icon():
    ba = QtCore.QByteArray.fromBase64(_APP_ICON_B64.encode())
    pm = QtGui.QPixmap()
    pm.loadFromData(ba, "PNG")
    return QtGui.QIcon(pm)
# ---------- Simple EMA ----------
class EMA:
    def __init__(self, a=0.15):
        self.a = float(a)
        self.y = None
    def push(self, x):
        x = float(x)
        if self.y is None:
            self.y = x
        else:
            self.y = self.a * x + (1 - self.a) * self.y
        return self.y
# ---------- Circular Gauge ----------
class CircularGauge(QtWidgets.QWidget):
    valueChanged = QtCore.pyqtSignal(float)
    def __init__(self, title="Gauge", units="", minimum=0.0, maximum=100.0, parent=None):
        super().__init__(parent)
        self._title, self._units = title, units
        self._min, self._max = float(minimum), float(maximum)
        self._value = float(minimum)
        self._zones = [] # list of (end_value, QColor)
        self._accent = QtGui.QColor(59, 130, 246)
        self._highlight = False
        self.setMinimumSize(200, 210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    def setRange(self, mn, mx):
        self._min, self._max = float(mn), float(mx)
        self.update()
    def setValue(self, v):
        v = max(min(float(v), self._max), self._min)
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.valueChanged.emit(v)
            self.update()
    def setTitle(self, t):
        self._title = str(t)
        self.update()
    def setUnits(self, u):
        self._units = str(u)
        self.update()
    def setZones(self, zones):
        z = []
        for end, col in zones:
            if not isinstance(col, QtGui.QColor):
                col = QtGui.QColor(*col)
            z.append((float(end), col))
        self._zones = z
        self.update()
    def setAccent(self, c):
        self._accent = c
        self.update()
    def setHighlight(self, f):
        self._highlight = bool(f)
        self.update()
    def _frac(self, v):
        if self._max == self._min:
            return 0.0
        return (v - self._min) / (self._max - self._min)
    def paintEvent(self, ev):
        r = self.rect()
        pad = 18
        th = 22
        vh = 30
        d = min(r.width() - 2 * pad, r.height() - th - vh - 2 * pad)
        cx, cy = r.width() // 2, r.top() + th + pad + d // 2
        rect = QtCore.QRectF(cx - d // 2, cy - d // 2, d, d)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        # Title
        f = QtGui.QFont("Segoe UI", 10, QtGui.QFont.DemiBold)
        p.setFont(f)
        p.setPen(QtGui.QColor("#e2e8f0"))
        p.drawText(QtCore.QRectF(0, 0, r.width(), th),
                   QtCore.Qt.AlignCenter, self._title)
        # Background circle
        p.setBrush(QtGui.QColor("#3b3f52"))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(rect)
        # Base arc
        arc_w = 18 if self._highlight else 14
        pen = QtGui.QPen(QtGui.QColor("#5a6078"), arc_w)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        # Filled arc
        frac = self._frac(self._value)
        start = 90
        if self._zones:
            cur = 0.0
            for end, col in self._zones:
                endf = self._frac(end)
                s, e = cur, min(frac, endf)
                if e > s:
                    a0 = start - s * 360
                    a1 = start - e * 360
                    pen = QtGui.QPen(col, arc_w)
                    pen.setCapStyle(QtCore.Qt.RoundCap)
                    p.setPen(pen)
                    p.drawArc(rect, int(a0 * 16), int((a1 - a0) * 16))
                cur = endf
                if frac <= endf:
                    break
            if frac > cur:
                a0 = start - cur * 360
                a1 = start - frac * 360
                pen = QtGui.QPen(self._accent, arc_w)
                pen.setCapStyle(QtCore.Qt.RoundCap)
                p.setPen(pen)
                p.drawArc(rect, int(a0 * 16), int((a1 - a0) * 16))
        else:
            a0 = start
            a1 = start - frac * 360
            pen = QtGui.QPen(self._accent, arc_w)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, int(a0 * 16), int((a1 - a0) * 16))
        # Value text
        p.setPen(QtGui.QColor("#ffffff"))
        f = QtGui.QFont("Segoe UI", 16, QtGui.QFont.Bold)
        p.setFont(f)
        p.drawText(QtCore.QRectF(cx - 70, cy - 16, 140, 32),
                   QtCore.Qt.AlignCenter, f"{self._value:.1f}")
        # Units
        f = QtGui.QFont("Segoe UI", 11)
        p.setFont(f)
        p.drawText(QtCore.QRectF(cx - 70, cy + 6, 140, 20),
                   QtCore.Qt.AlignCenter, self._units)
        # Min/Max labels
        p.setPen(QtGui.QColor("#aab0c0"))
        f = QtGui.QFont("Segoe UI", 8)
        p.setFont(f)
        p.drawText(QtCore.QRectF(pad, cy + d // 2 + 5, 50, 14),
                   QtCore.Qt.AlignLeft, f"{self._min:.0f}")
        p.drawText(QtCore.QRectF(r.width() - pad - 50, cy + d // 2 + 5, 50, 14),
                   QtCore.Qt.AlignRight, f"{self._max:.0f}")
# ---------- Right-side info panel (with bottom-right image + fan animation) ----------
class InfoPanel(QtWidgets.QWidget):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.setMinimumWidth(330)
        self.setMaximumWidth(430)
        self.fanMovie = None
        self.fanLabel = None
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)
        card = QtWidgets.QFrame(objectName="card")
        card.setStyleSheet("""
            QFrame#card {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                             stop:0 #15161a, stop:1 #101113);
                border:1px solid #2a2d34;
                border-radius:14px;
            }
        """)
        cl = QtWidgets.QVBoxLayout(card)
        cl.setContentsMargins(18, 18, 18, 18)
        cl.setSpacing(12)
        title = QtWidgets.QLabel("PROJECT")
        title.setStyleSheet("color:#9aa4b2; letter-spacing:2px; font: 11px 'Segoe UI';")
        h1 = QtWidgets.QLabel(info.get("course", "IA 3018 – DAQ Project"))
        h1.setWordWrap(True)
        h1.setStyleSheet("color:#e2e9ed; font: 18px 'Segoe UI Semibold';")
        accent = QtWidgets.QFrame()
        accent.setFixedHeight(3)
        accent.setStyleSheet("background:#2f5bff; border-radius:2px;")
        def row(k, v):
            w = QtWidgets.QWidget()
            l = QtWidgets.QHBoxLayout(w)
            l.setContentsMargins(0, 6, 0, 6)
            kL = QtWidgets.QLabel(k)
            kL.setStyleSheet("color:#8b93a1; font:11.5px 'Segoe UI';")
            vL = QtWidgets.QLabel(v)
            vL.setWordWrap(True)
            vL.setStyleSheet("color:#eaeef7; font:13.5px 'Segoe UI Semibold';")
            l.addWidget(kL)
            l.addStretch(1)
            l.addWidget(vL, 1)
            return w
        r1 = row("Presenter", info.get("presenter", "—"))
        r2 = row("Reg No", info.get("regno", "—"))
        r3 = row("Topic", info.get("topic", "—"))
        note = QtWidgets.QLabel(info.get("note", ""))
        note.setWordWrap(True)
        note.setStyleSheet("""
            color:#c6d0e3; font:12px 'Segoe UI';
            background: rgba(47,91,255,0.10);
            border: 1px solid rgba(47,91,255,0.25);
            border-radius: 10px;
            padding: 10px;
        """)
        # Fan animation label
        self.fanLabel = QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        self.fanLabel.setMinimumHeight(120)
        self.fanLabel.setStyleSheet("background:transparent;")
        self._setup_fan_animation()
        # Bottom-right image
        self.img = QtWidgets.QLabel(alignment=QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.img.setMinimumHeight(120)
        self.img.setStyleSheet("background:transparent;")
        self._load_image()
        # badges row
        badges = QtWidgets.QHBoxLayout()
        badges.setSpacing(6)
        for t in ("Arduino + PyQt5", "Serial DAQ", "Temp / Gas / Vibration"):
            b = QtWidgets.QLabel(t)
            b.setStyleSheet("""
                color:#a9b2c3;
                font:11px 'Segoe UI';
                border:1px solid #2a2d34;
                border-radius:10px;
                padding:4px 8px;
                background:#17181d;
            """)
            badges.addWidget(b)
        badges.addStretch(1)
        cl.addWidget(title)
        cl.addWidget(h1)
        cl.addWidget(accent)
        cl.addSpacing(6)
        cl.addWidget(r1)
        cl.addWidget(r2)
        cl.addWidget(r3)
        if info.get("note"):
            cl.addSpacing(6)
            cl.addWidget(note)
        cl.addSpacing(8)
        cl.addWidget(self.fanLabel, 0, QtCore.Qt.AlignCenter)
        cl.addSpacing(8)
        cl.addLayout(badges)
        cl.addStretch(1)
        cl.addWidget(self.img, 0, QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        outer.addWidget(card, 1)
    def _setup_fan_animation(self):
        gif_path = self.info.get("fan_gif", "")
        if not gif_path:
            self.fanLabel.hide()
            return
        if not os.path.exists(gif_path):
            self.fanLabel.setText("Fan Animation (fan.gif not found)")
            self.fanLabel.setStyleSheet("color:#6b7280; font-size:11px;")
            return
        self.fanMovie = QtGui.QMovie(gif_path)
        self.fanMovie.setCacheMode(QtGui.QMovie.CacheAll)
        self.fanMovie.setSpeed(100)
        self.fanLabel.setMovie(self.fanMovie)
        self.fanMovie.stop()
    def _load_image(self):
        path = self.info.get("image_path", "")
        if not path:
            self.img.hide()
            return
        pm = QtGui.QPixmap(path)
        if pm.isNull():
            self.img.hide()
            return
        max_w = int(self.info.get("image_max_w", 150))
        pm = pm.scaledToWidth(max_w, QtCore.Qt.SmoothTransformation)
        radius = int(self.info.get("image_round", 10))
        rounded = QtGui.QPixmap(pm.size())
        rounded.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(rounded)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pathR = QtGui.QPainterPath()
        pathR.addRoundedRect(QtCore.QRectF(0, 0, pm.width(), pm.height()), radius, radius)
        p.setClipPath(pathR)
        p.drawPixmap(0, 0, pm)
        p.end()
        self.img.setPixmap(rounded)
        self.img.show()
    def setFanRunning(self, running: bool):
        if not self.fanMovie:
            return
        if running:
            if self.fanMovie.state() != QtGui.QMovie.Running:
                self.fanMovie.start()
        else:
            if self.fanMovie.state() != QtGui.QMovie.NotRunning:
                self.fanMovie.stop()
# Flask app creation function
def create_flask_app(daq_app):
    app = Flask(__name__)
    @app.route('/')
    def dashboard():
        return daq_app._get_dashboard_html()
    @app.route('/data')
    def get_data():
        if not daq_app.rows:
            return jsonify({"error": "No data"})
        latest = daq_app.rows[-1]
        alarm = (
            latest["temp_c"] >= TEMP_CRITICAL or
            latest["gas_raw"] >= GAS_MODERATE_MAX or
            latest["vibration_adc_raw"] < VIB_THRESHOLD
        )
        fan_on = latest["temp_c"] >= TEMP_RELAY_ON or latest["gas_raw"] > GAS_MODERATE_MAX
        return jsonify({
            "temp_c": latest["temp_c"],
            "gas_pct": latest["gas_pct"],
            "vib_pct": latest["vibration_pct"],
            "gas_raw": latest["gas_raw"],
            "vib_raw": latest["vibration_adc_raw"],
            "alarm": alarm,
            "fan_on": fan_on,
            "timestamp": latest["datetime"]
        })
    return app
# ========== Main App ==========
class DAQApp(QtWidgets.QWidget):
    DEBUG_SERIAL = False # set True to print every raw line
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Industrial Safety DAQ — Temperature / Gas / Vibration")
        self.setWindowIcon(app_icon())
        self.resize(1340, 900)
        self.ser = None
        self.running = False
        self.rx_thread = None
        # Data buffers
        self.ts = []
        self.temp = []
        self.gas = []
        self.vib = []
        self.gas_raw = []
        self.vib_raw = []
        self.rows = []
        self.ema_gas = EMA(0.2)
        self.ema_vib = EMA(0.2)
        self.fan_on = False
        self._sim_t0 = None
        # Email alert configuration
        self.email_config = {
            'from': '',
            'to': '',
            'pass': '',
            'server': 'smtp.gmail.com',
            'port': 587
        }
        self.prev_alarm = False
        # Web dashboard
        self.flask_app = None
        self.web_thread = None
        self.web_running = False
        # ---------- TOP BAR ----------
        self.portBox = QtWidgets.QComboBox()
        self.baudBox = QtWidgets.QComboBox()
        self.baudBox.addItems(["9600", "115200"])
        self.refreshBtn = QtWidgets.QPushButton("Refresh")
        self.connectBtn = QtWidgets.QPushButton("Connect")
        self.startBtn = QtWidgets.QPushButton("Start")
        self.stopBtn = QtWidgets.QPushButton("Stop")
        self.viewBtn = QtWidgets.QPushButton("View Data") # NEW
        self.saveBtn = QtWidgets.QPushButton("Save CSV")
        self.clearBtn = QtWidgets.QPushButton("Clear")
        self.emailBtn = QtWidgets.QPushButton("Email Config") # NEW
        self.web_btn = QtWidgets.QPushButton("Start Web Dashboard")  # NEW
        self.simChk = QtWidgets.QCheckBox("Simulate")
        self.alertChk = QtWidgets.QCheckBox("Email Alerts") # NEW
        self.fanLabel = QtWidgets.QLabel("Fan: OFF")
        self.fanLabel.setStyleSheet("color:#f97373; font-weight:bold;")
        bar = QtWidgets.QHBoxLayout()
        for w in [
            QtWidgets.QLabel("Port"), self.portBox, self.baudBox,
            self.refreshBtn, self.connectBtn,
            self.startBtn, self.stopBtn,
            self.viewBtn,
            self.saveBtn, self.clearBtn,
            self.emailBtn, # NEW
            self.web_btn,  # NEW
            self.simChk,
            self.alertChk, # NEW
            self.fanLabel
        ]:
            bar.addWidget(w)
        bar.addStretch(1)
        # ---------- LED STATUS ROW ----------
        self.tempLedLabel = QtWidgets.QLabel("Temp LED: GREEN")
        self.gasLedLabel = QtWidgets.QLabel("Gas LED: GREEN")
        self.buzzerLabel = QtWidgets.QLabel("Buzzer: OFF")
        for lbl in (self.tempLedLabel, self.gasLedLabel, self.buzzerLabel):
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lbl.setMinimumWidth(160)
            lbl.setStyleSheet("""
                background:#22c55e;
                color:#ffffff;
                font-weight:bold;
                border-radius:12px;
                padding:4px 10px;
            """)
        ledLayout = QtWidgets.QHBoxLayout()
        tempCol = QtWidgets.QVBoxLayout()
        gasCol = QtWidgets.QVBoxLayout()
        buzCol = QtWidgets.QVBoxLayout()
        tempTitle = QtWidgets.QLabel("Temperature LEDs")
        tempTitle.setStyleSheet("color:#9ca3af; font-size:11px;")
        gasTitle = QtWidgets.QLabel("Gas LEDs")
        gasTitle.setStyleSheet("color:#9ca3af; font-size:11px;")
        buzTitle = QtWidgets.QLabel("Buzzer (Alarm)")
        buzTitle.setStyleSheet("color:#9ca3af; font-size:11px;")
        tempCol.addWidget(tempTitle)
        tempCol.addWidget(self.tempLedLabel)
        gasCol.addWidget(gasTitle)
        gasCol.addWidget(self.gasLedLabel)
        buzCol.addWidget(buzTitle)
        buzCol.addWidget(self.buzzerLabel)
        ledLayout.addLayout(tempCol)
        ledLayout.addSpacing(20)
        ledLayout.addLayout(gasCol)
        ledLayout.addSpacing(20)
        ledLayout.addLayout(buzCol)
        ledLayout.addStretch(1)
        # ---------- GAUGES ----------
        self.gTemp = CircularGauge("Temperature", "°C", 0, 100)
        self.gGas = CircularGauge("Gas Level", "", 0, 100)
        self.gVib = CircularGauge("Vibration", "", 0, 100)
        self.gTemp.setZones([
            (TEMP_WARN, (63, 185, 80)), # green zone end
            (TEMP_CRITICAL, (242, 204, 96)), # orange zone end
            (100, (248, 81, 73)) # red zone
        ])
        self.gGas.setZones([
            (30, (63, 185, 80)),
            (60, (242, 204, 96)),
            (100,(248, 81, 73))
        ])
        self.gVib.setZones([
            (20, (63, 185, 80)),
            (50, (242, 204, 96)),
            (100,(248, 81, 73))
        ])
        ggrid = QtWidgets.QGridLayout()
        ggrid.addWidget(self.gTemp, 0, 0)
        ggrid.addWidget(self.gGas, 0, 1)
        ggrid.addWidget(self.gVib, 0, 2)
        ggrid.setHorizontalSpacing(16)
        ggrid.setVerticalSpacing(16)
        ggrid.setContentsMargins(8, 8, 8, 8)
        # ---------- PLOTS ----------
        pg.setConfigOptions(antialias=True)
        self.pTemp = self._plot_widget("Temperature")
        self.pGas = self._plot_widget("Gas Level")
        self.pVib = self._plot_widget("Vibration")
        self.cTemp = self.pTemp.plot([], [], name="Temp", pen=pg.mkPen(width=3))
        self.cGas = self.pGas.plot([], [], name="Gas", pen=pg.mkPen(width=3))
        self.cVib = self.pVib.plot([], [], name="Vib", pen=pg.mkPen(width=3))
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(ggrid)
        ll.addWidget(self.pTemp, 1)
        ll.addWidget(self.pGas, 1)
        ll.addWidget(self.pVib, 1)
        self.infoPanel = InfoPanel(PROJECT_INFO)
        hsplit = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        hsplit.addWidget(left)
        hsplit.addWidget(self.infoPanel)
        hsplit.setStretchFactor(0, 1)
        hsplit.setStretchFactor(1, 0)
        hsplit.setSizes([1040, 360])
        root = QtWidgets.QVBoxLayout(self)
        root.addLayout(bar)
        root.addLayout(ledLayout)
        root.addWidget(hsplit, 1)
        # ---------- SIGNALS ----------
        self.refreshBtn.clicked.connect(self.refresh_ports)
        self.connectBtn.clicked.connect(self.toggle_connect)
        self.startBtn.clicked.connect(self.start_stream)
        self.stopBtn.clicked.connect(self.stop_stream)
        self.saveBtn.clicked.connect(self.save_csv)
        self.clearBtn.clicked.connect(self.clear_all)
        self.simChk.toggled.connect(self._on_sim_toggle)
        self.viewBtn.clicked.connect(self.show_data_popup)
        self.emailBtn.clicked.connect(self.email_config_dialog) # NEW
        self.web_btn.clicked.connect(self.toggle_web)  # NEW
        self.alertChk.toggled.connect(lambda: None) # Placeholder, used in send
        pg.setConfigOption('background', '#101113')
        pg.setConfigOption('foreground', 'w')
        self.setStyleSheet("""
            QWidget {
                background:#121214;
                color:#e8e8e8;
                font-family:'Segoe UI';
            }
            QPushButton {
                background:#232326;
                border:1px solid #333;
                padding:6px 10px;
                border-radius:8px;
            }
            QPushButton:pressed {
                background:#2a2a2d;
            }
            QComboBox {
                background:#1b1b1d;
                border:1px solid #333;
                padding:4px 6px;
                border-radius:6px;
            }
        """)
        self.refresh_ports()
        self._set_connected(False)
        self._start_ui_timer()
    # ---------- Web Dashboard ----------
    def toggle_web(self):
        if self.web_running:
            # Simple stop: set flag and note thread is daemon
            self.web_running = False
            self.web_btn.setText("Start Web Dashboard")
            QtWidgets.QMessageBox.information(self, "Web Dashboard", "Web dashboard stopped.")
        else:
            self.start_web()
    def start_web(self):
        if self.flask_app:
            return
        self.flask_app = create_flask_app(self)
        def run_server():
            self.flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
        self.web_thread = threading.Thread(target=run_server, daemon=True)
        self.web_thread.start()
        self.web_running = True
        self.web_btn.setText("Stop Web Dashboard")
        self.web_btn.setEnabled(True)  # Keep enabled for toggle
        QtWidgets.QMessageBox.information(self, "Web Dashboard", "Web dashboard started at http://localhost:5000\nOpen in browser for remote access.")
    def _get_dashboard_html(self):
        info = PROJECT_INFO
        html = """
<!DOCTYPE html>
<html>
<head>
    <title>Industrial Safety Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
    html {
            height: 100%;
        }
    body {
            background: linear-gradient(135deg, #0f0f11 0%, #1a1a1e 100%);
            color: #e8e8e8;
            font-family: 'Inter', sans-serif;
            margin: 0;
            padding: 20px;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
            box-sizing: border-box;
        }
    .container {
            display: flex;
            flex: 1;
            overflow: hidden;
        }
    .left {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding-right: 20px;
        }
        .right {
            width: 380px;
            padding-left: 20px;
            background: transparent;
            display: flex;
            flex-direction: column;
        }
        h1 {
            text-align: center;
            margin: 0 0 20px 0;
            color: #e2e9ed;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: 1px;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
            flex-shrink: 0;
        }
        .gauges {
            display: flex;
            justify-content: space-around;
            margin: 20px 0;
            flex-wrap: wrap;
            gap: 10px;
        }
        .gauge-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 220px;
        }
        .gauge-title {
            font-size: 14px;
            font-weight: 500;
            color: #9aa4b2;
            margin-bottom: 10px;
            text-align: center;
        }
        .gauge {
            --size: 160px;
            --cutout: 60%;
            --value: 0;
            --color: #22c55e;
            --background: #101113;
            width: var(--size);
            height: var(--size);
            border-radius: calc(var(--size) / 2);
            background:
                radial-gradient(var(--background) 0 var(--cutout), transparent var(--cutout) 100%),
                conic-gradient(
                    from 0deg,
                    var(--color) calc(360deg * var(--value)),
                    #374151 calc(360deg * var(--value)) 360deg
                );
            text-align: center;
            line-height: var(--size);
            position: relative;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            transition: all 0.5s ease;
        }
        .gauge::after {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: calc(var(--size) * var(--cutout) / 100 * 2);
            height: calc(var(--size) * var(--cutout) / 100 * 2);
            background: var(--background);
            border-radius: 50%;
            transform: translate(-50%, -50%);
            z-index: 1;
        }
        .gauge-value {
            position: relative;
            z-index: 2;
            font-size: 24px;
            font-weight: 700;
            color: #e2e9ed;
            display: block;
        }
        .gauge-unit {
            position: relative;
            z-index: 2;
            font-size: 12px;
            font-weight: 500;
            color: #9aa4b2;
            display: block;
        }
        .plots {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 15px;
            overflow: hidden;
        }
        .plot {
            height: 220px;
            margin: 5px 0;
            background: linear-gradient(145deg, #101113, #0e0f11);
            border-radius: 12px;
            padding: 15px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.05);
            position: relative;
            overflow: hidden;
            flex: 1;
            display: flex;
            align-items: stretch;
        }
        .plot::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, #2f5bff, transparent);
        }
        .status {
            display: flex;
            justify-content: space-around;
            margin: 20px 0;
            flex-wrap: wrap;
            gap: 10px;
        }
        .led {
            padding: 12px 24px;
            border-radius: 16px;
            font-weight: 600;
            text-align: center;
            min-width: 160px;
            margin: 5px;
            color: white;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            transition: all 0.3s ease;
            border: 1px solid rgba(255,255,255,0.1);
            flex-shrink: 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .led:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(0,0,0,0.3);
        }
        .green {
            background: linear-gradient(135deg, #22c55e, #16a34a);
        }
        .orange {
            background: linear-gradient(135deg, #f97316, #ea580c);
        }
        .red {
            background: linear-gradient(135deg, #ef4444, #dc2626);
        }
        .gray {
            background: linear-gradient(135deg, #6b7280, #4b5563);
        }
        .info-card {
            background: linear-gradient(145deg, #15161a, #101113);
            border: 1px solid rgba(42, 45, 52, 0.5);
            border-radius: 18px;
            padding: 24px;
            flex: 1;
            overflow-y: auto;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
            backdrop-filter: blur(10px);
            display: flex;
            flex-direction: column;
        }
        .info-title {
            color: #9aa4b2;
            letter-spacing: 3px;
            font-size: 12px;
            margin-bottom: 12px;
            font-weight: 500;
            text-transform: uppercase;
        }
        .info-h1 {
            color: #e2e9ed;
            font-size: 22px;
            font-weight: 600;
            margin-bottom: 12px;
            word-wrap: break-word;
            overflow-wrap: break-word;
            hyphens: auto;
            letter-spacing: -0.5px;
        }
        .accent {
            height: 4px;
            background: linear-gradient(90deg, #2f5bff, #7c3aed);
            border-radius: 2px;
            margin: 12px 0;
            box-shadow: 0 2px 4px rgba(47, 91, 255, 0.3);
            flex-shrink: 0;
        }
        .info-row {
            display: flex;
            justify-content: space-between;
            margin: 8px 0;
            align-items: center;
            padding: 4px 0;
            flex-shrink: 0;
        }
        .info-row:hover {
            background: rgba(47, 91, 255, 0.05);
            border-radius: 8px;
            padding: 4px 8px;
        }
        .info-key {
            color: #8b93a1;
            font-size: 12px;
            font-weight: 500;
            flex-shrink: 0;
        }
        .info-value {
            color: #eaeef7;
            font-size: 14px;
            font-weight: 600;
            word-wrap: break-word;
            overflow-wrap: break-word;
            hyphens: auto;
            flex: 1;
            text-align: right;
            letter-spacing: 0.2px;
            margin-left: 10px;
        }
        .note {
            color: #c6d0e3;
            font-size: 13px;
            background: rgba(47,91,255,0.08);
            border: 1px solid rgba(47,91,255,0.2);
            border-radius: 12px;
            padding: 12px;
            margin: 12px 0;
            line-height: 1.5;
            box-shadow: inset 0 1px 3px rgba(0,0,0,0.1);
            word-wrap: break-word;
            overflow-wrap: break-word;
            hyphens: auto;
            flex-shrink: 0;
        }
        .fan-status {
            text-align: center;
            margin: 20px 0;
            font-size: 18px;
            font-weight: 600;
            padding: 12px;
            border-radius: 12px;
            background: rgba(0,0,0,0.2);
            transition: all 0.3s ease;
            flex-shrink: 0;
        }
        .fan-status.green {
            background: rgba(34, 197, 94, 0.1);
            border: 1px solid rgba(34, 197, 94, 0.3);
            color: #22c55e;
        }
        .fan-status.red {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
        }
        .badges {
            display: flex;
            gap: 8px;
            margin: 20px 0;
            flex-wrap: wrap;
            flex-shrink: 0;
        }
        .badge {
            color: #a9b2c3;
            font-size: 11px;
            border: 1px solid rgba(42, 45, 52, 0.5);
            border-radius: 12px;
            padding: 6px 12px;
            background: rgba(23, 24, 29, 0.8);
            font-weight: 500;
            transition: all 0.3s ease;
            letter-spacing: 0.5px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
            flex: 1;
            min-width: 0;
        }
        .badge:hover {
            background: rgba(47, 91, 255, 0.1);
            border-color: #2f5bff;
            color: #2f5bff;
            transform: translateY(-1px);
        }
        .image-placeholder {
            width: 100%;
            height: 140px;
            background: linear-gradient(145deg, #2a2d34, #1e2025);
            border-radius: 12px;
            margin-top: auto;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #6b7280;
            font-size: 13px;
            font-weight: 500;
            border: 1px dashed rgba(107, 114, 128, 0.3);
            transition: all 0.3s ease;
            flex-shrink: 0;
            word-wrap: break-word;
            text-align: center;
            padding: 10px;
        }
        .image-placeholder:hover {
            background: linear-gradient(145deg, #2f5bff20, #2a2d34);
            color: #2f5bff;
            border-color: rgba(47, 91, 255, 0.5);
        }

        /* Chart enhancements */
        .plot canvas {
            filter: drop-shadow(0 2px 4px rgba(0,0,0,0.2));
            width: 100% !important;
            height: 100% !important;
        }
    </style>
</head>
<body>
    <h1>Industrial Safety Monitoring Dashboard</h1>
    <div class="container">
        <div class="left">
            <div class="gauges">
                <div class="gauge-container">
                    <div class="gauge-title">Temperature</div>
                    <div class="gauge" id="tempGauge">
                        <span class="gauge-value">0</span>
                        <span class="gauge-unit">°C</span>
                    </div>
                </div>
                <div class="gauge-container">
                    <div class="gauge-title">Gas Level</div>
                    <div class="gauge" id="gasGauge">
                        <span class="gauge-value">0</span>
                        <span class="gauge-unit">%</span>
                    </div>
                </div>
                <div class="gauge-container">
                    <div class="gauge-title">Vibration</div>
                    <div class="gauge" id="vibGauge">
                        <span class="gauge-value">0</span>
                        <span class="gauge-unit">%</span>
                    </div>
                </div>
            </div>
            <div class="status">
                <div class="led green" id="tempLed">Temp LED: GREEN (Safe)</div>
                <div class="led green" id="gasLed">Gas LED: GREEN (Safe)</div>
                <div class="led green" id="buzLed">Buzzer: OFF</div>
                <div class="led red" id="fanLed">Fan: OFF</div>
            </div>
            <div class="plots">
                <div class="plot">
                    <canvas id="tempChart"></canvas>
                </div>
                <div class="plot">
                    <canvas id="gasChart"></canvas>
                </div>
                <div class="plot">
                    <canvas id="vibChart"></canvas>
                </div>
            </div>
        </div>
        <div class="right">
            <div class="info-card">
                <div class="info-title">PROJECT</div>
                <div class="info-h1">%%COURSE%%</div>
                <div class="accent"></div>
                <div class="info-row">
                    <span class="info-key">Presenter</span>
                    <span class="info-value">%%PRESENTER%%</span>
                </div>
                <div class="info-row">
                    <span class="info-key">Reg No</span>
                    <span class="info-value">%%REGNO%%</span>
                </div>
                <div class="info-row">
                    <span class="info-key">Topic</span>
                    <span class="info-value">%%TOPIC%%</span>
                </div>
                <div class="note">%%NOTE%%</div>
                <div class="fan-status" id="webFanStatus">Fan: OFF</div>
                <div class="badges">
                    <span class="badge">Arduino + PyQt5</span>
                    <span class="badge">Serial DAQ</span>
                    <span class="badge">Temp / Gas / Vibration</span>
                </div>
                <div class="image-placeholder">Logo Image</div>
            </div>
        </div>
    </div>
    <script>
        // Charts
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { display: true, color: 'white', grid: { color: '#374151' } },
                y: { beginAtZero: true, color: 'white', grid: { color: '#374151' } }
            },
            plugins: {
                legend: { display: false }
            },
            backgroundColor: '#101113'
        };

        var tempCtx = document.getElementById('tempChart').getContext('2d');
        var tempChart = new Chart(tempCtx, {
            type: 'line',
            data: { labels: [], datasets: [{
                label: 'Temp (°C)',
                data: [],
                borderColor: 'rgb(255, 99, 132)',
                backgroundColor: 'rgba(255, 99, 132, 0.2)',
                tension: 0.1,
                fill: false
            }] },
            options: chartOptions
        });

        var gasCtx = document.getElementById('gasChart').getContext('2d');
        var gasChart = new Chart(gasCtx, {
            type: 'line',
            data: { labels: [], datasets: [{
                label: 'Gas (%)',
                data: [],
                borderColor: 'rgb(54, 162, 235)',
                backgroundColor: 'rgba(54, 162, 235, 0.2)',
                tension: 0.1,
                fill: false
            }] },
            options: chartOptions
        });

        var vibCtx = document.getElementById('vibChart').getContext('2d');
        var vibChart = new Chart(vibCtx, {
            type: 'line',
            data: { labels: [], datasets: [{
                label: 'Vib (%)',
                data: [],
                borderColor: 'rgb(75, 192, 192)',
                backgroundColor: 'rgba(75, 192, 192, 0.2)',
                tension: 0.1,
                fill: false
            }] },
            options: chartOptions
        });

        let tempLabels = [], tempData = [];
        let gasLabels = [], gasData = [];
        let vibLabels = [], vibData = [];

        function getGaugeColor(value, levels, colors) {
            for (let i = 0; i < levels.length; i++) {
                if (value < levels[i]) {
                    return colors[i];
                }
            }
            return colors[colors.length - 1];
        }

        function updateGauge(gaugeId, value, unit, levels, colors) {
            const gauge = document.getElementById(gaugeId);
            const normalizedValue = Math.min(Math.max(value / 100, 0), 1);
            gauge.style.setProperty('--value', normalizedValue);
            const color = getGaugeColor(value, levels, colors);
            gauge.style.setProperty('--color', color);
            gauge.querySelector('.gauge-value').textContent = Math.round(value);
            gauge.querySelector('.gauge-unit').textContent = unit;
        }

        function updateData() {
            fetch('/data')
                .then(response => response.json())
                .then(data => {
                    if (data.error) return;

                    // Update Gauges
                    updateGauge('tempGauge', data.temp_c, '°C', [32, 38, 100], ['#22c55e', '#f97316', '#ef4444']);
                    updateGauge('gasGauge', data.gas_pct, '%', [30, 60, 100], ['#22c55e', '#f97316', '#ef4444']);
                    updateGauge('vibGauge', data.vib_pct, '%', [20, 50, 100], ['#22c55e', '#f97316', '#ef4444']);

                    // Update LEDs
                    // Temp LED
                    let tempText, tempClass;
                    if (data.temp_c < 32) {
                        tempText = 'Temp LED: GREEN (Safe)';
                        tempClass = 'green';
                    } else if (data.temp_c < 38) {
                        tempText = 'Temp LED: ORANGE (Warning)';
                        tempClass = 'orange';
                    } else {
                        tempText = 'Temp LED: RED (Critical)';
                        tempClass = 'red';
                    }
                    document.getElementById('tempLed').textContent = tempText;
                    document.getElementById('tempLed').className = `led ${tempClass}`;

                    // Gas LED
                    let gasText, gasClass;
                    if (data.gas_raw <= 300) {
                        gasText = 'Gas LED: GREEN (Safe)';
                        gasClass = 'green';
                    } else if (data.gas_raw <= 600) {
                        gasText = 'Gas LED: ORANGE (Moderate)';
                        gasClass = 'orange';
                    } else {
                        gasText = 'Gas LED: RED (High)';
                        gasClass = 'red';
                    }
                    document.getElementById('gasLed').textContent = gasText;
                    document.getElementById('gasLed').className = `led ${gasClass}`;

                    // Buzzer
                    let buzText = data.alarm ? 'Buzzer: ON (Alarm)' : 'Buzzer: OFF';
                    let buzClass = data.alarm ? 'red' : 'green';
                    document.getElementById('buzLed').textContent = buzText;
                    document.getElementById('buzLed').className = `led ${buzClass}`;

                    // Fan
                    let fanText = data.fan_on ? 'Fan: ON' : 'Fan: OFF';
                    let fanClass = data.fan_on ? 'green' : 'red';
                    document.getElementById('fanLed').textContent = fanText;
                    document.getElementById('fanLed').className = `led ${fanClass}`;
                    document.getElementById('webFanStatus').textContent = fanText;
                    document.getElementById('webFanStatus').className = `fan-status ${fanClass}`;

                    // Charts
                    let now = new Date(data.timestamp).toLocaleTimeString();
                    [tempLabels, tempData] = updateChart(now, data.temp_c, tempLabels, tempData, tempChart);
                    [gasLabels, gasData] = updateChart(now, data.gas_pct, gasLabels, gasData, gasChart);
                    [vibLabels, vibData] = updateChart(now, data.vib_pct, vibLabels, vibData, vibChart);
                }).catch(err => console.log('Fetch error:', err));
        }

        function updateChart(label, value, labels, data, chart) {
            labels.push(label);
            data.push(value);
            if (labels.length > 50) {
                labels.shift();
                data.shift();
            }
            chart.data.labels = labels;
            chart.data.datasets[0].data = data;
            chart.update('none');
            return [labels, data];
        }

        setInterval(updateData, 1000);
        updateData();
    </script>
</body>
</html>
        """
        # replace only the intended placeholders so CSS braces remain literal
        html = html.replace("%%COURSE%%", info.get('course', ''))
        html = html.replace("%%PRESENTER%%", info.get('presenter', ''))
        html = html.replace("%%REGNO%%", info.get('regno', ''))
        html = html.replace("%%TOPIC%%", info.get('topic', ''))
        html = html.replace("%%NOTE%%", info.get('note', ''))
        return html
    # ---------- Helpers ----------
    def _plot_widget(self, title):
        w = pg.PlotWidget(title=title)
        w.setLabel('bottom', 'Time', 's')
        w.showGrid(x=True, y=True, alpha=0.3)
        return w
    def _set_connected(self, ok):
        sim = self.simChk.isChecked()
        self.startBtn.setEnabled(ok if not sim else False)
        self.stopBtn.setEnabled(False)
        self.saveBtn.setEnabled(True)
        self.clearBtn.setEnabled(True)
        self.connectBtn.setText("Disconnect" if ok else "Connect")
    def _start_ui_timer(self):
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.redraw)
        self.timer.start(200)
    def refresh_ports(self):
        self.portBox.clear()
        for p in serial.tools.list_ports.comports():
            self.portBox.addItem(p.device)
    # ---------- Serial control ----------
    def toggle_connect(self):
        # Disconnect
        if self.ser and self.ser.is_open:
            self.running = False
            QtCore.QThread.msleep(200)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self._set_connected(False)
            return
        # Connect
        if self.portBox.count() == 0:
            QtWidgets.QMessageBox.information(self, "Serial", "No COM ports found.")
            return
        port = self.portBox.currentText()
        # Force baud = 9600 to match Arduino sketch
        baud = 9600
        self.baudBox.setCurrentText("9600")
        try:
            self.ser = serial.Serial(port, baud, timeout=1)
            self._set_connected(True)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Serial Error", str(ex))
    def start_stream(self):
        sim = self.simChk.isChecked()
        if not sim and not (self.ser and self.ser.is_open):
            QtWidgets.QMessageBox.information(
                self, "Serial",
                "Connect to Arduino first or enable Simulate."
            )
            return
        if self.running:
            return
        self.running = True
        self._sim_t0 = time.time()
        self.rx_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.rx_thread.start()
        self.startBtn.setEnabled(False)
        self.stopBtn.setEnabled(True)
    def stop_stream(self):
        self.running = False
        self.startBtn.setEnabled(not self.simChk.isChecked())
        self.stopBtn.setEnabled(False)
    def _on_sim_toggle(self, on: bool):
        if on:
            self.startBtn.setEnabled(False)
            if not self.running:
                self.start_stream()
        else:
            if self.running and not (self.ser and self.ser.is_open):
                self.stop_stream()
            self.startBtn.setEnabled(self.ser is not None and self.ser.is_open)
    # ---------- Email Configuration Dialog ----------
    def email_config_dialog(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Email Alert Configuration (Gmail Recommended)")
        dlg.resize(400, 200)
        layout = QtWidgets.QFormLayout(dlg)
        from_le = QtWidgets.QLineEdit(self.email_config.get('from', ''))
        to_le = QtWidgets.QLineEdit(self.email_config.get('to', ''))
        pass_le = QtWidgets.QLineEdit(self.email_config.get('pass', ''))
        pass_le.setEchoMode(QtWidgets.QLineEdit.Password)
        layout.addRow("Sender Email:", from_le)
        layout.addRow("Recipient Email:", to_le)
        layout.addRow("App Password:", pass_le)
        note = QtWidgets.QLabel("Note: For Gmail, use App Password (not regular password). Enable 2FA and generate at myaccount.google.com/apppasswords")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9ca3af; font-size:10px;")
        layout.addRow(note)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(lambda: self._save_email_config(from_le.text(), to_le.text(), pass_le.text(), dlg))
        btns.rejected.connect(dlg.reject)
        layout.addRow(btns)
        dlg.exec_()
    def _save_email_config(self, fr, to, pw, dlg):
        self.email_config['from'] = fr
        self.email_config['to'] = to
        self.email_config['pass'] = pw
        dlg.accept()
        QtWidgets.QMessageBox.information(self, "Config Saved", "Email configuration updated. Enable 'Email Alerts' to start receiving notifications.")
    # ---------- Send Alert Email ----------
    def send_alert_email(self, t, g_raw, v_raw):
        if not self.alertChk.isChecked():
            return
        if not all([self.email_config['from'], self.email_config['to'], self.email_config['pass']]):
            QtWidgets.QMessageBox.warning(self, "Email Alert", "Please configure email settings first.")
            return
        try:
            from_email = self.email_config['from']
            to_email = self.email_config['to']
            password = self.email_config['pass']
            server = smtplib.SMTP(self.email_config['server'], self.email_config['port'])
            server.starttls()
            server.login(from_email, password)
            msg = MIMEMultipart()
            msg['From'] = from_email
            msg['To'] = to_email
            msg['Subject'] = "🚨 Industrial Safety Alert: Critical Condition Detected"
            body = f"""
Industrial Safety Monitoring System Alert
Timestamp: {datetime.now().isoformat(timespec='seconds')}
Critical Condition Detected:
- Temperature: {t:.1f} °C (Threshold: >= {TEMP_CRITICAL} °C)
- Gas Level: {g_raw:.0f} (raw ADC) (Threshold: >= {GAS_MODERATE_MAX})
- Vibration: {v_raw:.0f} (raw ADC) (Threshold: < {VIB_THRESHOLD})
Immediate action recommended. Buzzer activated and Fan may be running.
Dashboard: Check the connected Python DAQ application for real-time data.
"""
            msg.attach(MIMEText(body, 'plain'))
            text = msg.as_string()
            server.sendmail(from_email, to_email, text)
            server.quit()
            print("[EMAIL] Alert sent successfully.")
        except Exception as e:
            print(f"[EMAIL ERROR] Failed to send alert: {e}")
            QtWidgets.QMessageBox.critical(self, "Email Error", f"Failed to send email alert:\n{str(e)}\n\nCheck configuration and network.")
    # ---------- Reader loop ----------
    def _reader_loop(self):
        """
        Simulation:
            generate fake samples at ~5 Hz.
        Real mode:
            expect CSV lines: tempC,gasRaw,vibLevel
        """
        buffer = bytearray()
        bad = 0
        while self.running:
            try:
                if self.simChk.isChecked():
                    tempC, gas_raw, vib_raw = self._simulate_sample()
                    self._ingest(tempC, gas_raw, vib_raw)
                    time.sleep(0.2)
                    continue
                # Real mode
                if not (self.ser and self.ser.is_open):
                    break
                chunk = self.ser.read(self.ser.in_waiting or 1)
                if not chunk:
                    continue
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    line = line.rstrip(b"\r")
                    txt = line.decode(errors="ignore").strip()
                    if not txt:
                        continue
                    if self.DEBUG_SERIAL:
                        print("RAW:", txt)
                    parts = txt.split(",")
                    if len(parts) != 3:
                        bad += 1
                        if self.DEBUG_SERIAL:
                            print(f"[WARN] bad frame #{bad}: {txt}")
                        continue
                    try:
                        tempC = float(parts[0])
                        gas_raw = float(parts[1])
                        vib_raw = float(parts[2])
                    except ValueError:
                        bad += 1
                        if self.DEBUG_SERIAL:
                            print(f"[WARN] parse error #{bad}: {txt}")
                        continue
                    self._ingest(tempC, gas_raw, vib_raw)
            except Exception as e:
                if self.DEBUG_SERIAL:
                    print("[ERROR] reader:", e)
                time.sleep(0.1)
    # ---------- Simulator ----------
    def _simulate_sample(self):
        t = time.time() - (self._sim_t0 or time.time())
        temp = 30 + 5 * math.sin(t / 15.0) + random.gauss(0, 0.7)
        temp = max(20, min(80, temp))
        gas_raw = 300 + 400 * (math.sin(t / 20.0) + 1) / 2 + random.gauss(0, 20)
        gas_raw = max(0, min(1023, gas_raw))
        # Simulated vibration "level" (0–1023, higher = more vibration)
        vib_raw = 100 + 600 * abs(math.sin(t / 5.0)) + random.gauss(0, 30)
        vib_raw = max(0, min(1023, vib_raw))
        return temp, gas_raw, vib_raw
    # ---------- Data ingest ----------
    def _ingest(self, tempC, gas_raw, vib_raw):
        ts = time.time()
        # sanity checks on temp
        if tempC < 0 or tempC > 80:
            if self.temp:
                tempC = self.temp[-1]
            else:
                tempC = 31.0
        if self.temp:
            if abs(tempC - self.temp[-1]) > 15.0:
                tempC = self.temp[-1]
        # --- Gas as before (0–1023 → 0–100 %) ---
        gas_clamped = max(0.0, min(MAX_ADC, float(gas_raw)))
        gas_norm = gas_clamped / MAX_ADC
        gas_pct = 100.0 * gas_norm
        # --- Vibration: Arduino sends raw ADC where calm ≈1023, vibration → lower ---
        vib_raw_clamped = max(0.0, min(MAX_ADC, float(vib_raw)))
        # Convert so: calm ≈0 %, strong vibration ≈100 %
        vib_level = MAX_ADC - vib_raw_clamped
        vib_norm = vib_level / MAX_ADC
        vib_pct = 100.0 * vib_norm
        # Smooth
        gas_pct = self.ema_gas.push(gas_pct)
        vib_pct = self.ema_vib.push(vib_pct)
        # Store time-series
        self.ts.append(ts)
        self.temp.append(float(tempC))
        self.gas.append(float(gas_pct))
        self.vib.append(float(vib_pct))
        self.gas_raw.append(float(gas_raw))
        self.vib_raw.append(float(vib_raw)) # keep the original ADC for reference
        # For CSV log we save both raw ADC and derived vibration_level
        self.rows.append({
            "datetime": datetime.now().isoformat(timespec="seconds"),
            "temp_c": tempC,
            "gas_raw": gas_raw,
            "gas_pct": gas_pct,
            "vibration_adc_raw": vib_raw,
            "vibration_level": vib_level,
            "vibration_pct": vib_pct
        })
    # ---------- UI redraw ----------
    def _set_led_style(self, label: QtWidgets.QLabel, color: str, text: str):
        label.setText(text)
        label.setStyleSheet(f"""
            background:{color};
            color:#ffffff;
            font-weight:bold;
            border-radius:12px;
            padding:4px 10px;
        """)
    def redraw(self):
        if not self.ts:
            return
        # Latest values
        t = self.temp[-1]
        g_pct = self.gas[-1]
        v_pct = self.vib[-1]
        g_raw = self.gas_raw[-1] if self.gas_raw else None
        v_raw = self.vib_raw[-1] if self.vib_raw else None
        # Arduino buzzer condition:
        # tempC >= TEMP_CRITICAL OR gasRaw >= GAS_MODERATE_MAX OR vibRaw < VIB_THRESHOLD
        alarm = (
            (t >= TEMP_CRITICAL) or
            (g_raw is not None and g_raw >= GAS_MODERATE_MAX) or
            (v_raw is not None and v_raw < VIB_THRESHOLD)
        )
        # ---------- Email Alert (send only on state change to critical) ----------
        if alarm and not self.prev_alarm:
            self.send_alert_email(t, g_raw, v_raw)
        self.prev_alarm = alarm
        # ---------- Fan logic (mirror Arduino) ----------
        if (t >= TEMP_RELAY_ON or (g_raw is not None and g_raw > GAS_MODERATE_MAX)) and not self.fan_on:
            self.fan_on = True
        elif (t <= TEMP_RELAY_OFF and (g_raw is None or g_raw <= GAS_MODERATE_MAX)) and self.fan_on:
            self.fan_on = False
        if self.fan_on:
            self.fanLabel.setText("Fan: ON")
            self.fanLabel.setStyleSheet("color:#4ade80; font-weight:bold;")
        else:
            self.fanLabel.setText("Fan: OFF")
            self.fanLabel.setStyleSheet("color:#f97373; font-weight:bold;")
        self.infoPanel.setFanRunning(self.fan_on)
        # ---------- Temp LED indicator ----------
        if t < TEMP_WARN:
            self._set_led_style(self.tempLedLabel, "#22c55e", "Temp LED: GREEN (Safe)")
        elif t < TEMP_CRITICAL:
            self._set_led_style(self.tempLedLabel, "#f97316", "Temp LED: ORANGE (Warning)")
        else:
            self._set_led_style(self.tempLedLabel, "#ef4444", "Temp LED: RED (Critical)")
        # ---------- Gas LED indicator (RAW thresholds) ----------
        if g_raw is not None:
            if g_raw <= GAS_SAFE_MAX:
                self._set_led_style(self.gasLedLabel, "#22c55e", "Gas LED: GREEN (Safe)")
            elif g_raw <= GAS_MODERATE_MAX:
                self._set_led_style(self.gasLedLabel, "#f97316", "Gas LED: ORANGE (Moderate)")
            else:
                self._set_led_style(self.gasLedLabel, "#ef4444", "Gas LED: RED (High)")
        else:
            self._set_led_style(self.gasLedLabel, "#6b7280", "Gas LED: ---")
        # ---------- Update gauges ----------
        self.gTemp.setValue(max(0, min(100, t)))
        self.gGas.setValue(max(0, min(100, g_pct)))
        self.gVib.setValue(max(0, min(100, v_pct)))
        # ---------- Buzzer label (no animation) ----------
        if alarm:
            self.buzzerLabel.setText("Buzzer: ON (Alarm)")
            self.buzzerLabel.setStyleSheet("""
                background:#ef4444;
                color:#ffffff;
                font-weight:bold;
                border-radius:12px;
                padding:4px 10px;
            """)
        else:
            self.buzzerLabel.setText("Buzzer: OFF")
            self.buzzerLabel.setStyleSheet("""
                background:#22c55e;
                color:#ffffff;
                font-weight:bold;
                border-radius:12px;
                padding:4px 10px;
            """)
        # ---------- Highlight most critical gauge ----------
        vals = [
            (self.gTemp, t, 100),
            (self.gGas, g_pct, 100),
            (self.gVib, v_pct, 100),
        ]
        best = -1.0
        idx = -1
        for i, (gauge, val, mx) in enumerate(vals):
            norm = val / mx if mx != 0 else 0
            if norm > best:
                best, idx = norm, i
        for i, (gauge, _, __) in enumerate(vals):
            gauge.setHighlight(i == idx)
        # ---------- Update plots ----------
        t0 = self.ts[0]
        x = [s - t0 for s in self.ts[-600:]]
        def last(a):
            a = a[-600:]
            return [float(v) for v in a]
        if x:
            xmax = x[-1]
            xmin = max(0, xmax - 60)
            self.pTemp.setXRange(xmin, max(10, xmax))
            self.pGas.setXRange(xmin, max(10, xmax))
            self.pVib.setXRange(xmin, max(10, xmax))
        self.cTemp.setData(x, last(self.temp))
        self.cGas.setData(x, last(self.gas))
        self.cVib.setData(x, last(self.vib))
    # ---------- Actions ----------
    def save_csv(self):
        if not self.rows:
            QtWidgets.QMessageBox.information(self, "Save", "No data to save yet.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save CSV", "industrial_safety_log.csv", "CSV Files (*.csv)")
        if not path:
            return
        pd.DataFrame(self.rows).to_csv(path, index=False)
        QtWidgets.QMessageBox.information(self, "Saved", f"Saved to:\n{path}")
    def show_data_popup(self):
        """Show logged CSV data (real values) in a popup side window."""
        if not self.rows:
            QtWidgets.QMessageBox.information(self, "Data", "No data available yet.")
            return
        # Use last 200 samples to avoid a huge table
        rows = self.rows[-200:]
        # Column order – matches what you log in self.rows
        headers = [
            "datetime",
            "temp_c",
            "gas_raw",
            "gas_pct",
            "vibration_adc_raw",
            "vibration_level",
            "vibration_pct",
        ]
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Logged Data (Latest Samples)")
        dlg.resize(900, 500)
        layout = QtWidgets.QVBoxLayout(dlg)
        table = QtWidgets.QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        # ---- Make header clearly visible on dark theme ----
        header = table.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #1f2933;
                color: #e5e7eb;
                font-weight: bold;
                padding: 4px;
                border: 1px solid #4b5563;
            }
        """)
        header.setStretchLastSection(True)
        for r, row in enumerate(rows):
            for c, key in enumerate(headers):
                val = row.get(key, "")
                if isinstance(val, float):
                    txt = f"{val:.2f}"
                else:
                    txt = str(val)
                item = QtWidgets.QTableWidgetItem(txt)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        btnClose = QtWidgets.QPushButton("Close")
        btnClose.clicked.connect(dlg.accept)
        btnLayout = QtWidgets.QHBoxLayout()
        btnLayout.addStretch(1)
        btnLayout.addWidget(btnClose)
        layout.addLayout(btnLayout)
        dlg.exec_()
    def clear_all(self):
        self.ts.clear()
        self.temp.clear()
        self.gas.clear()
        self.vib.clear()
        self.gas_raw.clear()
        self.vib_raw.clear()
        self.rows.clear()
        self.prev_alarm = False
# ---------- main ----------
def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    pg.setConfigOptions(background='#101113', foreground='w')
    w = DAQApp()
    w.show()
    sys.exit(app.exec_())
if __name__ == "__main__":
    main()