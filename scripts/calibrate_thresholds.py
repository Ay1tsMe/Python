#! /usr/bin/python3

"""Automatically calibrate LiBoard photoresistor thresholds via USB.

Each '?' sent to the board returns one CSV line of 64 ADC values.
We take averages for empty and automatically detect when a piece is placed,
wait 1 second to capture the lowest (occupied) value, then print a THRESHOLD[64].
"""

import argparse
import statistics
import time
import numpy
import sounddevice
from typing import List

# Sound generation settings
fs = 44100 # sample rate
duration = 0.2
frequency = 392 # Hz

samples = numpy.sin(2 * numpy.pi * numpy.arange(fs * duration) * frequency / fs)

try:
    import serial  # pyserial
except ImportError:
    raise SystemExit("This script requires pyserial. Install with:  pip install pyserial")

FILES = ['A','B','C','D','E','F','G','H']
RANKS = ['1','2','3','4','5','6','7','8']
SQUARES = [f"{f}{r}" for r in RANKS for f in FILES]  # A1..H8 order


def _read_snapshot(ser: serial.Serial, timeout_s: float = 1.0, retries: int = 3) -> List[int]:
    """Request one CSV snapshot ('?') and parse 64 ints, retrying if needed."""
    for attempt in range(retries):
        try:
            ser.reset_input_buffer()
            ser.write(b'?')
            ser.flush()

            end = time.time() + timeout_s
            while time.time() < end:
                line = ser.readline()
                if not line:
                    continue
                try:
                    text = line.decode('ascii', errors='strict').strip()
                except UnicodeDecodeError:
                    continue
                parts = text.split(',')
                if len(parts) != 64:
                    continue
                try:
                    vals = [int(p) for p in parts]
                except ValueError:
                    continue
                return vals
        except Exception:
            pass  # ignore transient serial errors
        print("  [!] CSV read timeout — retrying...")
        time.sleep(1.0)

    raise TimeoutError("Failed to get CSV snapshot after multiple retries")


def _average_readings(ser: serial.Serial, samples: int, delay_s: float) -> List[int]:
    """Average N snapshots."""
    buckets = [[] for _ in range(64)]
    for _ in range(samples):
        vals = _read_snapshot(ser)
        for i, v in enumerate(vals):
            buckets[i].append(v)
        time.sleep(delay_s)
    return [int(statistics.mean(b)) for b in buckets]


def _wait_for_piece(ser: serial.Serial, idx: int, baseline: int, drop_threshold: int = 50) -> int:
    """Wait for the ADC value at square `idx` to drop significantly from baseline, then sample for 1s."""
    print("  Waiting for piece placement...")
    while True:
        vals = _read_snapshot(ser)
        v = vals[idx]
        if baseline - v > drop_threshold:
            print("  Piece detected, waiting 1s to stabilize...")
            t_end = time.time() + 1.0
            lowest = v
            while time.time() < t_end:
                vals2 = _read_snapshot(ser)
                v2 = vals2[idx]
                if v2 < lowest:
                    lowest = v2
                time.sleep(0.05)
            return lowest
        time.sleep(0.05)


def _wait_for_removal(ser: serial.Serial, idx: int, baseline: int, tolerance: int = 30):
    """Wait until the ADC value returns near baseline (piece removed)."""
    while True:
        vals = _read_snapshot(ser)
        v = vals[idx]
        if abs(v - baseline) < tolerance:
            return
        time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='Serial port the board is connected to')
    parser.add_argument('-b', '--baud-rate', default=9600, type=int,
                        help="Board's baud rate")
    parser.add_argument('--samples', default=15, type=int,
                        help='Snapshots to average for empty baseline')
    parser.add_argument('--delay', default=0.05, type=float,
                        help='Delay between snapshots (s)')
    args = parser.parse_args()

    print(f"Connecting to LiBoard on {args.port} at {args.baud_rate} baud...")
    try:
        ser = serial.Serial(args.port, args.baud_rate, timeout=1)
    except serial.SerialException as e:
        raise SystemExit(f"Could not open port {args.port}: {e}")

    time.sleep(2.0)
    input("\nMake sure the board is COMPLETELY EMPTY, then press Enter...")
    print("Collecting baseline (unoccupied) readings...")
    empty = _average_readings(ser, args.samples, args.delay)
    print("Baseline captured.\n")
    sounddevice.play(samples, fs)
    sounddevice.wait()

    occupied = [0] * 64
    thresholds = [0] * 64

    for i, sq in enumerate(SQUARES):
        print(f"\n=== Square {sq} ===")
        occ_val = _wait_for_piece(ser, i, empty[i])
        occupied[i] = occ_val
        sounddevice.play(samples, fs)
        sounddevice.wait()
        print(f"  Empty: {empty[i]:>4} | Occupied: {occupied[i]:>4}")

        print("  Waiting for removal...")
        _wait_for_removal(ser, i, empty[i])

        hi, lo = max(empty[i], occupied[i]), min(empty[i], occupied[i])
        thresholds[i] = int((hi + lo) / 2)

    print("\nCalibration complete!\n")
    print("// Paste this into your firmware (.ino):")
    print("unsigned short THRESHOLD[64] = {")
    for r in range(8):
        row = ', '.join(f"{t:4}" for t in thresholds[r*8:(r+1)*8])
        print(f"  // Rank {r+1}\n  {row},")
    print("};")

    ser.close()
    print("\nAll done.")


if __name__ == '__main__':
    main()
