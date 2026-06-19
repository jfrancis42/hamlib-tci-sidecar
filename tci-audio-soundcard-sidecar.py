#!/usr/bin/env python3
"""
TCI Audio Sidecar for Hamlib rigctld.

Architecture
------------

ExpertSDR3 ──TCI WebSocket──> rigctld ──binary TCI frames──> sidecar
                              (CAT)        (TCP :4534)        (this)
                                                                │
                                                                ▼
                                                        PulseAudio sinks
                                                        (tci-rx, tci-tx)
                                                                │
                                                                ▼
                                                       JS8Call / fldigi /
                                                       WSJT-X / etc.

Wire format on the rigctld<->sidecar TCP socket
-----------------------------------------------

Pure binary, length-framed TCI frames in both directions.  Every frame
starts with a 64-byte header, followed by `length * channels *
sample_bytes` payload bytes.  No text framing, no delimiters, no
mixed-mode demux.

Header (16 little-endian uint32 words):
    [0] receiver       trx index (0)
    [1] sample_rate    Hz (0 for control frames)
    [2] format         0=int16  1=int24  2=int32  3=float32
    [3] codec          0
    [4] crc            0
    [5] length         number of audio samples in payload
    [6] stream_type    1=RX_AUDIO  2=TX_AUDIO  3=TX_CHRONO  (extensible)
    [7] channels       1 for mono
    [8..15] reserved   zero-filled, room for future fields

Stream types
    1 RX_AUDIO   rigctld -> sidecar.  Audio from the radio.
    2 TX_AUDIO   sidecar -> rigctld.  Audio to the radio.
    3 TX_CHRONO  rigctld -> sidecar.  Empty payload; `length` carries
                                      the number of samples ExpertSDR3
                                      wants for the next TX chunk.

Why binary-only: audio payloads contain arbitrary bytes (including
0x0A).  Any text-line framing on this socket would shred frames.
Length-framed binary lets a single parser handle both control and
audio with no ambiguity.
"""
import argparse
import numpy as np
import queue
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time


# TCI stream types (must match TCI_STREAM_* in Hamlib rigs/dummy/tci2.c)
STREAM_RX_AUDIO  = 1
STREAM_TX_AUDIO  = 2
STREAM_TX_CHRONO = 3
STREAM_PTT_STATE = 4   # control frame: length=0 (PTT off) or 1 (PTT on)

# TCI sample format codes
FORMAT_INT16 = 0
FORMAT_INT24 = 1
FORMAT_INT32 = 2
FORMAT_FLOAT32 = 3

SAMPLE_BYTES = {
    FORMAT_INT16: 2,
    FORMAT_INT24: 3,
    FORMAT_INT32: 4,
    FORMAT_FLOAT32: 4,
}

# Header structure
HEADER_LEN = 64
HEADER_FMT = '<16I'  # 16 little-endian uint32

# Audio configuration for the virtual sinks (matches what ExpertSDR3 emits
# and what client apps like JS8Call expect for HF digital).
SAMPLE_RATE = 8000
CHANNELS = 1
PA_FORMAT = 's16le'

# TX buffer thresholds, in samples.  At 8 kHz, 50 ms = 400 samples.
TX_MIN_SAMPLES = int(0.050 * SAMPLE_RATE)   # below this -> send silence
TX_MAX_SAMPLES = int(4.000 * SAMPLE_RATE)   # cap on growth (prevents runaway)
RX_MAX_SAMPLES = int(4.000 * SAMPLE_RATE)



# -------------------------------------------------------------------------
# Frame parsing / building
# -------------------------------------------------------------------------

def build_frame(receiver, sample_rate, fmt, length, stream_type, channels,
                payload=b''):
    """Build a complete TCI binary frame: 64-byte header + payload."""
    header = struct.pack(
        HEADER_FMT,
        receiver, sample_rate, fmt, 0,
        0, length, stream_type, channels,
        0, 0, 0, 0, 0, 0, 0, 0,
    )
    return header + payload


def parse_header(buf32):
    """Parse a 64-byte header.  Returns dict, or None on bad header."""
    if len(buf32) < HEADER_LEN:
        return None
    h = struct.unpack(HEADER_FMT, buf32[:HEADER_LEN])
    return {
        'receiver':     h[0],
        'sample_rate':  h[1],
        'format':       h[2],
        'codec':        h[3],
        'length':       h[5],
        'stream_type':  h[6],
        'channels':     h[7],
    }


def frame_payload_bytes(hdr):
    """Bytes of payload following the header for a frame with this header.

    Stream-type semantics:
      RX_AUDIO  / TX_AUDIO   length = samples in payload, payload follows
      TX_CHRONO              length = samples *requested* (next TX chunk),
                             no payload bytes follow
      PTT_STATE              length = 0 (PTT off) or 1 (PTT on),
                             no payload bytes follow
    """
    if hdr['stream_type'] in (STREAM_TX_CHRONO, STREAM_PTT_STATE):
        return 0
    sb = SAMPLE_BYTES.get(hdr['format'])
    if sb is None:
        return None
    return hdr['length'] * hdr['channels'] * sb


# -------------------------------------------------------------------------
# Sidecar
# -------------------------------------------------------------------------

class Sidecar:
    def __init__(self, args):
        self.args = args
        self.tx_gain = 10.0 ** (args.tx_gain_db / 20.0)
        self.rx_gain = 10.0 ** (args.rx_gain_db / 20.0)

        self.running = True

        # Socket to rigctld audio sidechannel
        self.sock = None

        # PulseAudio helpers
        self.pacat_proc = None
        self.parec_proc = None
        self.null_sink_rx = None  # module id
        self.null_sink_tx = None

        # Audio buffers
        self.rx_buf = bytearray()
        self.rx_lock = threading.Lock()
        self.tx_buf = bytearray()
        self.tx_lock = threading.Lock()

        # Event-driven TX: rigctld_reader pushes (samples_needed) on
        # TX_CHRONO; tx_sender pops and ships TX_AUDIO immediately.
        self.tx_chrono_queue = queue.Queue()

        # Stats
        self.rx_frames = 0
        self.tx_frames = 0
        self.tx_silence_frames = 0

    # ----- audio devices -----

    def check_pulse(self):
        for tool in ('pactl', 'pacat', 'parec'):
            if shutil.which(tool) is None:
                self._die(f"required tool not found: {tool}\n"
                          "install pulseaudio-utils")
        r = subprocess.run(['pactl', 'info'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            self._die("pactl could not reach audio server")
        for line in r.stdout.splitlines():
            if line.startswith('Server Name:'):
                self._log(f"audio server: {line.split(':', 1)[1].strip()}")
                break

    def create_sinks(self):
        """Create tci-rx and tci-tx null sinks (idempotent)."""
        # Unload any stale sinks with our names.
        r = subprocess.run(['pactl', 'list', 'sinks', 'short'],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in (
                f"{self.args.name}-rx", f"{self.args.name}-tx",
            ):
                subprocess.run(['pactl', 'unload-module', parts[0]],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        time.sleep(0.5)

        # Advertise a low buffer target on the sink.  Without this, PA's
        # default per-stream prebuffer can be ~2 seconds on a slow sink
        # like ours (8 kHz mono), which adds 2 s of latency between
        # JS8Call writing audio and parec capturing it.  That breaks
        # slot-aligned modes like JS8 and FT8.
        #
        # node.latency = quantum_samples / sample_rate.  20 ms at 8 kHz
        # = 160 samples.  Clients that request lower latency than the
        # sink's default will get it; clients that don't will inherit
        # this hint.
        latency_quantum = int(0.020 * SAMPLE_RATE)  # 160 @ 8 kHz = 20 ms
        sink_props = f"node.latency={latency_quantum}/{SAMPLE_RATE}"

        for suffix in ('rx', 'tx'):
            dev = f"{self.args.name}-{suffix}"
            args = (
                f"sink_name={dev} rate={SAMPLE_RATE} "
                f"channels={CHANNELS} format={PA_FORMAT} "
                f"sink_properties={sink_props}"
            )
            r = subprocess.run(
                ['pactl', 'load-module', 'module-null-sink', args],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                self._die(f"failed to create {dev}: {r.stderr}")
            mod_id = r.stdout.strip()
            if suffix == 'rx':
                self.null_sink_rx = mod_id
            else:
                self.null_sink_tx = mod_id
            self._log(f"created {dev} (module {mod_id}, latency {latency_quantum}/{SAMPLE_RATE})")

    def cleanup_sinks(self):
        for mod in (self.null_sink_rx, self.null_sink_tx):
            if mod:
                subprocess.run(['pactl', 'unload-module', mod],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

    def start_pa_helpers(self):
        """Start pacat (RX playback) and parec (TX capture)."""
        self.pacat_proc = subprocess.Popen(
            ['pacat', '--playback', f'--device={self.args.name}-rx',
             f'--rate={SAMPLE_RATE}', f'--channels={CHANNELS}',
             f'--format={PA_FORMAT}'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.parec_proc = subprocess.Popen(
            ['parec', f'--device={self.args.name}-tx.monitor',
             f'--rate={SAMPLE_RATE}', f'--channels={CHANNELS}',
             f'--format={PA_FORMAT}'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if self.pacat_proc.poll() is not None:
            self._die("pacat exited immediately")
        if self.parec_proc.poll() is not None:
            self._die("parec exited immediately")

    # ----- network -----

    def connect_rigctld(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.args.rigctld_host, self.args.rigctld_port))
        self.sock = s
        self._log(
            f"connected to rigctld at "
            f"{self.args.rigctld_host}:{self.args.rigctld_port}",
        )

    # ----- threads -----

    def rigctld_reader(self):
        """Read TCI frames from rigctld and dispatch by stream_type."""
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

            # Drain as many complete frames as we have.
            while len(buf) >= HEADER_LEN:
                hdr = parse_header(buf)
                if hdr is None:
                    break

                payload_bytes = frame_payload_bytes(hdr)
                if payload_bytes is None:
                    self._log(
                        f"rigctld_reader: bad format {hdr['format']}, "
                        f"skipping 64 bytes")
                    del buf[:HEADER_LEN]
                    continue

                # Sanity bound.  If header looks corrupt, drop one byte
                # and rescan.  (Should not happen with the new binary
                # protocol, but defensive against future bugs.)
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
                    break  # wait for more bytes

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
        """Authoritative PTT edge from rigctld.

        On 0->1 (TX starting): flush parec capture.  Whatever parec
        captured while the radio was idle is, by definition, not what
        the user wants transmitted -- it would just delay live audio
        from reaching the radio at the start of every transmission.

        On 1->0: nothing to do; the buffer drains naturally as
        TX_CHRONO requests stop arriving.
        """
        self._log(f"PTT_STATE: {'ON' if ptt_on else 'OFF'}")
        if ptt_on:
            with self.tx_lock:
                if self.tx_buf:
                    self._log(
                        f"PTT-on edge: flushing {len(self.tx_buf)} bytes "
                        "of pre-TX parec capture")
                    self.tx_buf.clear()

    def _handle_rx_audio(self, hdr, payload):
        """Convert RX payload to s16le mono and push to RX buffer."""
        fmt = hdr['format']
        if fmt == FORMAT_INT16:
            samples = np.frombuffer(payload, dtype='<i2')
        elif fmt == FORMAT_FLOAT32:
            f = np.frombuffer(payload, dtype='<f4')
            samples = np.clip(f * 32767.0, -32768, 32767).astype(np.int16)
        elif fmt == FORMAT_INT32:
            i32 = np.frombuffer(payload, dtype='<i4')
            samples = (i32 >> 16).astype(np.int16)
        else:
            return  # int24 not supported by current ExpertSDR3 paths

        if hdr['channels'] > 1:
            samples = samples.reshape(-1, hdr['channels']).mean(axis=1) \
                             .astype(np.int16)

        if self.rx_gain != 1.0:
            samples = np.clip(samples.astype(np.float32) * self.rx_gain,
                              -32768, 32767).astype(np.int16)

        out = samples.tobytes()
        with self.rx_lock:
            self.rx_buf += out
            # Trim if we've fallen way behind (shouldn't normally happen).
            limit = RX_MAX_SAMPLES * 2
            if len(self.rx_buf) > limit:
                del self.rx_buf[:len(self.rx_buf) - limit]

        self.rx_frames += 1
        if self.rx_frames % 100 == 0:
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            self._log(
                f"RX #{self.rx_frames}: {len(samples)} samp, "
                f"RMS={rms:.0f} (rx_buf={len(self.rx_buf)} bytes)")

    def pacat_writer(self):
        """Drain the RX buffer to pacat's stdin."""
        self._log("pacat_writer: started")
        chunk = 1024  # 512 samples * 2 bytes
        while self.running:
            with self.rx_lock:
                if len(self.rx_buf) >= chunk:
                    data = bytes(self.rx_buf[:chunk])
                    del self.rx_buf[:chunk]
                else:
                    data = None
            if data is None:
                time.sleep(0.005)
                continue
            try:
                self.pacat_proc.stdin.write(data)
                self.pacat_proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._log(f"pacat_writer: write error: {e}")
                break
        self.running = False
        self._log("pacat_writer: stopped")

    def parec_reader(self):
        """Pull TX audio from parec's stdout into the TX buffer."""
        self._log("parec_reader: started")
        while self.running:
            try:
                data = self.parec_proc.stdout.read(1024)
            except OSError as e:
                self._log(f"parec_reader: read error: {e}")
                break
            if not data:
                break
            with self.tx_lock:
                self.tx_buf += data
                limit = TX_MAX_SAMPLES * 2
                if len(self.tx_buf) > limit:
                    del self.tx_buf[:len(self.tx_buf) - limit]
        self.running = False
        self._log("parec_reader: stopped")

    def tx_sender(self):
        """Wait for TX_CHRONO, ship TX_AUDIO frame in response."""
        self._log("tx_sender: started")
        while self.running:
            try:
                samples_needed = self.tx_chrono_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            need_bytes = samples_needed * 2  # s16le mono
            silence = False

            with self.tx_lock:
                if len(self.tx_buf) >= max(need_bytes, TX_MIN_SAMPLES * 2):
                    data = bytes(self.tx_buf[:need_bytes])
                    del self.tx_buf[:need_bytes]
                else:
                    data = b'\x00' * need_bytes
                    silence = True

            samples = np.frombuffer(data, dtype='<i2')
            if not silence and self.tx_gain != 1.0:
                samples = np.clip(samples.astype(np.float32) * self.tx_gain,
                                  -32768, 32767).astype(np.int16)
                data = samples.tobytes()

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
            if silence or self.tx_frames % 50 == 0:
                peak = int(np.max(np.abs(samples))) if len(samples) else 0
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) \
                    if len(samples) else 0.0
                tag = 'SILENCE' if silence else 'audio'
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
        self._log(f"TX gain {self.args.tx_gain_db:+.1f} dB "
                  f"({self.tx_gain:.3f}x)")
        self._log(f"RX gain {self.args.rx_gain_db:+.1f} dB "
                  f"({self.rx_gain:.3f}x)")

        self.check_pulse()
        self.create_sinks()
        self.connect_rigctld()
        self.start_pa_helpers()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        threads = [
            threading.Thread(target=self.rigctld_reader, daemon=True),
            threading.Thread(target=self.pacat_writer,   daemon=True),
            threading.Thread(target=self.parec_reader,   daemon=True),
            threading.Thread(target=self.tx_sender,      daemon=True),
        ]
        for t in threads:
            t.start()

        self._log("ready - audio bridge active")
        self._log(f"  RX device: {self.args.name}-rx.monitor")
        self._log(f"  TX device: {self.args.name}-tx")

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
        for proc in (self.pacat_proc, self.parec_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.cleanup_sinks()
        self._log("done")

    def _on_signal(self, *args):
        self._log("signal received, stopping")
        self.running = False

    # ----- log helpers -----

    def _log(self, msg):
        print(f"[SIDECAR] {msg}", flush=True)

    def _die(self, msg):
        print(f"[SIDECAR] FATAL: {msg}", file=sys.stderr, flush=True)
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description='TCI audio sidecar for rigctld')
    p.add_argument('--rigctld-host', default='localhost')
    p.add_argument('--rigctld-port', type=int, default=4534,
                   help='rigctld audio sidechannel port (-C audio_port=)')
    p.add_argument('--name', default='tci',
                   help='sink name prefix; creates <name>-rx and <name>-tx')
    p.add_argument('--tx-gain-db', type=float, default=20.0,
                   help='TX audio gain in dB (default +20)')
    p.add_argument('--rx-gain-db', type=float, default=0.0,
                   help='RX audio gain in dB (default 0)')
    args = p.parse_args()

    Sidecar(args).run()


if __name__ == '__main__':
    main()
