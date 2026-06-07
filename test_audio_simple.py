#!/usr/bin/env python3
"""
Minimal test: Connect to rigctld audio socket and dump received frames.
No PulseAudio, no threads - just raw audio reception to prove the connection works.
"""
import socket
import struct
import sys
import time

STREAM_TYPE_RX_AUDIO = 1
STREAM_TYPE_TX_AUDIO = 2
STREAM_TYPE_TX_CHRONO = 3

def parse_tci_frame(data):
    """Parse TCI binary frame header."""
    if len(data) < 64:
        return None

    header = struct.unpack('<8I', data[:32])
    return {
        'receiver': header[0],
        'sample_rate': header[1],
        'format': header[2],
        'length': header[5],
        'stream_type': header[6],
        'channels': header[7],
        'audio_data': data[64:]
    }

def main():
    print("=== Simple Audio Test ===")
    print("Connecting to rigctld at localhost:4534...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('127.0.0.1', 4534))
    print("Connected!")

    frame_count = 0
    start_time = time.time()

    try:
        while True:
            # Read TCI frame (expect 1088 bytes for RX_AUDIO)
            data = sock.recv(8192)
            if not data:
                print("Connection closed by rigctld")
                break

            frame = parse_tci_frame(data)
            if not frame:
                print(f"Invalid frame (size={len(data)})")
                continue

            frame_count += 1

            if frame['stream_type'] == STREAM_TYPE_RX_AUDIO:
                # Calculate some stats
                import array
                samples = array.array('h', frame['audio_data'][:frame['length']*2])
                rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
                peak = max(abs(s) for s in samples)
                nonzero = sum(1 for s in samples if s != 0)

                print(f"RX_AUDIO #{frame_count}: {len(samples)} samples, "
                      f"RMS={rms:.1f}, Peak={peak}, NonZero={nonzero}/{len(samples)}")

                # Stop after 20 frames to verify it's working
                if frame_count >= 20:
                    elapsed = time.time() - start_time
                    print(f"\n=== SUCCESS ===")
                    print(f"Received {frame_count} frames in {elapsed:.1f}s")
                    print(f"Rate: {frame_count/elapsed:.1f} frames/sec")
                    print(f"Audio is flowing correctly from rigctld!")
                    break

            elif frame['stream_type'] == STREAM_TYPE_TX_CHRONO:
                print(f"TX_CHRONO: {frame['length']} samples requested")

            else:
                print(f"Unknown stream type: {frame['stream_type']}")

    except KeyboardInterrupt:
        print("\n\nInterrupted")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sock.close()
        print("Socket closed")

if __name__ == '__main__':
    main()
