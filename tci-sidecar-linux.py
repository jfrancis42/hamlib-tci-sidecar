#!/usr/bin/env python3
"""
TCI Audio Sidecar for Hamlib rigctld

Connects to rigctld's audio socket (port 4534) and bridges TCI audio to/from
virtual PulseAudio devices. This allows ham radio software (JS8Call, fldigi,
WSJT-X, etc.) to use the SunSDR2 Pro via standard audio devices.

Architecture:
    ExpertSDR3 (TCI) <-> rigctld <-> THIS SIDECAR <-> PulseAudio (tci-rx, tci-tx)
                                                           |
                                                    Ham Radio Software

rigctld handles all TCI protocol and CAT control. This sidecar only handles:
- Creating virtual audio devices
- Receiving RX_AUDIO_STREAM frames from rigctld and playing to tci-rx
- Capturing TX audio from tci-tx and sending TX_AUDIO_STREAM to rigctld
- Handling TX_CHRONO frames for transmit timing

Usage:
    ./tci_sidecar.py --rigctld-host localhost --rigctld-port 4534 --name tci
"""
import argparse
import numpy as np
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

# TCI stream types (from TCI spec)
STREAM_TYPE_RX_AUDIO = 1
STREAM_TYPE_TX_AUDIO = 2
STREAM_TYPE_TX_CHRONO = 3

# TCI data format codes
FORMAT_INT16 = 0
FORMAT_INT24 = 1
FORMAT_INT32 = 2
FORMAT_FLOAT32 = 3

# Audio configuration (standard for ham radio digital modes)
SAMPLE_RATE = 8000
CHANNELS = 1
FORMAT = 's16le'  # signed 16-bit little-endian

# Linear gain factors set from CLI in main() (--tx-gain-db / --rx-gain-db).
# Defaults are chosen so the system works with JS8Call out of the box.
TX_GAIN_LIN = 10.0   # 20 dB; JS8Call outputs ~ -20 dBFS and ExpertSDR3 ignores quieter audio
RX_GAIN_LIN = 1.0    # 0 dB; RX is loud enough already

# Global state
running = True
rigctld_sock = None
rigctld_host = None
rigctld_port = None
pacat_proc = None
parec_proc = None
null_sink_rx = None
null_sink_tx = None

# Audio buffers and locks
rx_buf = []
rx_lock = threading.Lock()
tx_buf = []
tx_lock = threading.Lock()

# TX state
tx_chrono_pending = 0  # How many samples ExpertSDR3 expects us to send
tx_chrono_lock = threading.Lock()


def parse_tci_frame(data):
    """
    Parse TCI binary frame.

    Frame structure (from TCI spec):
      - 64-byte header:
          - Offset 0-31: 8 uint32 fields (little-endian)
              [0] receiver (0 or 1)
              [1] sample_rate
              [2] data_format (0=int16, 1=int24, 2=int32, 3=float32)
              [3] codec (reserved)
              [4] crc (reserved)
              [5] length (number of samples)
              [6] stream_type (1=RX_AUDIO, 2=TX_AUDIO, 3=TX_CHRONO)
              [7] channels
          - Offset 32-63: reserved (32 bytes)
      - Audio data: starts at offset 64

    Returns dict with parsed fields, or None if invalid.
    """
    if len(data) < 64:
        return None

    # Parse 8 uint32 header fields
    header = struct.unpack('<8I', data[:32])

    return {
        'receiver': header[0],
        'sample_rate': header[1],
        'format': header[2],
        'codec': header[3],
        'crc': header[4],
        'length': header[5],      # Number of samples
        'stream_type': header[6],
        'channels': header[7],
        'audio_data': data[64:]   # Everything after 64-byte header
    }


def create_tci_frame(receiver, sample_rate, data_format, length, stream_type, channels, audio_data):
    """
    Create TCI binary frame for transmission.

    Used to send TX_AUDIO_STREAM frames back to rigctld.
    """
    # Create 8 uint32 header fields
    header = struct.pack('<8I',
                        receiver,
                        sample_rate,
                        data_format,
                        0,  # codec (reserved)
                        0,  # crc (reserved)
                        length,
                        stream_type,
                        channels)

    # Pad to 64 bytes
    header += b'\x00' * 32

    return header + audio_data


def check_dependencies():
    """Verify pactl/pacat/parec are on PATH and that a PulseAudio-compatible
    server is running.  Exit with a helpful error message if anything is
    missing -- this saves the user from diagnosing a FileNotFoundError or
    a connection refused error from deep inside an audio thread."""
    missing = [tool for tool in ("pactl", "pacat", "parec")
               if shutil.which(tool) is None]
    if missing:
        print(
            f"[SIDECAR] ERROR: required tools not found on PATH: {', '.join(missing)}\n"
            f"[SIDECAR] Install the PulseAudio command-line tools:\n"
            f"[SIDECAR]   Debian/Ubuntu/Mint: sudo apt install pulseaudio-utils\n"
            f"[SIDECAR]   Fedora/RHEL:        sudo dnf install pulseaudio-utils\n"
            f"[SIDECAR]   Arch:               sudo pacman -S libpulse\n"
            f"[SIDECAR] (These work whether the system audio server is\n"
            f"[SIDECAR]  PulseAudio itself or PipeWire with pipewire-pulse.)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # `pactl info` exits non-zero if no PA-compatible server is reachable.
    try:
        result = subprocess.run(
            ["pactl", "info"], capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        print(f"[SIDECAR] ERROR: failed to run pactl: {e}", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(
            f"[SIDECAR] ERROR: pactl could not reach an audio server:\n"
            f"[SIDECAR]   {result.stderr.strip() or result.stdout.strip()}\n"
            f"[SIDECAR] You need either PulseAudio or PipeWire (with the\n"
            f"[SIDECAR] pipewire-pulse shim) running for your user session.\n"
            f"[SIDECAR] On a desktop session this is normally automatic; on a\n"
            f"[SIDECAR] headless box you may need:\n"
            f"[SIDECAR]   systemctl --user enable --now pipewire pipewire-pulse\n"
            f"[SIDECAR]   (or pulseaudio.service depending on the distro)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Helpful (not fatal): identify what server we're talking to.
    server_name = ""
    for line in result.stdout.splitlines():
        if line.startswith("Server Name:"):
            server_name = line.split(":", 1)[1].strip()
            break
    print(f"[SIDECAR] Audio server: {server_name or 'PulseAudio-compatible'}")


def create_audio_devices(name):
    """Create PulseAudio null sinks for RX and TX."""
    global null_sink_rx, null_sink_tx

    print(f"[SIDECAR] Creating PulseAudio devices: {name}-rx, {name}-tx")

    # Clean up any existing tci devices first
    print(f"[SIDECAR] Cleaning up old {name} devices...")
    result = subprocess.run(['pactl', 'list', 'sinks', 'short'],
                          capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (f'{name}-rx' in parts[1] or f'{name}-tx' in parts[1]):
            module_id = parts[0]
            print(f"[SIDECAR]   Unloading old module {module_id} ({parts[1]})")
            subprocess.run(['pactl', 'unload-module', module_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(0.5)  # Let PipeWire settle

    for suffix in ['rx', 'tx']:
        dev_name = f"{name}-{suffix}"

        # Create null sink with explicit format
        result = subprocess.run(
            ['pactl', 'load-module', 'module-null-sink',
             f'sink_name={dev_name}',
             f'rate={SAMPLE_RATE}',
             f'channels={CHANNELS}',
             f'format={FORMAT}'],
            capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create {dev_name}: {result.stderr}")

        module_id = result.stdout.strip()
        if suffix == 'rx':
            null_sink_rx = module_id
        else:
            null_sink_tx = module_id

        print(f"[SIDECAR]   Created {dev_name} (module {module_id})")


def cleanup_audio_devices():
    """Remove PulseAudio null sinks."""
    global null_sink_rx, null_sink_tx

    print("[SIDECAR] Cleaning up audio devices")
    for module_id in [null_sink_rx, null_sink_tx]:
        if module_id:
            subprocess.run(['pactl', 'unload-module', module_id],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


def pacat_writer():
    """
    Thread: Write RX audio from buffer to tci-rx device.

    Reads from rx_buf (populated by rigctld_reader) and writes to pacat stdin.
    """
    global pacat_proc, rx_buf

    print("[SIDECAR] pacat_writer thread started")

    write_count = 0
    check_count = 0
    while running:
        # Small sleep to avoid burning CPU, but fast enough to catch samples
        time.sleep(0.001)  # 1ms check interval
        check_count += 1
        with rx_lock:
            buf_len = len(rx_buf)
            if buf_len < 512:
                if check_count % 500 == 0:  # Every 5 seconds
                    print(f"[SIDECAR] pacat_writer: checked {check_count} times, buf_len={buf_len} (waiting)")
                continue

            # Take 512 samples (1024 bytes for int16)
            samples = rx_buf[:512]
            del rx_buf[:512]  # Remove from buffer in-place
            # Log every write until we see it working
            if write_count < 10 or write_count % 100 == 0:
                print(f"[SIDECAR] pacat_writer: taking {len(samples)} samples from buffer (remaining={len(rx_buf)})")

        try:
            # Convert to int16 numpy array and write to pacat
            s16 = np.array(samples, dtype=np.int16)
            bytes_written = pacat_proc.stdin.write(s16.tobytes())
            pacat_proc.stdin.flush()

            write_count += 1
            if write_count % 100 == 0:  # Every 100 writes (~6 seconds at 8kHz)
                rms = float(np.sqrt(np.mean(s16.astype(np.float32) ** 2)))
                print(f"[SIDECAR] pacat_writer: wrote {write_count} chunks, last RMS={rms:.1f}, bytes={bytes_written}")
        except (BrokenPipeError, OSError) as e:
            print(f"[SIDECAR] pacat write error: {e}")
            break

    print("[SIDECAR] pacat_writer thread stopped")


def parec_reader():
    """
    Thread: Read TX audio from tci-tx.monitor and buffer it.

    Reads from parec stdout and populates tx_buf (consumed by tx_sender).
    """
    global parec_proc, tx_buf

    print("[SIDECAR] parec_reader thread started")

    while running:
        try:
            # Read 512 samples (1024 bytes for int16)
            data = parec_proc.stdout.read(1024)
            if not data:
                break

            # Convert to int16 array
            samples = np.frombuffer(data, dtype=np.int16)

            with tx_lock:
                tx_buf.extend(samples.tolist())

                # Limit buffer to 2 seconds
                max_samples = SAMPLE_RATE * 2
                if len(tx_buf) > max_samples:
                    del tx_buf[:len(tx_buf) - max_samples]  # Keep last max_samples

        except Exception as e:
            print(f"[SIDECAR] parec read error: {e}")
            break

    print("[SIDECAR] parec_reader thread stopped")


def tx_sender():
    """
    Thread: Send TX audio to rigctld when TX_CHRONO requests it.

    When a TX_CHRONO frame arrives (from rigctld_reader), it sets tx_chrono_pending
    to the number of samples needed. This thread then pulls that many samples from
    tx_buf, creates a TX_AUDIO_STREAM frame, and sends it to rigctld.
    """
    global rigctld_sock, tx_buf, tx_chrono_pending

    print("[SIDECAR] tx_sender thread started")

    while running:
        time.sleep(0.01)  # 10ms check interval

        with tx_chrono_lock:
            if tx_chrono_pending == 0:
                continue

            needed = tx_chrono_pending
            tx_chrono_pending = 0

        # Get samples from TX buffer
        is_silence = False
        with tx_lock:
            buf_avail = len(tx_buf)
            if buf_avail < needed:
                # Not enough audio yet, send silence
                samples = [0] * needed
                is_silence = True
            else:
                samples = tx_buf[:needed]
                del tx_buf[:needed]  # Remove from buffer in-place

        # Create TX_AUDIO_STREAM frame.  Apply --tx-gain-db (default 20 dB =
        # 10x) with clipping protection.  ExpertSDR3 silently ignores TX
        # audio below some threshold; that's why the default is so high.
        if TX_GAIN_LIN == 1.0:
            s16_array = np.array(samples, dtype=np.int16)
        else:
            s16_array = np.clip(
                np.array(samples, dtype=np.float32) * TX_GAIN_LIN,
                -32768, 32767,
            ).astype(np.int16)
        audio_bytes = s16_array.tobytes()

        # Log RMS+peak so we can see what the radio actually receives
        peak = int(np.max(np.abs(s16_array))) if len(s16_array) else 0
        rms = float(np.sqrt(np.mean(s16_array.astype(np.float32) ** 2))) if len(s16_array) else 0.0

        frame = create_tci_frame(
            receiver=0,
            sample_rate=SAMPLE_RATE,
            data_format=FORMAT_INT16,
            length=len(samples),
            stream_type=STREAM_TYPE_TX_AUDIO,
            channels=CHANNELS,
            audio_data=audio_bytes
        )

        try:
            rigctld_sock.sendall(frame)
            tag = "SILENCE" if is_silence else "audio"
            print(f"[SIDECAR] Sent TX_AUDIO frame: {len(samples)} samples ({tag}, peak={peak} rms={rms:.0f}, buf_avail_was={buf_avail})")
        except Exception as e:
            print(f"[SIDECAR] Failed to send TX audio: {e}")
            break

    print("[SIDECAR] tx_sender thread stopped")


def keepalive():
    """
    Thread: Maintain persistent CAT connection to keep rigctld audio flowing.

    rigctld's audio thread only runs when a CAT client is connected. This maintains
    a persistent connection and sends periodic keepalive commands.
    """
    print("[SIDECAR] keepalive thread started")

    cat_sock = None
    while running:
        try:
            if cat_sock is None:
                # Connect to CAT port (not audio port)
                cat_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                cat_sock.settimeout(2)
                cat_sock.connect((rigctld_host, 4532))
                print("[SIDECAR] keepalive CAT connection established")

            # Send frequency query every 1 second to keep audio flowing
            # rigctld only streams audio while actively processing commands
            time.sleep(1)
            cat_sock.sendall(b"f\n")
            response = cat_sock.recv(1024)  # Read response to keep protocol happy

            # Only log every 30 pings to avoid spam
            if int(time.time()) % 30 == 0:
                print(f"[SIDECAR] keepalive active (response: {response.decode().strip()[:20]}...)")

        except Exception as e:
            print(f"[SIDECAR] keepalive error: {e}, reconnecting...")
            if cat_sock:
                try:
                    cat_sock.close()
                except:
                    pass
                cat_sock = None
            time.sleep(5)

    if cat_sock:
        try:
            cat_sock.close()
        except:
            pass

    print("[SIDECAR] keepalive thread stopped")


def rigctld_reader():
    """
    Thread: Read TCI frames from rigctld socket.

    Handles three frame types:
    - RX_AUDIO_STREAM: Add samples to rx_buf for pacat_writer
    - TX_CHRONO: Set tx_chrono_pending to trigger tx_sender
    - TX_AUDIO_STREAM: Ignore (we send these, should not receive)

    Auto-reconnects if rigctld closes the connection.
    """
    global rigctld_sock, rx_buf, tx_chrono_pending, rigctld_host, rigctld_port

    print("[SIDECAR] rigctld_reader thread started")

    while running:
        try:
            # Read frame (expect 1088 bytes for RX_AUDIO with 512 int16 samples)
            data = rigctld_sock.recv(8192)
            if not data:
                print("[SIDECAR] rigctld connection closed, reconnecting...")

                # Attempt to reconnect
                try:
                    rigctld_sock.close()
                except:
                    pass

                # Wait a bit before reconnecting
                time.sleep(1)

                # Reconnect
                try:
                    rigctld_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    rigctld_sock.connect((rigctld_host, rigctld_port))
                    print("[SIDECAR] Reconnected to rigctld")
                    continue
                except Exception as e:
                    print(f"[SIDECAR] Reconnection failed: {e}")
                    time.sleep(5)
                    continue

            frame = parse_tci_frame(data)
            if not frame:
                print(f"[SIDECAR] Invalid frame (size={len(data)})")
                continue

            stream_type = frame['stream_type']

            if stream_type == STREAM_TYPE_RX_AUDIO:
                # Decode audio based on format
                audio_data = frame['audio_data']

                if frame['format'] == FORMAT_INT16:
                    samples = np.frombuffer(audio_data, dtype=np.int16)
                elif frame['format'] == FORMAT_FLOAT32:
                    # Convert float32 [-1.0, 1.0] to int16
                    floats = np.frombuffer(audio_data, dtype=np.float32)
                    samples = (floats * 32767).astype(np.int16)
                else:
                    print(f"[SIDECAR] Unsupported RX format: {frame['format']}")
                    continue

                # Apply --rx-gain-db (default 0 dB = unity).  Skipping the
                # multiply when unity keeps RX cheap.
                if RX_GAIN_LIN != 1.0:
                    samples = np.clip(
                        samples.astype(np.float32) * RX_GAIN_LIN,
                        -32768, 32767,
                    ).astype(np.int16)

                # Add to RX buffer
                with rx_lock:
                    rx_buf.extend(samples.tolist())

                    # Limit buffer to 4 seconds
                    max_samples = SAMPLE_RATE * 4
                    if len(rx_buf) > max_samples:
                        del rx_buf[:len(rx_buf) - max_samples]  # Keep last max_samples

                # Debug: Show sample statistics
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                peak = int(np.max(np.abs(samples)))
                nonzero = int(np.count_nonzero(samples))
                first_10 = samples[:10].tolist()

                print(f"[SIDECAR] RX_AUDIO: {len(samples)} samples (buf={len(rx_buf)}) "
                      f"RMS={rms:.1f} Peak={peak} NonZero={nonzero} First10={first_10}")

            elif stream_type == STREAM_TYPE_TX_CHRONO:
                # ExpertSDR3 is requesting TX audio
                samples_needed = frame['length']

                with tx_chrono_lock:
                    tx_chrono_pending = samples_needed

                print(f"[SIDECAR] TX_CHRONO: {samples_needed} samples needed")

            elif stream_type == STREAM_TYPE_TX_AUDIO:
                # We send these, should not receive them
                print("[SIDECAR] Unexpected TX_AUDIO frame received (ignoring)")

            else:
                print(f"[SIDECAR] Unknown stream type: {stream_type}")

        except Exception as e:
            print(f"[SIDECAR] rigctld_reader error: {e}")
            break

    print("[SIDECAR] rigctld_reader thread stopped")


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    global running
    print("\n[SIDECAR] Shutdown signal received")
    running = False


def main():
    global running, rigctld_sock, rigctld_host, rigctld_port, pacat_proc, parec_proc

    parser = argparse.ArgumentParser(description='TCI Audio Sidecar')
    parser.add_argument('--rigctld-host', default='localhost',
                       help='rigctld audio socket host')
    parser.add_argument('--rigctld-port', type=int, default=4534,
                       help='rigctld audio socket port')
    parser.add_argument('--name', default='tci',
                       help='Base name for audio devices (creates NAME-rx, NAME-tx)')
    parser.add_argument('--tx-gain-db', type=float, default=20.0,
                       help='TX audio gain in dB.  Applied to samples coming from '
                            'NAME-tx.monitor before they reach ExpertSDR3.  Default 20 dB '
                            '(=10x linear) compensates for JS8Call/WSJT-X output '
                            'level (~ -20 dBFS) which ExpertSDR3 ignores.  Clipping '
                            'protection is automatic.')
    parser.add_argument('--rx-gain-db', type=float, default=0.0,
                       help='RX audio gain in dB.  Applied to samples received from '
                            'rigctld before they reach the NAME-rx PulseAudio sink.  '
                            'Default 0 dB (=unity).  Use a small positive value if '
                            'your application reads RX too quietly, or a negative value '
                            'to attenuate.  Clipping protection is automatic.')
    args = parser.parse_args()

    # Convert dB to linear gain factors once so the hot path is just a multiply.
    global TX_GAIN_LIN, RX_GAIN_LIN
    TX_GAIN_LIN = 10.0 ** (args.tx_gain_db / 20.0)
    RX_GAIN_LIN = 10.0 ** (args.rx_gain_db / 20.0)
    print(f"[SIDECAR] TX gain {args.tx_gain_db:+.1f} dB ({TX_GAIN_LIN:.3f}x)")
    print(f"[SIDECAR] RX gain {args.rx_gain_db:+.1f} dB ({RX_GAIN_LIN:.3f}x)")

    # Verify audio tooling and a running PA-compatible server are available
    # before we touch anything.
    check_dependencies()

    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Store connection params for reconnection
    rigctld_host = args.rigctld_host
    rigctld_port = args.rigctld_port

    try:
        # Create audio devices
        create_audio_devices(args.name)

        # Connect to rigctld
        print(f"[SIDECAR] Connecting to rigctld at {rigctld_host}:{rigctld_port}")
        rigctld_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rigctld_sock.connect((rigctld_host, rigctld_port))
        print("[SIDECAR] Connected to rigctld")

        # Start pacat for RX playback (tci-rx)
        print(f"[SIDECAR] Starting pacat for {args.name}-rx")
        pacat_proc = subprocess.Popen(
            ['pacat', '--playback', f'--device={args.name}-rx',
             f'--rate={SAMPLE_RATE}', f'--channels={CHANNELS}',
             f'--format={FORMAT}'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

        # Start parec for TX capture (tci-tx.monitor)
        print(f"[SIDECAR] Starting parec for {args.name}-tx.monitor")
        parec_proc = subprocess.Popen(
            ['parec', f'--device={args.name}-tx.monitor',
             f'--rate={SAMPLE_RATE}', f'--channels={CHANNELS}',
             f'--format={FORMAT}'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

        # Give processes time to start
        time.sleep(0.5)

        # Check if processes are alive
        if pacat_proc.poll() is not None:
            raise RuntimeError("pacat exited immediately")
        if parec_proc.poll() is not None:
            raise RuntimeError("parec exited immediately")

        # Start worker threads
        print("[SIDECAR] Starting worker threads")
        threading.Thread(target=rigctld_reader, daemon=True).start()
        threading.Thread(target=pacat_writer, daemon=True).start()
        threading.Thread(target=parec_reader, daemon=True).start()
        threading.Thread(target=tx_sender, daemon=True).start()
        threading.Thread(target=keepalive, daemon=True).start()

        print("[SIDECAR] Ready - audio bridge active")
        print(f"[SIDECAR] RX audio: {args.name}-rx.monitor")
        print(f"[SIDECAR] TX audio: {args.name}-tx")

        # Main loop - just wait
        while running:
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[SIDECAR] Interrupted")
    except Exception as e:
        print(f"[SIDECAR] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("[SIDECAR] Shutting down")
        running = False

        # Close socket
        if rigctld_sock:
            try:
                rigctld_sock.close()
            except:
                pass

        # Terminate audio processes
        for proc in [pacat_proc, parec_proc]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except:
                    proc.kill()

        # Cleanup audio devices
        cleanup_audio_devices()

        print("[SIDECAR] Shutdown complete")


if __name__ == '__main__':
    main()
