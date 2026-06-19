#!/usr/bin/env python3
"""
TCI IQ Viewer -- live FFT + waterfall over a SunSDR2's IQ stream.

Connects to the IQ sidecar's ZMQ PUB endpoint (default
``tcp://localhost:5555``) and to rigctld's CAT port (default 4532) so
the X axis tracks the radio's actual dial frequency, and clicking on
the FFT or waterfall retunes the radio.

This is the user-facing companion to ``tci-iq-sidecar.py``.  It runs
as a single Python file; no install, no GR companion .grc to compile.

Run on the same host as the IQ sidecar (loopback ZMQ + rigctld).  For
remote use, an SSH tunnel works:

    ssh -L 5555:localhost:5555 -L 4532:localhost:4532 radio-host

Then point this viewer at the local ports.

Dependencies (Arch / Linux Mint / Ubuntu equivalents):
    - python (3.9+)
    - python-gnuradio  (or python3-gnuradio)
    - python-pyqt5     (or python3-pyqt5)
    - python-pyzmq     (only used transitively by GR's gr-zeromq;
                        viewer itself doesn't import zmq)

Limitations
    - Click-to-tune is approximate near the edges of the trace.  GR's
      freq_sink/waterfall widgets don't expose a public click signal
      with frequency mapping in 3.10, so we compute the mapping
      ourselves from widget width.  Clicks near the trace center are
      accurate to tens of Hz; clicks near the labelled edges can be
      off by 1-2 kHz because the QwtPlot canvas has invisible margins.
"""
import argparse
import socket
import sys
import threading
import time

# Pick whichever Qt binding GR was built against.  GR 3.10 on most
# distros uses PyQt5; some newer builds use PyQt6.
try:
    from PyQt5 import Qt
    QT_FLAVOR = "PyQt5"
except ImportError:
    try:
        from PyQt6 import QtCore as Qt  # type: ignore
        QT_FLAVOR = "PyQt6"
    except ImportError:
        sys.stderr.write(
            "ERROR: neither PyQt5 nor PyQt6 is installed.  Install the\n"
            "package that matches your GNU Radio build (most distros\n"
            "ship PyQt5 with GR 3.10).\n")
        sys.exit(1)

import sip
from gnuradio import gr, blocks, zeromq, qtgui
from gnuradio import fft as gr_fft


# -------------------------------------------------------------------------
# CAT bridge: a thin client that polls rigctld for the current
# frequency and exposes a method to retune.  Survives a sidecar
# restart by reopening the socket on every failure.
# -------------------------------------------------------------------------

class CATBridge:
    """Threadsafe small wrapper around a rigctld TCP connection.

    Two responsibilities:
      - poll() returns the current dial frequency in Hz, or None on
        any failure (caller treats as "stay at last known")
      - retune(hz) sends 'F <hz>\\n' and consumes the reply

    Both methods serialise on a lock so the polling thread and the
    UI thread don't step on each other on the same socket.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._sock = None
        self._lock = threading.Lock()

    def _ensure_open(self):
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect((self.host, self.port))
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            raise
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
                self._ensure_open()
                self._sock.sendall((cmd + "\n").encode())
                data = b""
                while not data.endswith(b"\n"):
                    chunk = self._sock.recv(256)
                    if not chunk:
                        raise OSError("EOF")
                    data += chunk
                    if len(data) > 4096:
                        break
                return data.decode(errors="replace").strip()
            except OSError:
                self._close()
                return None

    def poll(self):
        """Return current dial frequency in Hz, or None on failure."""
        reply = self._send_recv("f")
        if reply is None:
            return None
        try:
            return int(reply.splitlines()[0])
        except (ValueError, IndexError):
            return None

    def retune(self, hz):
        """Send 'F <hz>'.  Returns True on RPRT 0, False otherwise."""
        reply = self._send_recv(f"F {int(hz)}")
        return reply is not None and reply.strip().endswith("0")


# -------------------------------------------------------------------------
# Event filter: catches mouse-press on the FFT/waterfall widget and
# converts the x coordinate into a frequency, then asks the CAT
# bridge to retune.  Approximate: the trace canvas is smaller than
# the host widget by some unknown axis-label margin.  Good enough for
# casual exploration; off by a kHz or two near the edges.
# -------------------------------------------------------------------------

class ClickToTuneFilter(Qt.QObject):
    def __init__(self, widget, get_center_span_fn, retune_fn, parent=None):
        super().__init__(parent)
        self._widget = widget
        self._get_center_span = get_center_span_fn
        self._retune = retune_fn
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        # MouseButtonPress = 2 in Qt5 enum; same value works under PyQt6
        if event.type() == Qt.QEvent.MouseButtonPress:
            try:
                if event.button() != Qt.Qt.LeftButton:
                    return False
                x = event.pos().x()
                w = self._widget.width()
                if w <= 0:
                    return False
                center, span = self._get_center_span()
                # Approximate: the canvas spans most of the widget
                # width, with axis labels eating ~10% on each side.
                # Cropping the click to [10%, 90%] then linearly
                # mapping to [center - span/2, center + span/2] is
                # close enough for visual targets.
                margin = 0.10
                lo, hi = w * margin, w * (1.0 - margin)
                if x < lo:
                    x = lo
                elif x > hi:
                    x = hi
                rel = (x - lo) / (hi - lo)        # 0..1
                hz = center - span / 2 + rel * span
                self._retune(hz)
                return True
            except Exception:
                return False
        return False


# -------------------------------------------------------------------------
# Main flowgraph + window
# -------------------------------------------------------------------------

class IQViewer(gr.top_block, Qt.QWidget):

    def __init__(self, args):
        gr.top_block.__init__(self, "TCI IQ Viewer")
        Qt.QWidget.__init__(self)

        self.args = args
        self.cat = CATBridge(args.cat_host, args.cat_port)
        self.center_freq = args.freq  # initial guess; updated by polling
        self.sample_rate = args.rate

        self.setWindowTitle("TCI IQ Viewer")
        layout = Qt.QVBoxLayout(self)

        # Status bar at the top
        self.status_label = Qt.QLabel("connecting...")
        self.status_label.setStyleSheet(
            "QLabel { font-family: monospace; "
            "padding: 4px; background: #222; color: #ddd; }")
        layout.addWidget(self.status_label)

        # ZMQ source: complex float32 from the IQ sidecar
        self.zmq_src = zeromq.sub_source(
            itemsize=gr.sizeof_gr_complex,
            vlen=1,
            address=args.zmq,
            timeout=100,
            pass_tags=False,
            hwm=64,
        )

        # FFT trace
        self.freq_sink = qtgui.freq_sink_c(
            args.fft_size,
            gr_fft.window.WIN_BLACKMAN_hARRIS,
            self.center_freq,
            self.sample_rate,
            "FFT",
            1, None,
        )
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(args.fft_ymin, args.fft_ymax)
        self.freq_sink.set_y_label("dB", "")
        self.freq_sink.enable_autoscale(args.fft_autoscale)
        self.freq_sink.enable_grid(True)
        self.freq_sink.set_fft_average(args.fft_average)
        self.freq_sink.set_fft_window_normalized(False)
        self.freq_widget = sip.wrapinstance(
            self.freq_sink.qwidget(), Qt.QWidget)
        layout.addWidget(self.freq_widget, stretch=1)

        # Waterfall
        self.wf_sink = qtgui.waterfall_sink_c(
            args.fft_size,
            gr_fft.window.WIN_BLACKMAN_hARRIS,
            self.center_freq,
            self.sample_rate,
            "Waterfall",
            1, None,
        )
        self.wf_sink.set_update_time(0.10)
        self.wf_sink.enable_grid(True)
        self.wf_sink.set_intensity_range(args.wf_min, args.wf_max)
        self.wf_widget = sip.wrapinstance(
            self.wf_sink.qwidget(), Qt.QWidget)
        layout.addWidget(self.wf_widget, stretch=2)

        # Wire everything together
        self.connect(self.zmq_src, self.freq_sink)
        self.connect(self.zmq_src, self.wf_sink)

        # Click-to-tune on both widgets
        self._freq_filter = ClickToTuneFilter(
            self.freq_widget,
            lambda: (self.center_freq, self.sample_rate),
            self._user_retune)
        self._wf_filter = ClickToTuneFilter(
            self.wf_widget,
            lambda: (self.center_freq, self.sample_rate),
            self._user_retune)

        # CAT polling timer: keep the X-axis labels accurate.
        self._poll_timer = Qt.QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cat)
        self._poll_timer.start(int(args.cat_poll_ms))

        self.resize(900, 700)

    # ----- CAT polling / retuning -----

    def _user_retune(self, hz):
        ok = self.cat.retune(hz)
        if ok:
            self.center_freq = hz
            self.freq_sink.set_frequency_range(hz, self.sample_rate)
            self.wf_sink.set_frequency_range(hz, self.sample_rate)
            self._set_status(zmq_ok=True, cat_ok=True, freq_hz=hz,
                             extra=f"retuned to {hz/1e6:.6f} MHz")
        else:
            self._set_status(zmq_ok=True, cat_ok=False,
                             freq_hz=self.center_freq,
                             extra=f"retune to {hz/1e6:.6f} MHz FAILED")

    def _poll_cat(self):
        hz = self.cat.poll()
        if hz is None:
            self._set_status(zmq_ok=True, cat_ok=False,
                             freq_hz=self.center_freq)
            return
        if hz != self.center_freq:
            self.center_freq = hz
            self.freq_sink.set_frequency_range(hz, self.sample_rate)
            self.wf_sink.set_frequency_range(hz, self.sample_rate)
        self._set_status(zmq_ok=True, cat_ok=True, freq_hz=hz)

    def _set_status(self, zmq_ok, cat_ok, freq_hz, extra=""):
        zmq_t = ("● zmq" if zmq_ok else "○ zmq")  # filled vs hollow
        cat_t = ("● cat" if cat_ok else "○ cat")
        msg = (f"{zmq_t}   {cat_t}   "
               f"freq: {freq_hz/1e6:.6f} MHz   "
               f"rate: {self.sample_rate/1000:.0f} kHz")
        if extra:
            msg += f"   |   {extra}"
        self.status_label.setText(msg)


# -------------------------------------------------------------------------
# main
# -------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument('--zmq', default='tcp://localhost:5555',
                   help="IQ sidecar ZMQ PUB endpoint (default %(default)s)")
    p.add_argument('--cat-host', default='localhost',
                   help="rigctld host for CAT polling (default %(default)s)")
    p.add_argument('--cat-port', type=int, default=4532,
                   help="rigctld port (default %(default)s)")
    p.add_argument('--cat-poll-ms', type=int, default=500,
                   help="CAT poll interval, ms (default %(default)s)")
    p.add_argument('--rate', type=int, default=192000,
                   help="IQ sample rate Hz (must match sidecar; default %(default)s)")
    p.add_argument('--freq', type=float, default=14_078_000.0,
                   help="Initial center freq for labels until CAT replies "
                        "(default %(default)s)")
    p.add_argument('--fft-size', type=int, default=1024,
                   help="FFT bins (default %(default)s)")
    p.add_argument('--fft-ymin', type=float, default=-130,
                   help="FFT Y-axis min dB.  ExpertSDR3 IQ noise floor "
                        "sits around -125 dB; -130 is one division of "
                        "headroom below.  Default %(default)s")
    p.add_argument('--fft-ymax', type=float, default=-40,
                   help="FFT Y-axis max dB.  Strong signals on HF rarely "
                        "exceed -50 dB on the IQ stream.  Default "
                        "%(default)s")
    p.add_argument('--fft-autoscale', action='store_true',
                   help="Let GR auto-fit the FFT Y axis instead of using "
                        "--fft-ymin/--fft-ymax")
    p.add_argument('--fft-average', type=float, default=0.2,
                   help="FFT smoothing 0..1 (default %(default)s)")
    p.add_argument('--wf-min', type=float, default=-125,
                   help="Waterfall intensity min dB.  Set to a few dB "
                        "below the typical noise floor so the noise "
                        "shows as dark and signals stand out.  Default "
                        "%(default)s")
    p.add_argument('--wf-max', type=float, default=-60,
                   help="Waterfall intensity max dB.  Default %(default)s")
    args = p.parse_args()

    qapp = Qt.QApplication(sys.argv)
    fg = IQViewer(args)
    fg.start()
    fg.show()

    # Clean shutdown.  GR's top_block runs on background threads; we
    # have to stop+wait BEFORE Qt destroys the widget tree, otherwise
    # those threads race with Python finalisation and segfault.  See
    # the matching comment in tci-gr-test/qt_iq_waterfall.py.
    def stop_flowgraph():
        try:
            fg.stop()
            fg.wait()
        except Exception:
            pass

    qapp.aboutToQuit.connect(stop_flowgraph)
    import signal as _sig
    _sig.signal(_sig.SIGINT, lambda *_: Qt.QApplication.quit())

    # Without this idle timer, Python signal handlers don't run while
    # Qt's native event loop is blocked.
    nudge = Qt.QTimer()
    nudge.start(500)
    nudge.timeout.connect(lambda: None)

    qapp.exec_()
    del fg


if __name__ == '__main__':
    main()
