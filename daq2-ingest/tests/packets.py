"""Builds DAQ2 packets the way the firmware's process_block() does, for tests and the load generator."""
import math
import struct

import numpy as np

import daq2

# Firmware defaults: 100 MHz timer, six channels in two ADC slots, ~30 kHz frames
TIMER_HZ = 100_000_000
NOMINAL_ADDEND = 1 << 31
RANK_TICKS = 1664                    # (accumulation 22 + guard 4) * 64 ticks
FRAME_TICKS = 2 * RANK_TICKS         # two ranks without neutral: 30048 frames/s


def period_q32(frame_ticks=FRAME_TICKS, addend=NOMINAL_ADDEND):
    return frame_ticks * 20 * addend


def build(values, first_time_ns, *, sequence=0, first_index=0, flags=daq2.FLAG_PTP_LOCKED |
          daq2.FLAG_TIME_VALID, ptp_epoch=1, config_epoch=1, frame_ticks=FRAME_TICKS,
          rank_ticks=RANK_TICKS, addend=NOMINAL_ADDEND, uncertainty_ns=520):
    values = np.asarray(values, dtype="<f4")
    frames, channels = values.shape
    value_ids = [i if i < channels else daq2.UNUSED for i in range(8)]
    # Voltages in rank 0, currents rank 1, neutral rank 2 (daq_slot_signal)
    rank = [0, 0, 0, 1, 1, 1, 2, 2]
    offsets = [((rank[i] * rank_ticks * 20 * addend) >> 32) if i < channels else 0
               for i in range(8)]
    header = struct.pack(
        "<IHHQQQQHHIIIIIHBBIHH8B8IIII",
        daq2.MAGIC, 2, 128, sequence, first_index, first_time_ns,
        period_q32(frame_ticks, addend), 2 * 26, frames, flags, ptp_epoch, uncertainty_ns,
        config_epoch, 1_562_500, channels, 2 if channels == 6 else 3, 0 if channels == 6 else 1,
        rank_ticks, 22, 0, *value_ids, *offsets, frame_ticks, 0, 0)
    return header + values.tobytes()


def sine_block(first_index, frames, channels=6, hz=50.0, frame_rate=None):
    """Three-phase 230 V / 10 A sine samples for frames starting at first_index."""
    frame_rate = frame_rate or TIMER_HZ / FRAME_TICKS
    t = (first_index + np.arange(frames)) / frame_rate
    cols = []
    for k in range(channels):
        amp = 230 * math.sqrt(2) if k % 6 < 3 else 10 * math.sqrt(2)
        cols.append(amp * np.sin(2 * math.pi * hz * t - 2 * math.pi * (k % 3) / 3))
    return np.stack(cols, axis=1)
