#!/usr/bin/env python3
"""
TCI Audio GR Sidecar -- bridges Hamlib rigctld's audio sidechannel to
GNU Radio over ZMQ.

Same wire protocol as ``tci-audio-soundcard-sidecar.py`` (the PulseAudio audio
sidecar): connects to rigctld on the audio sidechannel port (default
4534), parses 64-byte TCI binary frames, dispatches by ``stream_type``.
The difference is the consumer side:

  - RX audio: TCI int16 mono -> float32 mono -> ZMQ PUB endpoint
    (default ``tcp://*:5557``).  Any GR flowgraph attaches via the
    stock ``zmq_sub_source`` block (item type ``float``, vec length 1).
  - TX audio: ZMQ PULL endpoint (default ``tcp://*:5558``) accepts
    float32 samples from a GR flowgraph (``zmq_push_sink``).  Samples
    are buffered; on every TX_CHRONO from the radio, the right-sized
    chunk is converted back to int16 and shipped.
  - PTT_STATE: ON flushes the TX buffer (same logic as the PulseAudio
    sidecar -- whatever was queued before PTT engaged is, by
    definition, not what the user wants transmitted).

This sidecar is an alternative to ``tci-audio-soundcard-sidecar.py``, not a complement
to it.  rigctld's audio sidechannel only accepts one client at a time;
run either the PulseAudio sidecar (for JS8Call/fldigi/etc.) or this GR
sidecar, not both.

ZMQ ports:

  RX PUB   tcp://*:5557     (GR consumes here)
  TX PULL  tcp://*:5558     (GR pushes here)

Both PUB and PULL are lossy under backpressure -- if a slow consumer
falls behind on RX, old messages drop; if too much TX audio is pushed
without PTT, the buffer is capped and old data drops.  Lossy is the
right behaviour for live audio.

Sample rate is 8 kHz mono throughout (the rate ExpertSDR3 negotiates
for HF digital modes -- ``AUDIO_SAMPLERATE`` is set by rigctld).  GR
flowgraphs that want 48 kHz can resample at the boundary.
"""
import argparse
import numpy as np
import queue
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

# Sample format codes (TCI header word [2]).
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
HEADER_FMT = '<16I'

# Audio rate ExpertSDR3 negotiates for HF digital modes.  Both
# directions of this sidecar use this rate; resampling to/from other
# rates lives in the consumer's GR flowgraph, not here.
SAMPLE_RATE = 8000
CHANNELS = 1

# TX buffer thresholds, in samples.  At 8 kHz, 50 ms = 400 samples.
TX_MIN_SAMPLES = int(0.050 * SAMPLE_RATE)   # below this -> send silence
TX_MAX_SAMPLES = int(4.000 * SAMPLE_RATE)   # cap on growth (drop oldest)
RX_MAX_SAMPLES = int(4.000 * SAMPLE_RATE)


# -------------------------------------------------------------------------
# Frame helpers
# -------------------------------------------------------------------------

def build_frame(receiver, sample_rate, fmt, length, stream_type, channels,
                payload=b''):
    """Build a 64-byte TCI header + payload."""
    header = struct.pack(
        HEADER_FMT,
        receiver, sample_rate, fmt, 0,
        0, length, stream_type, channels,
        0, 0, 0, 0, 0, 0, 0, 0,
    )
    return header + payload


def parse_header(buf):
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


def frame_payload_bytes(hdr):
    if hdr['stream_type'] in (STREAM_TX_CHRONO, STREAM_PTT_STATE):
        return 0
    sb = SAMPLE_BYTES.get(hdr['format'])
    if sb is None:
        return None
    return hdr['length'] * hdr['channels'] * sb


# -------------------------------------------------------------------------
# Sidecar
# -------------------------------------------------------------------------

class GRSidecar:

    def __init__(self, args):
        self.args = args
        self.tx_gain = 10.0 ** (args.tx_gain_db / 20.0)
        self.rx_gain = 10.0 ** (args.rx_gain_db / 20.0)

        self.running = True

        # Socket to rigctld audio sidechannel
        self.sock = None

        # ZMQ
        self.zmq_ctx = None
        self.zmq_rx_pub = None    # publishes RX audio (float32) to GR
        self.zmq_tx_pull = None   # receives TX audio (float32) from GR

        # TX buffer (bytes, int16 native after format conversion).  The
        # tx_pull_reader thread fills it; the tx_sender thread drains it
        # in response to TX_CHRONO.
        self.tx_buf = bytearray()
        self.tx_lock = threading.Lock()

        # Event-driven TX
        self.tx_chrono_queue = queue.Queue()

        # Stats
        self.rx_frames = 0
        self.tx_frames = 0
        self.tx_silence_frames = 0

    # ----- log helpers -----

    def _log(self, msg):
        print(f"[AUDIO-GR] {msg}", flush=True)

    def _die(self, msg):
        print(f"[AUDIO-GR] FATAL: {msg}", file=sys.stderr, flush=True)
        sys.exit(1)

    # ----- setup -----

    def setup_zmq(self):
        self.zmq_ctx = zmq.Context.instance()

        self.zmq_rx_pub = self.zmq_ctx.socket(zmq.PUB)
        self.zmq_rx_pub.setsockopt(zmq.SNDHWM, 64)
        self.zmq_rx_pub.setsockopt(zmq.LINGER, 0)
        self.zmq_rx_pub.bind(self.args.zmq_rx_bind)
        self._log(f"ZMQ PUB (RX audio) bound to {self.args.zmq_rx_bind}")

        self.zmq_tx_pull = self.zmq_ctx.socket(zmq.PULL)
        self.zmq_tx_pull.setsockopt(zmq.RCVHWM, 64)
        self.zmq_tx_pull.setsockopt(zmq.LINGER, 0)
        self.zmq_tx_pull.bind(self.args.zmq_tx_bind)
        self._log(f"ZMQ PULL (TX audio) bound to {self.args.zmq_tx_bind}")

    def connect_rigctld(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.args.rigctld_host, self.args.rigctld_port))
        self.sock = s
        self._log(
            f"connected to rigctld at "
            f"{self.args.rigctld_host}:{self.args.rigctld_port}")

    # ----- inbound: rigctld -> us -----

    def rigctld_reader(self):
        """Read TCI frames from rigctld and dispatch."""
        self._log("rigctld_reader: started")
        buf = bytearray()
        while self.running:
            try:
                chunk = self.sock.recv(8192)
            except OSError as e:
                if self.running:
                    self._log(f"rigctld_reader: recv error: {e}")
                break
            if not chunk:
                self._log("rigctld_reader: rigctld closed connection")
                break
            buf += chunk

            while len(buf) >= HEADER_LEN:
                hdr = parse_header(buf)
                if hdr is None:
                    break

                payload_bytes = frame_payload_bytes(hdr)
                if payload_bytes is None:
                    self._log(
                        f"rigctld_reader: bad format {hdr['format']}, "
                        "skipping 64 bytes")
                    del buf[:HEADER_LEN]
                    continue

                if (hdr['stream_type'] not in (STREAM_RX_AUDIO,
                                                STREAM_TX_AUDIO,
                                                STREAM_TX_CHRONO,
                                                STREAM_PTT_STATE)
                        or hdr['channels'] == 0
                        or hdr['channels'] > 8
                        or hdr['length'] > 65536):
                    self._log(
                        "rigctld_reader: bogus header "
                        f"stream={hdr['stream_type']} "
                        f"len={hdr['length']} ch={hdr['channels']}, "
                        "resyncing")
                    del buf[:1]
                    continue

                frame_len = HEADER_LEN + payload_bytes
                if len(buf) < frame_len:
                    break

                payload = bytes(buf[HEADER_LEN:frame_len])
                del buf[:frame_len]
                self._dispatch(hdr, payload)

        self.running = False
        self._log("rigctld_reader: stopped")

    def _dispatch(self, hdr, payload):
        st = hdr['stream_type']
        if st == STREAM_RX_AUDIO:
            self._handle_rx_audio(hdr, payload)
        elif st == STREAM_TX_CHRONO:
            self.tx_chrono_queue.put(hdr['length'])
        elif st == STREAM_PTT_STATE:
            self._handle_ptt_edge(hdr['length'])
        # STREAM_TX_AUDIO from rigctld would be a protocol error; ignore.

    def _handle_ptt_edge(self, ptt_on):
        """On 0->1: flush the TX buffer.  Anything queued before PTT
        engaged is by definition not what the user wants transmitted
        (it would just delay live audio at the start of every TX)."""
        self._log(f"PTT_STATE: {'ON' if ptt_on else 'OFF'}")
        if ptt_on:
            with self.tx_lock:
                if self.tx_buf:
                    self._log(
                        f"PTT-on edge: flushing {len(self.tx_buf)} bytes "
                        "of pre-TX queued samples")
                    self.tx_buf.clear()

    def _handle_rx_audio(self, hdr, payload):
        """Convert TCI RX_AUDIO payload to float32 mono and publish to
        GR over the RX ZMQ PUB socket."""
        fmt = hdr['format']
        if fmt == FORMAT_INT16:
            samples_i16 = np.frombuffer(payload, dtype='<i2')
            samples_f = samples_i16.astype(np.float32) / 32768.0
        elif fmt == FORMAT_FLOAT32:
            samples_f = np.frombuffer(payload, dtype='<f4').copy()
        elif fmt == FORMAT_INT32:
            samples_i32 = np.frombuffer(payload, dtype='<i4')
            samples_f = samples_i32.astype(np.float32) / 2147483648.0
        else:
            return  # int24 not supported by current ExpertSDR3 paths

        if hdr['channels'] > 1:
            samples_f = samples_f.reshape(-1, hdr['channels']) \
                                 .mean(axis=1).astype(np.float32)

        if self.rx_gain != 1.0:
            samples_f = np.clip(samples_f * self.rx_gain,
                                -1.0, 1.0).astype(np.float32)

        msg = samples_f.tobytes()
        try:
            self.zmq_rx_pub.send(msg, flags=zmq.NOBLOCK)
        except zmq.Again:
            # PUB drops at HWM, but NOBLOCK + Again can also happen
            # under sustained pressure.  Just drop.
            pass

        self.rx_frames += 1
        if self.rx_frames % 100 == 0:
            rms = float(np.sqrt(np.mean(samples_f ** 2))) \
                if len(samples_f) else 0.0
            peak = float(np.max(np.abs(samples_f))) \
                if len(samples_f) else 0.0
            self._log(
                f"RX #{self.rx_frames}: {len(samples_f)} samp, "
                f"RMS={rms:.4f} peak={peak:.4f}")

    # ----- inbound: GR -> us (TX audio) -----

    def tx_pull_reader(self):
        """Pull TX float32 audio from the GR flowgraph and accumulate
        as int16 in the TX buffer."""
        self._log("tx_pull_reader: started")
        # Use poll so we can wake up on shutdown.
        poller = zmq.Poller()
        poller.register(self.zmq_tx_pull, zmq.POLLIN)
        while self.running:
            events = dict(poller.poll(timeout=200))
            if self.zmq_tx_pull not in events:
                continue
            try:
                msg = self.zmq_tx_pull.recv(flags=zmq.NOBLOCK)
            except zmq.Again:
                continue

            # GR pushes float32; convert to int16 with clipping.  Apply
            # TX gain here -- same place the PulseAudio sidecar does it.
            f = np.frombuffer(msg, dtype=np.float32)
            if self.tx_gain != 1.0:
                f = f * self.tx_gain
            i16 = np.clip(f * 32768.0, -32768, 32767).astype(np.int16)

            with self.tx_lock:
                self.tx_buf += i16.tobytes()
                limit = TX_MAX_SAMPLES * 2
                if len(self.tx_buf) > limit:
                    del self.tx_buf[:len(self.tx_buf) - limit]

        self.running = False
        self._log("tx_pull_reader: stopped")

    # ----- outbound: us -> rigctld (TX audio) -----

    def tx_sender(self):
        """Wait for TX_CHRONO, ship STREAM_TX_AUDIO frame in response."""
        self._log("tx_sender: started")
        while self.running:
            try:
                samples_needed = self.tx_chrono_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            need_bytes = samples_needed * 2  # int16 mono
            silence = False
            partial = False

            with self.tx_lock:
                avail = len(self.tx_buf)
                if avail >= need_bytes:
                    # Full chunk available -- normal case.
                    data = bytes(self.tx_buf[:need_bytes])
                    del self.tx_buf[:need_bytes]
                elif avail > 0:
                    # Partial chunk: ship what we have, pad the rest
                    # with silence.  This is what real audio devices
                    # do; alternative (drop everything and send pure
                    # silence) inflicts buffer-pacing-edge-case
                    # silence on the radio, which is much worse for
                    # the user.  The radio gets a slightly-quieter
                    # frame at the start of each TX cycle as the
                    # buffer fills, then full audio thereafter.
                    data = bytes(self.tx_buf) + (b'\x00' * (need_bytes - avail))
                    self.tx_buf.clear()
                    partial = True
                else:
                    # Buffer truly empty -- send pure silence.  Happens
                    # when the radio asks for audio before any has been
                    # pushed (PTT-on edge), or after the producer stops.
                    data = b'\x00' * need_bytes
                    silence = True

            frame = build_frame(
                receiver=0,
                sample_rate=SAMPLE_RATE,
                fmt=FORMAT_INT16,
                length=samples_needed,
                stream_type=STREAM_TX_AUDIO,
                channels=CHANNELS,
                payload=data,
            )

            try:
                self.sock.sendall(frame)
            except OSError as e:
                self._log(f"tx_sender: send error: {e}")
                break

            self.tx_frames += 1
            if silence:
                self.tx_silence_frames += 1
            if silence or partial or self.tx_frames % 50 == 0:
                samples_i16 = np.frombuffer(data, dtype='<i2')
                peak = int(np.max(np.abs(samples_i16))) \
                    if len(samples_i16) else 0
                rms = float(np.sqrt(np.mean(
                    samples_i16.astype(np.float32) ** 2))) \
                    if len(samples_i16) else 0.0
                if silence:
                    tag = 'SILENCE'
                elif partial:
                    tag = f'partial({avail} of {need_bytes} bytes)'
                else:
                    tag = 'audio'
                with self.tx_lock:
                    buf_left = len(self.tx_buf)
                self._log(
                    f"TX #{self.tx_frames}: {samples_needed} samp ({tag}, "
                    f"peak={peak} rms={rms:.0f}, "
                    f"tx_buf_left={buf_left} bytes, "
                    f"silence_total={self.tx_silence_frames})")

        self.running = False
        self._log("tx_sender: stopped")

    # ----- lifecycle -----

    def run(self):
        self._log(
            f"starting -- rigctld={self.args.rigctld_host}:"
            f"{self.args.rigctld_port}  "
            f"rx={self.args.zmq_rx_bind}  tx={self.args.zmq_tx_bind}")
        self._log(f"TX gain {self.args.tx_gain_db:+.1f} dB "
                  f"({self.tx_gain:.3f}x)")
        self._log(f"RX gain {self.args.rx_gain_db:+.1f} dB "
                  f"({self.rx_gain:.3f}x)")

        self.setup_zmq()
        self.connect_rigctld()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        threads = [
            threading.Thread(target=self.rigctld_reader, daemon=True),
            threading.Thread(target=self.tx_pull_reader, daemon=True),
            threading.Thread(target=self.tx_sender,      daemon=True),
        ]
        for t in threads:
            t.start()

        self._log("ready -- audio bridge active (GR mode)")
        self._log(f"  GR consumes RX audio: {self.args.zmq_rx_bind}")
        self._log(f"  GR pushes TX audio:   {self.args.zmq_tx_bind}")
        self._log(f"  Sample rate: {SAMPLE_RATE} Hz, mono, float32")

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
        for s in (self.zmq_rx_pub, self.zmq_tx_pull):
            if s:
                try:
                    s.close()
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
    p = argparse.ArgumentParser(
        description="TCI audio sidecar for GNU Radio (ZMQ-based)")
    p.add_argument('--rigctld-host', default='localhost')
    p.add_argument('--rigctld-port', type=int, default=4534,
                   help="rigctld audio sidechannel port "
                        "(-C audio_port=, default %(default)s)")
    p.add_argument('--zmq-rx-bind', default='tcp://*:5557',
                   help="ZMQ PUB endpoint for RX audio "
                        "(GR consumes this; default %(default)s)")
    p.add_argument('--zmq-tx-bind', default='tcp://*:5558',
                   help="ZMQ PULL endpoint for TX audio "
                        "(GR pushes here; default %(default)s)")
    p.add_argument('--tx-gain-db', type=float, default=20.0,
                   help="TX audio gain in dB (default +20; "
                        "see ExpertSDR3 quiet-audio note)")
    p.add_argument('--rx-gain-db', type=float, default=0.0,
                   help="RX audio gain in dB (default 0)")
    args = p.parse_args()

    GRSidecar(args).run()


if __name__ == '__main__':
    main()
