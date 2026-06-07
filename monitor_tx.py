#!/usr/bin/env python3
"""
Monitor spectrum analyzer for 1 kHz tone transmission on 14.078 MHz

This script monitors the SSA3032X spectrum analyzer at 14.079 MHz (14.078 MHz + 1 kHz offset)
to detect when the radio is transmitting a 1 kHz test tone via USB modulation.

Setup:
- SSA3032X at 10.1.1.60:5025
- 2m whip antenna connected to SSA input
- Radio transmitting USB on 14.078 MHz with 1 kHz audio tone

The 1 kHz audio tone in USB mode creates a carrier at 14.078 MHz + 1 kHz = 14.079 MHz

Usage:
    ./monitor_tx.py [--threshold -40] [--duration 10]
"""
import argparse
import socket
import sys
import time

SSA_IP = "10.1.1.60"
SSA_PORT = 5025
CENTER_FREQ = 14.079e6  # 14.078 MHz + 1 kHz
SPAN = 10e3  # 10 kHz span
RBW = 100  # 100 Hz resolution bandwidth
VBW = 100  # 100 Hz video bandwidth


class SSA3000X:
    """Minimal SSA3032X spectrum analyzer driver"""

    def __init__(self, host, port=5025):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((host, port))

    def write(self, cmd):
        """Send SCPI command"""
        self.sock.sendall(f"{cmd}\n".encode())

    def query(self, cmd):
        """Send SCPI query and read response"""
        self.write(cmd)
        return self.sock.recv(65536).decode().strip()

    def close(self):
        self.sock.close()

    def identify(self):
        """Get instrument identification"""
        return self.query("*IDN?")

    def set_center_freq(self, freq_hz):
        """Set center frequency in Hz"""
        self.write(f":FREQ:CENT {freq_hz}")

    def set_span(self, span_hz):
        """Set frequency span in Hz"""
        self.write(f":FREQ:SPAN {span_hz}")

    def set_rbw(self, rbw_hz):
        """Set resolution bandwidth in Hz"""
        self.write(f":BAND:RES {rbw_hz}")

    def set_vbw(self, vbw_hz):
        """Set video bandwidth in Hz"""
        self.write(f":BAND:VID {vbw_hz}")

    def get_marker_amplitude(self, marker=1):
        """Get marker amplitude in dBm"""
        resp = self.query(f":CALC:MARK{marker}:Y?")
        return float(resp)

    def set_marker_max(self, marker=1):
        """Set marker to peak"""
        self.write(f":CALC:MARK{marker}:MAX")

    def set_marker_center(self, marker=1):
        """Set marker to center frequency"""
        self.write(f":CALC:MARK{marker}:CENT")


def monitor_tx(threshold_dbm=-40, duration_sec=10, verbose=True):
    """
    Monitor for TX signal at 14.079 MHz

    Args:
        threshold_dbm: Signal must exceed this level to be considered TX (dBm)
        duration_sec: How long to monitor (seconds)
        verbose: Print detailed status

    Returns:
        True if TX detected, False otherwise
    """
    if verbose:
        print(f"=== TX Monitor ===")
        print(f"Connecting to SSA3032X at {SSA_IP}:{SSA_PORT}")

    ssa = SSA3000X(SSA_IP, SSA_PORT)

    if verbose:
        idn = ssa.identify()
        print(f"Connected: {idn}")

    # Configure spectrum analyzer
    if verbose:
        print(f"\nConfiguring:")
        print(f"  Center: {CENTER_FREQ/1e6:.6f} MHz")
        print(f"  Span: {SPAN/1e3:.1f} kHz")
        print(f"  RBW: {RBW} Hz")
        print(f"  VBW: {VBW} Hz")

    ssa.set_center_freq(CENTER_FREQ)
    ssa.set_span(SPAN)
    ssa.set_rbw(RBW)
    ssa.set_vbw(VBW)
    time.sleep(0.5)

    # Set marker to center frequency
    ssa.set_marker_center(1)
    time.sleep(0.5)

    if verbose:
        print(f"\nMonitoring for {duration_sec} seconds...")
        print(f"Threshold: {threshold_dbm} dBm")
        print(f"Marker at {CENTER_FREQ/1e6:.6f} MHz\n")
        print("Time   Level    Status")
        print("----   -----    ------")

    tx_detected = False
    start_time = time.time()

    try:
        while time.time() - start_time < duration_sec:
            # Read marker level
            level = ssa.get_marker_amplitude(1)

            # Check if above threshold
            is_tx = level > threshold_dbm
            if is_tx:
                tx_detected = True

            if verbose:
                elapsed = time.time() - start_time
                status = "TX DETECTED" if is_tx else "no signal"
                print(f"{elapsed:4.1f}s  {level:6.1f} dBm  {status}")

            time.sleep(0.5)

    except KeyboardInterrupt:
        if verbose:
            print("\n\nInterrupted by user")
    finally:
        ssa.close()

    if verbose:
        print(f"\n{'='*40}")
        if tx_detected:
            print("✅ TX DETECTED - Radio is transmitting!")
        else:
            print("❌ NO TX - No signal above threshold")
        print(f"{'='*40}")

    return tx_detected


def main():
    parser = argparse.ArgumentParser(
        description='Monitor spectrum analyzer for 1 kHz tone TX on 14.078 MHz')
    parser.add_argument('--threshold', type=float, default=-40,
                       help='Threshold in dBm (default: -40)')
    parser.add_argument('--duration', type=int, default=10,
                       help='Monitor duration in seconds (default: 10)')
    parser.add_argument('--quiet', action='store_true',
                       help='Suppress verbose output')
    args = parser.parse_args()

    try:
        detected = monitor_tx(
            threshold_dbm=args.threshold,
            duration_sec=args.duration,
            verbose=not args.quiet
        )
        sys.exit(0 if detected else 1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == '__main__':
    main()
