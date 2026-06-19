#!/usr/bin/env python3
"""
TCI IQ Sidecar for Hamlib rigctld.

Architecture
------------

ExpertSDR3 ──TCI WebSocket──> rigctld ──binary TCI frames──> this sidecar
                              (CAT)        (TCP :4535)            │
                                                                  │ ZMQ PUB
                                                                  ▼
                                                          GNU Radio (or any
                                                          ZMQ subscriber)
                                                          consumes complex
                                                          float32 IQ samples

Wire format on the rigctld<->sidecar TCP socket
-----------------------------------------------

Same length-framed binary TCI protocol used by the audio sidechannel.
This sidecar only handles ``stream_type == STREAM_IQ (= 0)`` frames;
anything else is silently dropped.  See ``PROTOCOL.md`` for the full
header layout.

ZMQ output
----------

The sidecar runs a ZMQ PUB socket bound to a TCP endpoint (default
``tcp://*:5555``).  Every received TCI IQ frame is decoded to
``complex float32`` (interleaved ``re,im,re,im,...``) and published
as a single ZMQ message.

GNU Radio consumes this directly via the stock ``zmq_sub_source``
block from ``gr-zeromq``: set ``Address`` to ``tcp://HOST:5555`` and
``Type`` to ``complex float``.

ZMQ PUB is lossy under backpressure -- if a subscriber falls behind,
old messages are dropped on the publisher side.  This is the right
behaviour for a live SDR feed: we'd rather lose old samples than
stall the radio.
"""
import argparse
import numpy as np
import signal
import socket
import struct
import sys
import threading
import time

import zmq


# Stream types -- must match TCI_STREAM_* in Hamlib rigs/dummy/tci2.c.
STREAM_IQ        = 0
STREAM_RX_AUDIO  = 1
STREAM_TX_AUDIO  = 2
STREAM_TX_CHRONO = 3
STREAM_PTT_STATE = 4

# Sample-format codes from the TCI header word [2].
FORMAT_INT16   = 0
FORMAT_INT24   = 1
FORMAT_INT32   = 2
FORMAT_FLOAT32 = 3

SAMPLE_BYTES = {
    FORMAT_INT16:   2,
    FORMAT_INT24:   3,
    FORMAT_INT32:   4,
    FORMAT_FLOAT32: 4,
}

HEADER_LEN = 64
HEADER_FMT = '<16I'  # 16 little-endian uint32 words


def parse_header(buf):
    """Parse a 64-byte TCI header.  Returns dict, or None on bad header."""
    if len(buf) < HEADER_LEN:
        return None
    h = struct.unpack(HEADER_FMT, buf[:HEADER_LEN])
    return {
        'receiver':    h[0],
        'sample_rate': h[1],
        'format':      h[2],
        'codec':       h[3],
        'crc':         h[4],
        'length':      h[5],
        'stream_type': h[6],
        'channels':    h[7],
    }


def payload_to_complex64(payload, fmt, channels):
    """Convert a TCI IQ payload to numpy complex64 (interleaved re,im).

    ExpertSDR3 sends I/Q as channels=2 in DIGL/DIGU and other complex
    paths; channels=1 means a real signal (rare for the IQ stream but
    allowed by the protocol).  We handle channels=2 as the normal case
    and map channels=1 to a complex signal with q=0.

    Returns numpy array of dtype=complex64, or None on unsupported
    format.
    """
    if fmt == FORMAT_INT16:
        raw = np.frombuffer(payload, dtype='<i2').astype(np.float32)
        raw /= 32768.0
    elif fmt == FORMAT_INT32:
        raw = np.frombuffer(payload, dtype='<i4').astype(np.float32)
        raw /= 2147483648.0
    elif fmt == FORMAT_FLOAT32:
        raw = np.frombuffer(payload, dtype='<f4')
    elif fmt == FORMAT_INT24:
        # TCI's int24 is 3 bytes per sample, little-endian, signed.
        # numpy has no native int24; expand each 3-byte group to int32.
        b = np.frombuffer(payload, dtype=np.uint8)
        if len(b) % 3 != 0:
            return None
        # Interleave: low, mid, high  ->  build into int32
        low  = b[0::3].astype(np.int32)
        mid  = b[1::3].astype(np.int32)
        high = b[2::3].astype(np.int8).astype(np.int32)  # sign-extend
        raw = ((high << 16) | (mid << 8) | low).astype(np.float32)
        raw /= 8388608.0
    else:
        return None

    if channels == 2:
        # Interleaved I, Q -> complex
        if len(raw) % 2 != 0:
            return None
        return raw.view(np.complex64).copy()  # view + copy for ownership
    elif channels == 1:
        # Real signal; embed as complex with q=0
        return raw.astype(np.complex64)
    else:
        return None


class IQSidecar:
    def __init__(self, args):
        self.args = args
        self.running = True
        self.sock = None
        self.zmq_ctx = None
        self.zmq_pub = None

        self.frames_in    = 0
        self.bytes_out    = 0
        self.observed_rate = None
        self.observed_fmt  = None
        self.observed_ch   = None

    def _log(self, msg):
        print(f"[IQ-SIDECAR] {msg}", flush=True)

    def _die(self, msg):
        print(f"[IQ-SIDECAR] FATAL: {msg}", file=sys.stderr, flush=True)
        sys.exit(1)

    def setup_zmq(self):
        self.zmq_ctx = zmq.Context.instance()
        self.zmq_pub = self.zmq_ctx.socket(zmq.PUB)
        # Drop oldest messages under backpressure -- live SDR data; we
        # don't want to stall the publisher if a subscriber lags.
        self.zmq_pub.setsockopt(zmq.SNDHWM, 64)
        # Don't linger on shutdown: drop any pending messages immediately.
        self.zmq_pub.setsockopt(zmq.LINGER, 0)
        self.zmq_pub.bind(self.args.zmq_bind)
        self._log(f"ZMQ PUB bound to {self.args.zmq_bind}")

    def connect_rigctld(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.args.rigctld_host, self.args.rigctld_port))
        self.sock = s
        self._log(
            f"connected to rigctld at "
            f"{self.args.rigctld_host}:{self.args.rigctld_port}")

    def reader_loop(self):
        """Read TCI frames from rigctld, decode IQ, publish via ZMQ."""
        self._log("reader: started")
        buf = bytearray()
        last_log = time.monotonic()
        last_log_frames = 0
        last_log_bytes  = 0

        while self.running:
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                if self.running:
                    self._log(f"reader: recv error: {e}")
                break
            if not chunk:
                self._log("reader: rigctld closed connection")
                break
            buf += chunk

            while len(buf) >= HEADER_LEN:
                hdr = parse_header(buf)
                if hdr is None:
                    break

                fmt = hdr['format']
                ch  = hdr['channels']
                ln  = hdr['length']

                if fmt not in SAMPLE_BYTES:
                    self._log(
                        f"reader: bad format {fmt}, dropping 1 byte to resync")
                    del buf[:1]
                    continue
                if ch == 0 or ch > 2 or ln == 0 or ln > 65536:
                    self._log(
                        f"reader: bogus header st={hdr['stream_type']} "
                        f"len={ln} ch={ch}, resyncing")
                    del buf[:1]
                    continue

                payload_bytes = ln * SAMPLE_BYTES[fmt]  # ln is total samples,
                                                         # not per-channel
                frame_len = HEADER_LEN + payload_bytes
                if len(buf) < frame_len:
                    break  # wait for more

                payload = bytes(buf[HEADER_LEN:frame_len])
                del buf[:frame_len]

                if hdr['stream_type'] != STREAM_IQ:
                    # Not for us.  Defensive: rigctld already routes by
                    # stream_type but a misconfigured caller might still
                    # send something else.  Silently skip.
                    continue

                self._first_frame_log(hdr)

                samples = payload_to_complex64(payload, fmt, ch)
                if samples is None:
                    continue

                msg = samples.tobytes()  # complex64 interleaved
                try:
                    self.zmq_pub.send(msg, flags=zmq.NOBLOCK)
                    self.bytes_out += len(msg)
                except zmq.Again:
                    # PUB drops at HWM, but NOBLOCK + Again can also
                    # happen under sustained pressure.  Just drop.
                    pass

                self.frames_in += 1

            # Periodic stats line (every ~5 s).
            now = time.monotonic()
            if now - last_log >= 5.0:
                df = self.frames_in - last_log_frames
                db = self.bytes_out - last_log_bytes
                rate_kBps = db / (now - last_log) / 1024.0
                self._log(
                    f"stats: {df} frames, {db} bytes "
                    f"({rate_kBps:.1f} kB/s) over the last "
                    f"{now - last_log:.1f}s; "
                    f"running totals frames={self.frames_in} "
                    f"bytes={self.bytes_out}")
                last_log = now
                last_log_frames = self.frames_in
                last_log_bytes  = self.bytes_out

        self.running = False
        self._log("reader: stopped")

    def _first_frame_log(self, hdr):
        """Log a single line the first time we see a real IQ frame, plus
        any change in the negotiated parameters."""
        rate = hdr['sample_rate']
        fmt  = hdr['format']
        ch   = hdr['channels']

        if (rate, fmt, ch) != (self.observed_rate,
                               self.observed_fmt,
                               self.observed_ch):
            self._log(
                f"IQ stream: sample_rate={rate} Hz, format={fmt} "
                f"({['int16','int24','int32','float32'][fmt]}), "
                f"channels={ch} ({'complex' if ch == 2 else 'real'})")
            self.observed_rate = rate
            self.observed_fmt  = fmt
            self.observed_ch   = ch

    def run(self):
        self._log(
            f"starting -- rigctld={self.args.rigctld_host}:"
            f"{self.args.rigctld_port}  zmq={self.args.zmq_bind}")

        self.setup_zmq()
        self.connect_rigctld()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        t = threading.Thread(target=self.reader_loop, daemon=True)
        t.start()

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        self._log("shutting down")
        self.running = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        if self.zmq_pub:
            try:
                self.zmq_pub.close()
            except Exception:
                pass
        if self.zmq_ctx:
            try:
                self.zmq_ctx.term()
            except Exception:
                pass
        self._log("done")

    def _on_signal(self, *args):
        self._log("signal received, stopping")
        self.running = False


def main():
    p = argparse.ArgumentParser(description='TCI IQ sidecar for rigctld')
    p.add_argument('--rigctld-host', default='localhost')
    p.add_argument('--rigctld-port', type=int, default=4535,
                   help="rigctld IQ sidechannel port (-C iq_port=)")
    p.add_argument('--zmq-bind', default='tcp://*:5555',
                   help="ZMQ PUB endpoint to bind, default tcp://*:5555")
    args = p.parse_args()

    IQSidecar(args).run()


if __name__ == '__main__':
    main()
