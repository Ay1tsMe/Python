

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
import serial
from typing import List

# Sound generation settings
fs = 44100 # sample rate
duration = 0.2
frequency = 392 # Hz

samples = numpy.sin(2 * numpy.pi * numpy.arange(fs * duration) * frequency / fs)

FILES = ['A','B','C','D','E','F','G','H']
RANKS = ['1','2','3','4','5','6','7','8']
SQUARES = [f"{f}{r}" for f in FILES for r in RANKS]  # A1..A8 order labels

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

def _read_thresholds(ser: serial.Serial, timeout_s: float = 1.0, retries: int = 3):
    """Request current thresholds with '!' and parse either 1 (global) or 64 (per-square) ints.
       Returns (values, is_global)."""
    for attempt in range(retries):
        try:
            ser.reset_input_buffer()
            ser.write(b'!')
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

                parts = [p for p in text.split(',') if p != '']
                # Accept either a single integer (global) or 64 integers (per-square)
                if len(parts) not in (1, 64):
                    continue

                try:
                    vals = [int(p) for p in parts]
                except ValueError:
                    continue

                is_global = (len(vals) == 1)
                return vals, is_global
        except Exception:
            pass
        print("  [!] Threshold read timeout — retrying...")
        time.sleep(1.0)

    raise TimeoutError("Failed to get thresholds after multiple retries")

def _average_readings(ser: serial.Serial, samples: int, delay_s: float) -> List[int]:
    """Average N snapshots."""
    buckets = [[] for _ in range(64)]
    for _ in range(samples):
        vals = _read_snapshot(ser)
        for i, v in enumerate(vals):
            buckets[i].append(v)
        time.sleep(delay_s)
    return [int(statistics.mean(b)) for b in buckets]

def push_threshold_global(ser: serial.Serial, value: int):
    """Send a global threshold value to the LiBoard via its calibration mode."""
    try:
        ser.reset_input_buffer()
        ser.write(b'c')                 # tell board to enter calibration mode
        ser.flush()
        time.sleep(0.1)                 # small delay to let it switch

        ser.write(f"{value}\n".encode("ascii"))  # send threshold value
        ser.flush()
        time.sleep(0.2)                 # brief wait for Arduino to finish
        print(f"\n[OK] Pushed global threshold {value} to board.")
    except Exception as e:
        print(f"\n[WARN] Failed to push threshold to board: {e}")


def push_threshold_individual(ser: serial.Serial, values: List[int]):
    """Apply individual thresholds to the LiBoard via its calibration mode."""

    if len(values) != 64:
        raise ValueError("Need exactly 64 threshold values")

    # Format: plain CSV (no brackets/spaces required; spaces tolerated by Arduino)
    csv_line = ",".join(str(int(v)) for v in values) + "\n"

    try:
        ser.reset_input_buffer()
        ser.write(b'c')                 # tell board to enter calibration mode
        ser.flush()
        time.sleep(0.1)                 # small delay to let it switch

        ser.write(csv_line.encode("ascii"))  # send threshold value
        ser.flush()
        time.sleep(0.2)                 # brief wait for Arduino to finish
        print("\n[OK] Pushed individual thresholds to board.")
    except Exception as e:
        print(f"\n[WARN] Failed to push threshold to board: {e}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='Serial port the board is connected to')
    parser.add_argument('-b', '--baud-rate', default=9600, type=int,
                        help="Board's baud rate")
    parser.add_argument('-s', '--squares', default='',
                        help='Comma-separated squares to calibrate (e.g. "a1,c4,d5"). If omitted, calibrates by ranks (A1–H1, A2–H2, ...).')
    args = parser.parse_args()

    # Defaults for averaging snapshots
    n_samples = 15
    delay_s = 0.05

    print(f"Connecting to LiBoard on {args.port} at {args.baud_rate} baud...")
    try:
        ser = serial.Serial(args.port, args.baud_rate, timeout=1)
    except serial.SerialException as e:
        raise SystemExit(f"Could not open port {args.port}: {e}")

    time.sleep(2.0)

    # Enter quiet mode to turn of Bitboard output
    try:
        ser.reset_input_buffer()
        ser.write(b'q') 
        ser.flush()
    except Exception:
        pass

    # Detect board threshold mode
    try:
        th_vals, board_is_global = _read_thresholds(ser)
    except Exception as e:
        print(f"[WARN] Could not read current thresholds: {e}")
        th_vals, board_is_global = ([0]*64, False)  # conservative fallback

    # Apply mode based on Liboard firmware
    mode = 'g' if board_is_global else 'i'
    print(f"Detected board mode: {'GLOBAL' if board_is_global else 'PER-SQUARE'}")

    # Enforce: if board is global, disallow any individual-square calibration
    if board_is_global and args.squares.strip():
        raise SystemExit(
            "This board is configured for a single GLOBAL threshold; "
            "individual-square selection (-s) is not supported. "
            "Recompile firmware with per-square thresholds or remove -s."
        )

    input("\nMake sure the board is COMPLETELY EMPTY, then press Enter...")
    print("Collecting baseline (unoccupied) readings...")
    empty = _average_readings(ser, n_samples, delay_s)
    print("Baseline captured.\n")
    sounddevice.play(samples, fs)
    sounddevice.wait()

    occupied = [0] * 64
    # Use current thresholds programmed in Liboard
    thresholds = [th_vals[0]] * 64 if board_is_global else list(th_vals)

    # If specific squares were requested, calibrate only those, otherwise do ranks.
    if args.squares.strip():
        # --- Manual individual-square calibration ---
        raw_targets = [s.strip().upper() for s in args.squares.split(',') if s.strip()]
        seen = set()
        targets = [t for t in raw_targets if not (t in seen or seen.add(t))]

        # Validate format LETTER + rank 1..8
        def _valid_sq(t: str) -> bool:
            return len(t) >= 2 and t[0] in FILES and t[1:].isdigit() and 1 <= int(t[1:]) <= 8

        invalid = [t for t in targets if not _valid_sq(t)]
        if invalid:
            raise SystemExit(f"Invalid square(s): {', '.join(invalid)}. Use like -s a1,c4,d5")

        for sq in targets:
            file_letter = sq[0]
            rank_n = int(sq[1:])
            # rank-major index: A1,B1,...,H1, A2,B2,...,H8
            idx = (rank_n - 1) * 8 + FILES.index(file_letter)

            print(f"\n=== Square {sq} ===")
            input(f"Place a piece on {sq}, then press Enter to capture...")

            vals = _read_snapshot(ser)  # single request, manual trigger
            occupied[idx] = vals[idx]

            sounddevice.play(samples, fs)
            sounddevice.wait()

            print(f"  {sq}: Empty: {empty[idx]:>4} | Occupied: {occupied[idx]:>4}")

            input("Remove the piece, then press Enter to continue...")

            hi, lo = max(empty[idx], occupied[idx]), min(empty[idx], occupied[idx])
            thresholds[idx] = int((hi + lo) / 2)

    else:
        # --- Default: rank-by-rank calibration (A1–H1, A2–H2, ...) ---
        for f_idx, file_letter in enumerate(FILES):
            rank = f_idx + 1  # 1..8
            print(f"\n=== Rank {rank} (Squares A{rank}-H{rank}) ===")
            input(f"Place pieces on A{rank}-H{rank}, then press Enter to capture...")
            vals = _read_snapshot(ser)  # one request only

            # Indices for this rank in rank-major order
            indices = [(rank - 1) * 8 + k for k in range(8)]

            sounddevice.play(samples, fs)
            sounddevice.wait()

            # Use the single snapshot as occupied readings
            for j, idx in enumerate(indices):
                sq_label = f"{FILES[j]}{rank}"
                occupied[idx] = vals[idx]
                print(f"  {sq_label}: Empty: {empty[idx]:>4} | Occupied: {occupied[idx]:>4}")

            # ask user to clear this rank before proceeding
            input("Remove pieces from this rank, then press Enter to continue...")

            # Compute thresholds for this rank
            for idx in indices:
                hi, lo = max(empty[idx], occupied[idx]), min(empty[idx], occupied[idx])
                thresholds[idx] = int((hi + lo) / 2)

    print("\nCalibration complete!\n")

    if mode == 'g':
        # Single global threshold: average of per-square midpoints
        # (equivalently: mean of thresholds[] we computed above)
        # If we only did some squares (individual path), fall back to non-zeros.
        nonzero = [t for t in thresholds if t > 0]
        if not nonzero:
            raise SystemExit("No thresholds collected to compute a global value.")
        global_threshold = int(round(statistics.mean(nonzero)))

        print(f"\nApplying global threshold ({global_threshold}) to Liboard...")
        push_threshold_global(ser, global_threshold)
        
    else:
        # Output in A1–H1, A2–H2, … A8–H8 order
        individual_thresholds = []
        for rank in range(8):      # ranks 1–8
            for file in range(8):  # files A–H
                idx = rank * 8 + file
                individual_thresholds.append(thresholds[idx])
    
        print("\nApplying the following thresholds to Liboard...")
        print(",".join(str(v) for v in individual_thresholds))
        push_threshold_individual(ser, individual_thresholds)

    # Exit Quiet Mode to resume bitboard output
    try:
        ser.reset_input_buffer()
        ser.write(b'q')
        ser.flush()
    except Exception:
        pass

    ser.close()
    print("\nAll done.")


if __name__ == '__main__':
    main()

