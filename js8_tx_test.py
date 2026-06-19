#!/usr/bin/env python3
"""Drive a JS8Call TX via the legacy TCP API and capture whether RF actually
appears on the SSA3032X.

Connects to JS8Call on localhost:2442, calls send_message("@HB HB"), waits
for the TX cycle to start, runs the SSA monitor in parallel, reports both
PA sink-input activity and RF detection.
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/jfrancis/Dropbox/build/js8net-legacy")
from js8net_legacy import (
    start_net, get_freq, set_freq, get_callsign, set_speed,
    send_message, queue_message, set_tx_text,
)


def ssa_monitor(center_hz, span_hz, duration_sec, log_path):
    """Run the SSA marker-peak watch in this thread; write each sample to log_path."""
    s = socket.socket()
    s.settimeout(10)
    s.connect(("10.1.1.60", 5025))

    def cmd(c):
        s.sendall((c + "\n").encode())

    def q(c):
        cmd(c)
        return s.recv(65536).decode().strip()

    cmd(":POW:ATT 0")
    cmd(":POW:GAIN ON")
    cmd(f":FREQ:CENT {int(center_hz)}")
    cmd(f":FREQ:SPAN {int(span_hz)}")
    cmd(":BAND 100")
    cmd(":BAND:VID 100")
    cmd(":CALC:MARK1:STAT ON")
    time.sleep(0.5)

    samples = []
    with open(log_path, "w") as f:
        f.write(f"# SSA monitor center={center_hz} span={span_hz}\n")
        start = time.time()
        while time.time() - start < duration_sec:
            cmd(":CALC:MARK1:MAX")
            try:
                fr = float(q(":CALC:MARK1:X?"))
                amp = float(q(":CALC:MARK1:Y?"))
            except Exception as e:
                f.write(f"# err: {e}\n")
                break
            t = time.time() - start
            line = f"{t:.2f}\t{fr/1e6:.5f}\t{amp:.1f}"
            f.write(line + "\n")
            f.flush()
            samples.append((t, fr, amp))
            time.sleep(0.5)
    s.close()
    return samples


def pa_watcher(stop_evt, log_path):
    """Snapshot PulseAudio sink-input list every 0.5s during the test window."""
    with open(log_path, "w") as f:
        start = time.time()
        while not stop_evt.is_set():
            try:
                out = subprocess.check_output(
                    ["pactl", "list", "short", "sink-inputs"],
                    text=True, timeout=2,
                )
            except Exception as e:
                out = f"<err {e}>"
            t = time.time() - start
            f.write(f"--- t={t:.2f} ---\n")
            f.write(out)
            f.write("\n")
            f.flush()
            time.sleep(0.5)


def cat_state_watcher(stop_evt, log_path):
    """Poll rigctld every 0.5s for PTT, RFPOWER_METER, and SWR during the test."""
    with open(log_path, "w") as f:
        start = time.time()
        while not stop_evt.is_set():
            t = time.time() - start
            try:
                p = subprocess.run(
                    ["bash", "-c", "(echo t; echo l RFPOWER_METER; echo l SWR; sleep 0.1) | nc -w 1 localhost 4532"],
                    capture_output=True, text=True, timeout=2,
                )
                f.write(f"t={t:.2f}\n{p.stdout}\n")
                f.flush()
            except Exception as e:
                f.write(f"t={t:.2f} err {e}\n")
                f.flush()
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="@HB HB",
                        help="JS8Call message text (default: heartbeat)")
    parser.add_argument("--duration", type=float, default=40.0,
                        help="Total monitor duration, seconds (default 40)")
    parser.add_argument("--center", type=float, default=14079e3,
                        help="SSA center freq Hz (default 14.079 MHz = 14.078 dial + 1 kHz audio)")
    parser.add_argument("--span", type=float, default=10e3,
                        help="SSA span Hz (default 10 kHz)")
    parser.add_argument("--logdir", default="/tmp",
                        help="Where to write logs")
    parser.add_argument("--no-tx", action="store_true",
                        help="Run monitor only; don't trigger TX")
    args = parser.parse_args()

    print(f"Connecting to JS8Call API at localhost:2442 ...")
    start_net("localhost", 2442)
    time.sleep(2)

    cs = get_callsign()
    print(f"  callsign: {cs}")
    f = get_freq()
    print(f"  freq: {f}")

    # Make sure dial is at 14.078 MHz with audio offset 1000 (=> 14.079 USB)
    print("Setting dial 14.078 MHz, audio offset 1000 Hz")
    set_freq(14078000, 1000)
    time.sleep(1.0)
    print(f"  freq now: {get_freq()}")
    set_speed(0)  # normal speed, ~15 sec frame

    stop = threading.Event()
    pa_log = os.path.join(args.logdir, "pa_watch.log")
    cat_log = os.path.join(args.logdir, "cat_watch.log")
    ssa_log = os.path.join(args.logdir, "ssa_watch.log")

    t_pa = threading.Thread(target=pa_watcher, args=(stop, pa_log), daemon=True)
    t_cat = threading.Thread(target=cat_state_watcher, args=(stop, cat_log), daemon=True)
    t_pa.start()
    t_cat.start()

    print(f"\nStarting SSA monitor for {args.duration}s (output: {ssa_log})")
    if not args.no_tx:
        # Trigger TX after a short delay so monitor is up
        def tx_after_delay():
            time.sleep(3)
            print(f"\n>>> queueing JS8 message: {args.message!r}")
            send_message(args.message)
            print("    (TX will start at next 15-second slot boundary)")
        threading.Thread(target=tx_after_delay, daemon=True).start()

    samples = ssa_monitor(args.center, args.span, args.duration, ssa_log)

    stop.set()
    time.sleep(1)

    # Report
    print("\n=== RESULTS ===")
    if samples:
        max_t, max_f, max_a = max(samples, key=lambda s: s[2])
        above_50 = sum(1 for _, _, a in samples if a > -50)
        above_70 = sum(1 for _, _, a in samples if a > -70)
        print(f"SSA samples: {len(samples)}")
        print(f"  highest peak: {max_a:.1f} dBm @ {max_f/1e6:.4f} MHz at t={max_t:.1f}s")
        print(f"  samples above -50 dBm: {above_50}")
        print(f"  samples above -70 dBm: {above_70}")
        if above_50 > 0:
            print("  *** RF DETECTED ***")
        else:
            print("  *** NO RF ABOVE -50 dBm ***")
    print(f"\nlogs: {ssa_log} {pa_log} {cat_log}")


if __name__ == "__main__":
    main()
