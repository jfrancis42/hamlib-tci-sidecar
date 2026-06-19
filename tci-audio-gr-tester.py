#!/usr/bin/env python3
"""
GNU Radio test app for the audio-GR sidecar.

What it does:

- **RX side**: subscribes to ``tcp://localhost:5557`` (the audio-GR
  sidecar's RX PUB endpoint), shows a live audio FFT (0--4 kHz) and a
  time-domain waveform.  Confirms received audio is reaching GR.
- **TX side**: generates a 1 kHz sine wave, gates it with a PTT
  toggle, throttles to 8 kHz, pushes to ``tcp://localhost:5558`` (the
  sidecar's TX PULL endpoint).  Demonstrates the round-trip from GR
  through the sidecar to the radio.
- **PTT button**: sends ``T 1`` / ``T 0`` to rigctld over CAT (port
  4532).  When PTT is on, the tone is unmuted and flows out the
  antenna.

Run on the same host as the audio-GR sidecar (loopback ZMQ + rigctld).

Pre-requisites:

    sudo pacman -S gnuradio python-gnuradio gnuradio-companion python-pyqt5
    /tmp/tci.sh start          # with AUDIO_BACKEND=gr in tci.sh

Then:

    python3 tci-audio-gr-tester.py
"""
import argparse
import signal
import socket
import sys
import threading

try:
    from PyQt5 import Qt
except ImportError:
    sys.stderr.write(
        "ERROR: PyQt5 not installed.  Install python-pyqt5 (Arch) or "
        "python3-pyqt5 (Debian) or equivalent.\n")
    sys.exit(1)

import sip
from gnuradio import gr, blocks, analog, zeromq, qtgui
from gnuradio import fft as gr_fft


SAMPLE_RATE = 8000          # must match the sidecar
TONE_HZ     = 1000          # 1 kHz audio tone -> RF appears at dial+1 kHz in USB
DEFAULT_VOL = 0.5           # full-scale = 1.0; 0.5 is conservative


# -------------------------------------------------------------------------
# Tiny CAT bridge for PTT
# -------------------------------------------------------------------------

class CATBridge:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._sock = None

    def _open(self):
        s = socket.socket()
        s.settimeout(1.0)
        s.connect((self.host, self.port))
        self._sock = s

    def _close(self):
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def _send_recv(self, cmd):
        with self._lock:
            try:
                if self._sock is None:
                    self._open()
                self._sock.sendall((cmd + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = self._sock.recv(64)
                    if not chunk:
                        raise OSError("EOF")
                    buf += chunk
                    if len(buf) > 256:
                        break
                return buf.decode(errors="replace").strip()
            except OSError:
                self._close()
                return None

    def set_ptt(self, on):
        return self._send_recv("T 1" if on else "T 0")


# -------------------------------------------------------------------------
# Flowgraph + window
# -------------------------------------------------------------------------

class Tester(gr.top_block, Qt.QWidget):

    def __init__(self, args):
        gr.top_block.__init__(self, "TCI audio GR tester")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("TCI audio GR tester")
        self.args = args
        self.cat = CATBridge(args.cat_host, args.cat_port)
        self.ptt_on = False

        layout = Qt.QVBoxLayout(self)

        # ---- top control bar ----
        ctrl = Qt.QHBoxLayout()
        layout.addLayout(ctrl)

        self.ptt_btn = Qt.QPushButton("PTT  (off)")
        self.ptt_btn.setCheckable(True)
        self.ptt_btn.setStyleSheet(
            "QPushButton { font-size: 16px; padding: 8px 16px; }"
            "QPushButton:checked { background: #c62828; color: white; }")
        self.ptt_btn.toggled.connect(self._on_ptt_toggle)
        ctrl.addWidget(self.ptt_btn)

        ctrl.addWidget(Qt.QLabel("TX volume:"))
        self.vol_slider = Qt.QSlider(Qt.Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(int(DEFAULT_VOL * 100))
        self.vol_slider.valueChanged.connect(self._on_vol_change)
        ctrl.addWidget(self.vol_slider)

        self.vol_label = Qt.QLabel(f"{int(DEFAULT_VOL * 100)}%")
        ctrl.addWidget(self.vol_label)
        ctrl.addStretch(1)

        self.status_label = Qt.QLabel("starting...")
        self.status_label.setStyleSheet(
            "QLabel { font-family: monospace; padding: 4px; "
            "background: #222; color: #ddd; }")
        layout.addWidget(self.status_label)

        # ---- RX path ----
        # ZMQ SUB float32 mono 8 kHz -> FFT + waveform
        self.zmq_rx = zeromq.sub_source(
            itemsize=gr.sizeof_float,
            vlen=1,
            address=args.zmq_rx,
            timeout=100,
            pass_tags=False,
            hwm=64,
        )

        self.rx_freq_sink = qtgui.freq_sink_f(
            512,                                     # FFT size
            gr_fft.window.WIN_BLACKMAN_hARRIS,
            0,                                       # baseband, no center freq
            SAMPLE_RATE,
            "RX audio FFT (0..4 kHz)",
            1, None,
        )
        self.rx_freq_sink.set_update_time(0.10)
        self.rx_freq_sink.set_y_axis(-100, 0)
        self.rx_freq_sink.set_y_label("dB", "")
        self.rx_freq_sink.enable_autoscale(False)
        self.rx_freq_sink.enable_grid(True)
        self.rx_freq_sink.set_fft_average(0.2)
        rx_freq_w = sip.wrapinstance(self.rx_freq_sink.qwidget(), Qt.QWidget)
        layout.addWidget(rx_freq_w, stretch=2)

        self.rx_time_sink = qtgui.time_sink_f(
            1024, SAMPLE_RATE, "RX audio waveform", 1, None)
        self.rx_time_sink.set_update_time(0.10)
        self.rx_time_sink.set_y_axis(-1.0, 1.0)
        self.rx_time_sink.enable_grid(True)
        self.rx_time_sink.enable_autoscale(False)
        rx_time_w = sip.wrapinstance(self.rx_time_sink.qwidget(), Qt.QWidget)
        layout.addWidget(rx_time_w, stretch=1)

        # Connect RX path to BOTH visualisations
        self.connect(self.zmq_rx, self.rx_freq_sink)
        self.connect(self.zmq_rx, self.rx_time_sink)

        # ---- TX path ----
        # 1 kHz tone -> volume gate -> throttle -> ZMQ PUSH
        self.tx_tone = analog.sig_source_f(
            SAMPLE_RATE, analog.GR_SIN_WAVE, TONE_HZ, 1.0, 0.0, 0)
        self.tx_vol = blocks.multiply_const_ff(0.0)   # gated by PTT
        self._set_user_volume(DEFAULT_VOL)
        self.tx_throttle = blocks.throttle(gr.sizeof_float, SAMPLE_RATE,
                                            ignore_tags=True)
        self.zmq_tx = zeromq.push_sink(
            itemsize=gr.sizeof_float,
            vlen=1,
            address=args.zmq_tx,
            timeout=100,
            pass_tags=False,
            hwm=64,
        )

        # Show what we're sending in another time sink
        self.tx_time_sink = qtgui.time_sink_f(
            1024, SAMPLE_RATE, "TX audio (gated by PTT)", 1, None)
        self.tx_time_sink.set_update_time(0.10)
        self.tx_time_sink.set_y_axis(-1.0, 1.0)
        self.tx_time_sink.enable_grid(True)
        self.tx_time_sink.enable_autoscale(False)
        tx_time_w = sip.wrapinstance(self.tx_time_sink.qwidget(), Qt.QWidget)
        layout.addWidget(tx_time_w, stretch=1)

        # tone -> volume -> throttle -> [zmq, time]
        self.connect(self.tx_tone, self.tx_vol, self.tx_throttle)
        self.connect(self.tx_throttle, self.zmq_tx)
        self.connect(self.tx_throttle, self.tx_time_sink)

        self.resize(1000, 800)
        self._update_status()

    # ----- TX volume / PTT -----

    def _set_user_volume(self, vol):
        """Stash the user's intended volume.  Effective volume is 0
        when PTT is off, vol when PTT is on."""
        self._user_vol = vol
        self._apply_vol()

    def _apply_vol(self):
        eff = self._user_vol if self.ptt_on else 0.0
        self.tx_vol.set_k(eff)

    def _on_vol_change(self, v):
        vol = v / 100.0
        self._set_user_volume(vol)
        self.vol_label.setText(f"{v}%")
        self._update_status()

    def _on_ptt_toggle(self, checked):
        # Send CAT first so the radio knows before we start sending
        # tone.  The sidecar's PTT-edge buffer flush ensures any
        # pre-PTT audio that already arrived is dropped.
        ok = self.cat.set_ptt(checked)
        if ok is None:
            self.ptt_btn.setChecked(False)
            self.ptt_on = False
            self._apply_vol()
            self._update_status(extra="CAT unreachable; PTT not set")
            return
        self.ptt_on = checked
        self.ptt_btn.setText(f"PTT  ({'ON' if checked else 'off'})")
        self._apply_vol()
        self._update_status(
            extra=f"PTT -> {'ON' if checked else 'OFF'}; CAT reply: {ok}")

    def _update_status(self, extra=""):
        s = (f"RX: {self.args.zmq_rx}    "
             f"TX: {self.args.zmq_tx}    "
             f"vol: {int(self._user_vol * 100)}%    "
             f"PTT: {'ON' if self.ptt_on else 'off'}")
        if extra:
            s += f"   |   {extra}"
        self.status_label.setText(s)


# -------------------------------------------------------------------------
# main
# -------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument('--zmq-rx', default='tcp://localhost:5557',
                   help="audio-GR sidecar RX PUB endpoint "
                        "(default %(default)s)")
    p.add_argument('--zmq-tx', default='tcp://localhost:5558',
                   help="audio-GR sidecar TX PULL endpoint "
                        "(default %(default)s)")
    p.add_argument('--cat-host', default='localhost')
    p.add_argument('--cat-port', type=int, default=4532)
    args = p.parse_args()

    qapp = Qt.QApplication(sys.argv)
    fg = Tester(args)
    fg.start()
    fg.show()

    # Drop PTT on shutdown -- safety reflex.  Then stop the flowgraph
    # before Qt finishes destroying the widget tree (the matching
    # comment in tci-iq-viewer.py explains why).
    def cleanup():
        try:
            fg.cat.set_ptt(False)
        except Exception:
            pass
        try:
            fg.stop()
            fg.wait()
        except Exception:
            pass

    qapp.aboutToQuit.connect(cleanup)
    signal.signal(signal.SIGINT, lambda *_: Qt.QApplication.quit())

    nudge = Qt.QTimer()
    nudge.start(500)
    nudge.timeout.connect(lambda: None)

    qapp.exec_()
    del fg


if __name__ == '__main__':
    main()
