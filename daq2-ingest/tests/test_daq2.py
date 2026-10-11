import math
import time

import numpy as np
import pytest

import daq2
import packets

TAI = 37_000_000_000
# A whole UTC second near now, in TAI (ingest skips times far from the server's clock)
T0 = (time.time_ns() // 1_000_000_000) * 1_000_000_000 + TAI


def test_decode_header_and_values():
    values = packets.sine_block(0, 64)
    p = daq2.decode(packets.build(values, T0, sequence=7, first_index=448, ptp_epoch=3))
    assert (p.sequence, p.first_output_index, p.frames, p.channels) == (7, 448, 64, 6)
    assert p.locked and p.time_valid and p.ptp_epoch == 3
    assert p.value_ids[:6] == (0, 1, 2, 3, 4, 5) and p.value_ids[6] == daq2.UNUSED
    np.testing.assert_array_equal(p.values, values.astype(np.float32))


def test_frame_times_follow_firmware_formula():
    p = daq2.decode(packets.build(packets.sine_block(0, 64), T0))
    t = p.frame_times(TAI)
    period = packets.period_q32()
    assert t[0] == T0 - TAI
    assert t[63] == T0 - TAI + ((63 * period) >> 32)
    assert np.all(np.diff(t) > 0)
    # ~30048 frames/s
    assert abs((t[1] - t[0]) - 1e9 / 30048) < 1


def test_raw_lines_put_each_group_at_its_own_time():
    p = daq2.decode(packets.build(packets.sine_block(0, 64), T0))
    lines = daq2.raw_lines(p, "node_01", TAI)
    assert len(lines) == 2 * 64
    v0, i0 = lines[0], lines[64]
    assert v0.startswith("voltage,node_id=node_01 a=") and v0.endswith(f" {T0 - TAI}")
    # Currents are one ADC slot (1664 ticks at 100 MHz) after voltages
    assert i0.startswith("current,node_id=node_01 a=") and i0.endswith(f" {T0 - TAI + 16640}")


def test_integer_lines_are_milli_units():
    values = np.array([[230.4567, -1.0, 0.0, 9.8765, -0.0004, 0.0]] * 16)
    p = daq2.decode(packets.build(values, T0))
    v, i = daq2.raw_lines(p, "node_01", TAI, integer=True)[::16]
    assert v.startswith("voltage,node_id=node_01 a=230457i,b=-1000i,c=0i ")
    assert i.startswith("current,node_id=node_01 a=9877i,b=0i,c=0i ")


def test_neutral_mode_adds_neutral_measurement():
    values = packets.sine_block(0, 32, channels=8)
    p = daq2.decode(packets.build(values, T0, frame_ticks=3 * packets.RANK_TICKS))
    lines = daq2.raw_lines(p, "node_01", TAI)
    neutral = [l for l in lines if l.startswith("neutral,")]
    assert len(neutral) == 32 and " v=" in neutral[0] and ",i=" in neutral[0]
    assert neutral[0].endswith(f" {T0 - TAI + 2 * 16640}")


def test_tag_escaping():
    p = daq2.decode(packets.build(packets.sine_block(0, 16), T0))
    assert daq2.raw_lines(p, "node 1,x", TAI)[0].startswith("voltage,node_id=node\\ 1\\,x ")


@pytest.mark.parametrize("mutate,message", [
    (lambda b: b[:100], "short"),
    (lambda b: b"XXXX" + b[4:], "magic"),
    (lambda b: b[:-4], "length"),
])
def test_rejects_malformed(mutate, message):
    good = packets.build(packets.sine_block(0, 16), T0)
    with pytest.raises(daq2.DecodeError, match=message):
        daq2.decode(mutate(good))


def test_second_aggregator_rms_and_boundaries():
    agg = daq2.SecondAggregator("node_01")
    frames_per_s = 30048
    out = []
    index = 0
    # Just over two seconds of 64-frame packets
    while index < 2 * frames_per_s + 64:
        t = T0 + ((index * packets.period_q32()) >> 32)
        p = daq2.decode(packets.build(packets.sine_block(index, 64), t, first_index=index))
        out += agg.add(p, TAI)
        index += 64
    # Two complete seconds, each with a voltage and a current line
    assert len(out) == 4
    v = [l for l in out if l.startswith("voltage,")][0]
    fields = dict(kv.split("=") for kv in v.split(" ")[1].split(","))
    assert abs(float(fields["a_rms"]) - 230) < 0.5
    assert abs(float(fields["a_mean"])) < 0.5
    assert abs(float(fields["a_max"]) - 230 * math.sqrt(2)) < 1
    assert abs(int(fields["frames"].rstrip("i")) - frames_per_s) <= 1
    assert v.endswith(f" {T0 - TAI}")


def test_gap_and_invalid_time_counting():
    ingest = pytest.importorskip("ingest")

    class FakeWriter:
        def __init__(self):
            self.lines = {}

        def put(self, bucket, lines):
            self.lines.setdefault(bucket, []).extend(lines)

    ingest.stats.take()
    nodes, w = {}, FakeWriter()
    for first in (0, 64, 192):          # frames 128..191 missing
        t = T0 + ((first * packets.period_q32()) >> 32)
        ingest.process(nodes, w, "daq2/node_01/samples",
                       packets.build(packets.sine_block(first, 64), t, first_index=first))
    ingest.process(nodes, w, "daq2/node_01/samples",
                   packets.build(packets.sine_block(256, 64), T0, first_index=256, flags=0))
    s = ingest.stats.take()
    assert s["packets"] == 4 and s["gap_frames"] == 64 and s["invalid_time"] == 1
    # Invalid-time packet wrote nothing: 3 packets x (128 raw + 1 meta) lines
    assert len(w.lines[ingest.RAW_BUCKET]) == 3 * 129


def test_rate_adjusted_packets_kept_only_when_time_is_continuous():
    ingest = pytest.importorskip("ingest")

    class FakeWriter:
        def __init__(self):
            self.lines = []

        def put(self, bucket, lines):
            if bucket == ingest.RAW_BUCKET:
                self.lines.extend(lines)

    def send(nodes, w, first, *, valid=True, epoch=1, skew_ns=0):
        t = T0 + ((first * packets.period_q32()) >> 32) + skew_ns
        flags = daq2.FLAG_PTP_LOCKED | (daq2.FLAG_TIME_VALID if valid else 0)
        ingest.process(nodes, w, "daq2/node_01/samples",
                       packets.build(packets.sine_block(first, 64), t, first_index=first,
                                     flags=flags, ptp_epoch=epoch))

    ingest.stats.take()
    nodes, w = {}, FakeWriter()
    send(nodes, w, 0, valid=False)                 # nothing to compare against: skipped
    send(nodes, w, 64)                             # valid
    send(nodes, w, 128, valid=False, skew_ns=500)  # servo rate change, time continuous: kept
    send(nodes, w, 192, valid=False, skew_ns=50_000)   # time jumped 50 us: skipped
    send(nodes, w, 256)                            # valid
    send(nodes, w, 320, valid=False, epoch=2)      # clock stepped: skipped
    s = ingest.stats.take()
    assert s["time_flagged"] == 1 and s["invalid_time"] == 3
    meta = [l for l in w.lines if l.startswith("daq2_packet,")]
    assert len(meta) == 3
    assert "first_index=128i" in meta[1] and "time_valid=0i" in meta[1]


def test_unsynchronised_clock_is_skipped():
    ingest = pytest.importorskip("ingest")

    class FakeWriter:
        def __init__(self):
            self.lines = []

        def put(self, bucket, lines):
            self.lines.extend(lines)

    ingest.stats.take()
    nodes, w = {}, FakeWriter()
    boot = 14_000_000_000                                   # 14 s after boot, i.e. 1970
    ingest.process(nodes, w, "daq2/node_01/samples",
                   packets.build(packets.sine_block(0, 64), boot))
    now_tai = time.time_ns() + ingest.TAI_UTC_OFFSET_NS
    ingest.process(nodes, w, "daq2/node_01/samples",
                   packets.build(packets.sine_block(64, 64), now_tai, first_index=64, ptp_epoch=2))
    s = ingest.stats.take()
    assert s["implausible_time"] == 1 and len(w.lines) == 129
