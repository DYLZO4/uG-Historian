"""Publishes synthetic DAQ2 packets at the firmware's real rate, for load-testing daq2-ingest.

Each node sends 64-frame packets of three-phase 50 Hz sine at 30048 frames/s (~470 packets/s),
timestamped from this machine's clock as TAI. Not a node simulator: there is no PTP behaviour.

  python tests/load.py --host localhost --nodes 2 --seconds 60
"""
import argparse
import sys
import time

import paho.mqtt.client as mqtt

import packets

TAI_UTC_OFFSET_NS = 37_000_000_000
FRAMES = 64


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--nodes", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--prefix", default="load", help="node names are <prefix>_01, <prefix>_02, ...")
    args = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"daq2-load-{args.prefix}")
    client.max_queued_messages_set(0)
    client.connect(args.host, args.port)
    client.loop_start()

    period_ns = packets.period_q32() / 2**32
    start_ns = time.time_ns() + TAI_UTC_OFFSET_NS
    nodes = [f"{args.prefix}_{n + 1:02d}" for n in range(args.nodes)]
    sent = index = 0
    t_start = time.perf_counter()
    while time.perf_counter() - t_start < args.seconds:
        first_time = start_ns + ((index * packets.period_q32()) >> 32)
        block = packets.sine_block(index, FRAMES)
        for node in nodes:
            payload = packets.build(block, first_time, sequence=index // FRAMES, first_index=index)
            client.publish(f"daq2/{node}/samples", payload, qos=0)
            sent += 1
        index += FRAMES
        # Pace to real time: packet k is due at k * 64 frame periods after start
        due = t_start + index * period_ns / 1e9
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    elapsed = time.perf_counter() - t_start
    client.loop_stop()
    client.disconnect()
    print(f"sent {sent} packets ({index} frames per node) in {elapsed:.1f} s: "
          f"{sent / elapsed:.0f} packets/s, {index / elapsed:.0f} frames/s per node", file=sys.stderr)


if __name__ == "__main__":
    main()
