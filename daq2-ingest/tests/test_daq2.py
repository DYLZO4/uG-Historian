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



def test_utc_offset_comes_from_the_announce_when_present():
    block = packets.sine_block(0, 64)
    base = daq2.FLAG_PTP_LOCKED | daq2.FLAG_TIME_VALID
    assert daq2.decode(packets.build(block, T0, flags=base)).utc_offset_ns(TAI) == TAI
    announced = base | daq2.FLAG_PTP_TIMESCALE | daq2.FLAG_UTC_OFFSET | (38 << 16)
    assert daq2.decode(packets.build(block, T0, flags=announced)).utc_offset_ns(TAI) == 38_000_000_000
    # ARB timescale: the firmware reports a zero offset
    arb = base | daq2.FLAG_UTC_OFFSET
    assert daq2.decode(packets.build(block, T0, flags=arb)).utc_offset_ns(TAI) == 0
    # int16, so a negative offset survives
    negative = base | daq2.FLAG_PTP_TIMESCALE | daq2.FLAG_UTC_OFFSET | (0xFFFF << 16)
    assert daq2.decode(packets.build(block, T0, flags=negative)).utc_offset_ns(TAI) == -1_000_000_000

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
    # Two complete seconds, each with voltage, current and power lines
    assert len(out) == 6
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


def test_writer_posts_concurrently_and_retries_failures(monkeypatch):
    ingest = pytest.importorskip("ingest")
    import threading

    real_sleep = time.sleep   # ingest.time is this same module, so keep the original
    monkeypatch.setattr(ingest, "BATCH_LINES", 100)
    monkeypatch.setattr(ingest, "FLUSH_SECONDS", 0.05)
    lock = threading.Lock()
    written, active, peak, failed = [], [0], [0], []

    def fake_write(self, bucket, lines):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        real_sleep(0.05)                       # a slow InfluxDB
        with lock:
            active[0] -= 1
            if not failed:                     # first batch fails once, then is retried
                failed.append(lines[0])
                return False
            written.extend(lines)
        return True

    monkeypatch.setattr(ingest.Writer, "write", fake_write)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: real_sleep(min(s, 0.05)))
    w = ingest.Writer()
    w.start()
    lines = [f"line {i}" for i in range(2000)]
    w.put(ingest.RAW_BUCKET, lines)
    deadline = time.time() + 10
    while len(written) < len(lines) and time.time() < deadline:
        real_sleep(0.02)
    assert sorted(written) == sorted(lines)    # every line exactly once, the failed batch included
    assert peak[0] > 1                         # posts overlapped


def test_router_balances_nodes_and_keeps_them_in_place():
    ingest = pytest.importorskip("ingest")
    r = ingest.Router(4)
    first = [r.worker(f"node_{n:02d}") for n in range(5)]
    assert sorted(first[:4]) == [0, 1, 2, 3]            # one each before any doubles up
    assert max(r.counts) - min(r.counts) <= 1
    assert [r.worker(f"node_{n:02d}") for n in range(5)] == first   # sticky


SLOT_S = packets.RANK_TICKS / packets.TIMER_HZ   # current sampled one ADC slot after voltage


def one_second_power(phi, *, gap_after_packet=None):
    """Feeds just over a second of three-phase data with the current lagging by phi, sampled with
    the firmware's slot delay; returns the first complete second's power fields."""
    agg = daq2.SecondAggregator("node_01")
    out, index, n = [], 0, 0
    while index < 30048 + 128:
        t = T0 + ((index * packets.period_q32()) >> 32)
        block = packets.sine_block(index, 64, current_lag_s=SLOT_S, current_phase_rad=phi)
        out += agg.add(daq2.decode(packets.build(block, t, first_index=index)), TAI)
        index += 64
        n += 1
        if gap_after_packet is not None and n == gap_after_packet:
            index += 64                                    # one packet lost
    line = [l for l in out if l.startswith("power,")][0]
    return {k: float(v.rstrip("i")) for k, v in (kv.split("=") for kv in line.split(" ")[1].split(","))}


@pytest.mark.parametrize("phi_deg", [0, 60])
def test_power_matches_theory(phi_deg):
    f = one_second_power(math.radians(phi_deg))
    p_expected = 2300 * math.cos(math.radians(phi_deg))   # 230 V x 10 A per phase
    for ph in "abc":
        assert f[f"p_{ph}"] == pytest.approx(p_expected, rel=5e-4, abs=0.5)
        assert f[f"s_{ph}"] == pytest.approx(2300, rel=5e-4)
        assert f[f"pf_{ph}"] == pytest.approx(math.cos(math.radians(phi_deg)), abs=5e-4)
    assert f["p_total"] == pytest.approx(3 * p_expected, rel=5e-4, abs=1.5)


def test_slot_delay_correction_matters():
    # Without interpolation the 16.64 us current lag would add 0.3 deg: ~0.9% low at PF 0.5
    phi = math.radians(60)
    uncorrected = 2300 * math.cos(phi + 2 * math.pi * 50 * SLOT_S)
    corrected = one_second_power(phi)["p_a"]
    assert abs(corrected - 1150) < 0.1 * abs(uncorrected - 1150)
    assert abs(uncorrected - 1150) / 1150 > 0.008


def test_power_skips_only_the_frame_after_a_gap():
    with_gap = one_second_power(0.0, gap_after_packet=10)
    assert with_gap["frames"] in (30048 - 64 - 1, 30047 - 64 - 1)   # lost packet, plus 1 frame
    assert with_gap["p_a"] == pytest.approx(2300, rel=5e-4)
