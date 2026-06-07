#!/usr/bin/env python3
"""
TX test client - Generate and transmit 1 kHz tone via TCI

This script:
1. Connects to rigctld CAT port (4532)
2. Sets radio to 14.078 MHz USB
3. Generates 1 kHz tone
4. Plays tone to tci-tx virtual audio device
5. Triggers PTT via CAT
6. Monitors for TX_CHRONO and TX_AUDIO flow
7. PTT off after transmission

The tone will appear at 14.078 MHz + 1 kHz = 14.079 MHz in USB mode
"""
import argparse
import numpy as np
import socket
import subprocess
import sys
import time
import wave

def generate_tone(freq_hz=1000, duration_sec=5, sample_rate=8000):
    """
    Generate a sine wave tone.

    Args:
        freq_hz: Tone frequency in Hz
        duration_sec: Duration in seconds
        sample_rate: Sample rate in Hz

    Returns:
        numpy array of int16 samples
    """
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    samples = np.sin(2 * np.pi * freq_hz * t)

    # Scale to 80% of full scale to avoid clipping
    samples = (samples * 32767 * 0.8).astype(np.int16)

    return samples

def save_wav(filename, samples, sample_rate=8000):
    """Save samples to WAV file"""
    with wave.open(filename, 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())

def send_cat_command(sock, cmd):
    """Send CAT command via rigctld and get response"""
    sock.sendall(f"{cmd}\n".encode())
    response = sock.recv(4096).decode('ascii', errors='ignore').strip()
    return response

def main():
    parser = argparse.ArgumentParser(description='TX test client for TCI sidecar')
    parser.add_argument('--freq-mhz', type=float, default=14.078,
                       help='Transmit frequency in MHz (default: 14.078)')
    parser.add_argument('--tone-freq', type=int, default=1000,
                       help='Audio tone frequency in Hz (default: 1000)')
    parser.add_argument('--duration', type=int, default=5,
                       help='TX duration in seconds (default: 5)')
    parser.add_argument('--wav-file', default='/tmp/tx_tone.wav',
                       help='WAV file path (default: /tmp/tx_tone.wav)')
    args = parser.parse_args()

    print("=" * 70)
    print("TX Test Client - 1 kHz Tone via TCI")
    print("=" * 70)
    print()

    # Generate tone
    print(f"Generating {args.tone_freq} Hz tone ({args.duration} seconds)...")
    samples = generate_tone(freq_hz=args.tone_freq, duration_sec=args.duration)
    save_wav(args.wav_file, samples)
    print(f"✓ Saved to {args.wav_file}")
    print(f"  Samples: {len(samples)}")
    print(f"  RMS: {np.sqrt(np.mean(samples.astype(np.float32) ** 2)):.1f}")
    print()

    # Connect to rigctld CAT port
    print("Connecting to rigctld CAT port (localhost:4532)...")
    try:
        cat_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cat_sock.connect(('localhost', 4532))
        print("✓ Connected")
    except ConnectionRefusedError:
        print("✗ Connection refused - is rigctld running?")
        return 1
    print()

    # Set frequency
    print(f"Setting frequency to {args.freq_mhz:.3f} MHz...")
    freq_hz = int(args.freq_mhz * 1e6)
    resp = send_cat_command(cat_sock, f"F {freq_hz}")
    print(f"  Response: {resp}")

    # Verify frequency
    resp = send_cat_command(cat_sock, "f")
    actual_freq = int(resp.split('\n')[0])
    print(f"  Actual: {actual_freq/1e6:.3f} MHz")
    print()

    # Set mode to USB
    print("Setting mode to USB...")
    resp = send_cat_command(cat_sock, "M USB 0")
    print(f"  Response: {resp}")

    # Verify mode
    resp = send_cat_command(cat_sock, "m")
    print(f"  Actual mode: {resp.split()[0]}")
    print()

    # Start playing tone to tci-tx (but don't start yet)
    print(f"Starting tone playback to tci-tx...")
    player = subprocess.Popen(
        ['paplay', '--device=tci-tx', args.wav_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    print("✓ Tone playing to tci-tx")
    print()

    # Send PTT ON
    print("=" * 70)
    print("⚡ TRIGGERING PTT ON")
    print("=" * 70)
    resp = send_cat_command(cat_sock, "T 1")
    print(f"PTT ON response: {resp}")
    print()
    print("Check:")
    print("  1. /tmp/sidecar.log for TX_CHRONO and TX_AUDIO packets")
    print("  2. /tmp/rigctld.log for TCI traffic")
    print("  3. SSA3032X at 14.079 MHz for RF signal")
    print()

    # Transmit for the duration
    print(f"Transmitting for {args.duration} seconds...")
    for i in range(args.duration):
        time.sleep(1)
        print(f"  {i+1}s...")
    print()

    # Send PTT OFF
    print("=" * 70)
    print("PTT OFF")
    print("=" * 70)
    resp = send_cat_command(cat_sock, "T 0")
    print(f"PTT OFF response: {resp}")
    print()

    # Stop player
    player.terminate()
    try:
        player.wait(timeout=2)
    except:
        player.kill()

    cat_sock.close()

    print("=" * 70)
    print("TX Test Complete")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Check sidecar log: tail -50 /tmp/sidecar.log")
    print("  2. Check rigctld log: tail -50 /tmp/rigctld.log")
    print("  3. Run monitor_tx.py to verify RF output")
    print()

    return 0

if __name__ == '__main__':
    sys.exit(main())
