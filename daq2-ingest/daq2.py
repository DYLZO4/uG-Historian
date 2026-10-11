"""DAQ2 packet decoding and InfluxDB line protocol, kept free of I/O so tests can use it.

Wire format: daq_packet_t in uG-Power-DAQ app/daq/daq_task.h. A 128-byte little-endian header,
then frames x channels float32 values, rows packed at `channels` stride. Column c of frame i was
sampled at first_time_ns + ((i * period_q32_ns) >> 32) + offset_ns[c], in PTP (TAI) time.
"""
import struct
from dataclasses import dataclass

import numpy as np

MAGIC = 0x32514144  # "DAQ2"
HEADER_BYTES = 128
MAX_CHANNELS = 8

FLAG_PTP_LOCKED = 1
FLAG_TIME_VALID = 2
FLAG_APERTURE_CALIBRATED = 4

# daq_signal_t, the meaning of each value_ids entry
VA, VB, VC, IA, IB, IC, VN, IN = range(8)
UNUSED = 255

# Signals grouped into one point per frame. Each group shares a sampling instant (one ADC slot),
# so its timestamp is exact for every field in it.
GROUPS = (
    ("voltage", ((VA, "a"), (VB, "b"), (VC, "c"))),
    ("current", ((IA, "a"), (IB, "b"), (IC, "c"))),
    ("neutral", ((VN, "v"), (IN, "i"))),
)

_HEADER = struct.Struct("<IHHQQQQHHIIIIIHBBIHH8B8IIII")
assert _HEADER.size == HEADER_BYTES


class DecodeError(ValueError):
    pass


@dataclass
class Packet:
    sequence: int
    first_output_index: int
    first_time_ns: int        # TAI
    period_q32_ns: int        # frame period in ns * 2^32
    frames: int
    channels: int
    flags: int
    ptp_epoch: int
    uncertainty_ns: int
    config_epoch: int
    neutral_mode: int
    value_ids: tuple
    offset_ns: tuple
    values: np.ndarray        # float32, shape (frames, channels)

    @property
    def locked(self):
        return bool(self.flags & FLAG_PTP_LOCKED)

    @property
    def time_valid(self):
        return bool(self.flags & FLAG_TIME_VALID)

    def frame_times(self, tai_utc_offset_ns):
        """UTC ns of column 0 for each frame, as int64."""
        i = np.arange(self.frames, dtype=np.uint64)
        rel = (i * np.uint64(self.period_q32_ns)) >> np.uint64(32)
        return (rel + np.uint64(self.first_time_ns - tai_utc_offset_ns)).astype(np.int64)

    def column(self, signal):
        """Index of signal in the value rows, or None if this packet doesn't carry it."""
        try:
            col = self.value_ids.index(signal)
        except ValueError:
            return None
        return col if col < self.channels else None


def decode(payload):
    if len(payload) < HEADER_BYTES:
        raise DecodeError(f"short packet: {len(payload)} bytes")
    h = _HEADER.unpack_from(payload)
    (magic, version, header_bytes, sequence, first_index, first_time_ns, period_q32_ns,
     _decimation, frames, flags, ptp_epoch, uncertainty_ns, config_epoch, _raw_hz,
     channels, _ranks, neutral_mode, _rank_ticks, _accumulation, _reserved) = h[:20]
    value_ids = h[20:28]
    offset_ns = h[28:36]
    if magic != MAGIC:
        raise DecodeError(f"bad magic 0x{magic:08x}")
    if version != 2 or header_bytes != HEADER_BYTES:
        raise DecodeError(f"unsupported version {version} / header {header_bytes}")
    if not 0 < channels <= MAX_CHANNELS:
        raise DecodeError(f"bad channel count {channels}")
    expected = HEADER_BYTES + frames * channels * 4
    if len(payload) != expected:
        raise DecodeError(f"length {len(payload)} != {expected} for {frames}x{channels}")
    values = np.frombuffer(payload, dtype="<f4", offset=HEADER_BYTES).reshape(frames, channels)
    return Packet(sequence, first_index, first_time_ns, period_q32_ns, frames, channels, flags,
                  ptp_epoch, uncertainty_ns, config_epoch, neutral_mode, value_ids, offset_ns,
                  values)


def escape_tag(value):
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def group_columns(packet):
    """(measurement, [(column, field)], offset_ns) for each group this packet carries."""
    out = []
    for measurement, members in GROUPS:
        cols = [(packet.column(sig), field) for sig, field in members]
        cols = [(c, f) for c, f in cols if c is not None]
        if cols:
            out.append((measurement, cols, packet.offset_ns[cols[0][0]]))
    return out


def raw_lines(packet, node_id, tai_utc_offset_ns, decimals=3, integer=False):
    """One line per group per frame, each at its group's own sampling time.

    integer=True writes values as integers in units of 10^-decimals (e.g. mV and mA for 3), which
    InfluxDB compresses far better than floats; the precision is the same either way.
    """
    tag = escape_tag(node_id)
    times = packet.frame_times(tai_utc_offset_ns)
    lines = []
    for measurement, cols, offset in group_columns(packet):
        prefix = f"{measurement},node_id={tag} "
        block = packet.values[:, [c for c, _ in cols]]
        if integer:
            fmt = ",".join(f"{field}={{}}i" for _, field in cols)
            block = np.rint(block.astype(np.float64) * 10 ** decimals).astype(np.int64).tolist()
        else:
            fmt = ",".join(f"{field}={{:.{decimals}f}}" for _, field in cols)
            block = block.tolist()
        stamps = (times + offset).tolist()
        lines.extend(prefix + fmt.format(*row) + f" {t}" for row, t in zip(block, stamps))
    return lines


def packet_line(packet, node_id, tai_utc_offset_ns):
    """Per-packet metadata: lock state, timing quality and continuity."""
    t = packet.first_time_ns - tai_utc_offset_ns
    return (f"daq2_packet,node_id={escape_tag(node_id)} "
            f"sequence={packet.sequence}i,first_index={packet.first_output_index}i,"
            f"frames={packet.frames}i,locked={int(packet.locked)}i,"
            f"time_valid={int(packet.time_valid)}i,ptp_epoch={packet.ptp_epoch}i,"
            f"config_epoch={packet.config_epoch}i,uncertainty_ns={packet.uncertainty_ns}i {t}")


class SecondAggregator:
    """Per-second RMS/mean/min/max for each channel of one node, aligned to UTC seconds.

    A second is emitted once a frame from a later second arrives, so it covers every frame the
    node published for it. Packets from one node arrive in order (one TCP connection), so a second
    never receives frames after it has been emitted, except across a restart, which starts over.
    """

    def __init__(self, node_id):
        self.tag = escape_tag(node_id)
        self.current = None   # UTC second being accumulated
        self.acc = {}         # (measurement, field) -> [count, sum, sumsq, min, max]
        self.locked = True

    def add(self, packet, tai_utc_offset_ns):
        """Adds a packet's frames; returns lines for any seconds completed by it."""
        out = []
        times = packet.frame_times(tai_utc_offset_ns)
        seconds = times // 1_000_000_000
        # Split the packet where the second changes; usually it doesn't, or does once
        edges = np.flatnonzero(np.diff(seconds)) + 1
        for chunk in np.split(np.arange(packet.frames), edges):
            if not len(chunk):
                continue
            sec = int(seconds[chunk[0]])
            if self.current is not None and sec != self.current:
                out.extend(self._emit())
            if self.current != sec:
                self.current, self.acc, self.locked = sec, {}, True
            self.locked &= packet.locked
            for measurement, cols, _ in group_columns(packet):
                block = packet.values[chunk][:, [c for c, _ in cols]].astype(np.float64)
                for j, (_, field) in enumerate(cols):
                    x = block[:, j]
                    a = self.acc.get((measurement, field))
                    if a is None:
                        self.acc[(measurement, field)] = [len(x), x.sum(), (x * x).sum(),
                                                          x.min(), x.max()]
                    else:
                        a[0] += len(x); a[1] += x.sum(); a[2] += (x * x).sum()
                        a[3] = min(a[3], x.min()); a[4] = max(a[4], x.max())
        return out

    def _emit(self):
        fields, frames = {}, {}
        for (measurement, field), (n, s, ss, lo, hi) in self.acc.items():
            fields.setdefault(measurement, []).append(
                f"{field}_rms={np.sqrt(ss / n):.4f},{field}_mean={s / n:.4f},"
                f"{field}_min={lo:.3f},{field}_max={hi:.3f}")
            frames[measurement] = n  # same for every field of a measurement
        t = self.current * 1_000_000_000
        return [f"{m},node_id={self.tag} {','.join(f)},frames={frames[m]}i,"
                f"locked={int(self.locked)}i {t}"
                for m, f in fields.items()]
