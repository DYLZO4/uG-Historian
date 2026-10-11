"""Demo DAQ2 node: realistic three-phase voltage and current plus PTP servo status, for screenshots.

Publishes `daq2/<node>/samples` at the firmware's real rate and `microgrid/<node>/ptp_status/servo`
once a second. The signal is a small commercial board (feeder.py): constant with --steady, or with
appliances switching. --backfill first publishes that many seconds of history, faster than real
time, ending at the present. Not a node simulator: there is no PTP behaviour behind the status.

  python tests/mock_node.py --host localhost --node node_02 --backfill 600 --steady fridge,pump
"""
import argparse
import json
import math
import time

import numpy as np
import paho.mqtt.client as mqtt

import packets
from feeder import SHIFT, Feeder

FRAMES = 64
FRAME_RATE = packets.TIMER_HZ / packets.FRAME_TICKS
CURRENT_LAG_S = packets.RANK_TICKS / packets.TIMER_HZ   # second ADC slot samples later



class Signal:
    """Samples the feeder model (feeder.py) as the node's six channels."""

    def __init__(self, seconds, steady=None):
        self.feeder = Feeder(seconds, steady=steady)
        self.rng = np.random.default_rng(2)
        self.theta = 0.0          # fundamental phase at the next frame

    def block(self, t0):
        f = self.feeder
        hz = f.at(f.hz, t0)
        v_rms = f.voltage_rms(t0)
        comps = f.components(t0)
        n = np.arange(FRAMES)
        w = 2 * math.pi * hz
        th_v = self.theta + w * n / FRAME_RATE
        th_i = th_v + w * CURRENT_LAG_S
        self.theta = (self.theta + w * FRAMES / FRAME_RATE) % (2 * math.pi)

        cols = []
        for k in range(3):
            a = th_v - SHIFT[k]
            # Flat-topped LV voltage: 5th in anti-phase, a little 3rd and 7th
            v = math.sqrt(2) * v_rms[k] * (np.sin(a) - 0.022 * np.sin(5 * a)
                                           + 0.007 * np.sin(3 * a + 0.4) + 0.009 * np.sin(7 * a + 0.6))
            cols.append(v + self.rng.normal(0, 0.6, FRAMES))
        for k in range(3):
            a = th_i - SHIFT[k]
            i = np.zeros(FRAMES)
            for amps, lag, harm in comps[k]:
                b = a - lag
                i += math.sqrt(2) * amps * np.sin(b)
                for h, (frac, ang) in harm.items():
                    i += math.sqrt(2) * amps * frac * np.sin(h * b + ang)
            cols.append(i + self.rng.normal(0, 0.03, FRAMES))
        return np.stack(cols, axis=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--node", default="node_02")
    ap.add_argument("--seconds", type=float, default=3600, help="real-time run length after catching up")
    ap.add_argument("--backfill", type=float, default=0, help="seconds of history to publish first")
    ap.add_argument("--speedup", type=float, default=2.0, help="publish rate while backfilling")
    ap.add_argument("--freq-ppb", type=float, default=-1843.0, help="servo frequency correction")
    ap.add_argument("--tai-utc-offset", type=float, default=37,
                    help="seconds added to packet times (TAI); 0 if the grandmaster sends UTC")
    ap.add_argument("--steady", help="comma-separated loads to run continuously, e.g. fridge,pump; "
                    "no load switching at all (see feeder.LOADS)")
    args = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mock-{args.node}")
    client.max_queued_messages_set(0)
    client.connect(args.host, args.port)
    client.loop_start()

    rng = np.random.default_rng(7)
    sig = Signal(args.backfill + args.seconds + 5, steady=args.steady.split(",") if args.steady else None)
    period_q32 = packets.period_q32()
    # Data time reaches wall-clock time just as the backfill finishes
    launch_ns = time.time_ns()
    start_ns = launch_ns - int(args.backfill * (1 - 1 / args.speedup) * 1e9)
    wall0 = time.perf_counter()
    index = seq = 0
    next_ptp = 0.0
    freq_ppb = args.freq_ppb
    while True:
        t_data = index / FRAME_RATE                     # seconds since start_ns
        # Real-time point for this packet; while backfilling, run at --speedup x
        if t_data < args.backfill:
            due = wall0 + t_data / args.speedup
        else:
            due = wall0 + args.backfill / args.speedup + (t_data - args.backfill)
            if t_data - args.backfill > args.seconds:
                break
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

        first_time = start_ns + ((index * period_q32) >> 32)         # UTC
        payload = packets.build(sig.block(t_data), first_time + int(args.tai_utc_offset * 1e9),
                                sequence=seq, first_index=index,
                                uncertainty_ns=int(40 + rng.integers(0, 15)))
        client.publish(f"daq2/{args.node}/samples", payload, qos=0)

        # Servo status starts at launch: backfilled points would interleave with an earlier run's.
        # Its ts is UTC, as the firmware publishes it
        if t_data >= next_ptp and first_time >= launch_ns:
            # The servo's correction wobbles a few ppb around the oscillator's fixed error
            freq_ppb += (args.freq_ppb - freq_ppb) / 120 + rng.normal(0, 0.35)
            status = {"ts": first_time, "offset_ns": int(rng.normal(0, 28)),
                      "path_delay_ns": int(rng.normal(612, 6)), "freq_ppb": int(freq_ppb),
                      "state": 2, "locked": 1, "stepped": 0}
            client.publish(f"microgrid/{args.node}/ptp_status/servo", json.dumps(status), qos=0)
        if t_data >= next_ptp:
            next_ptp += 1.0

        index += FRAMES
        seq += 1
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
