"""Subscribes to the DAQ nodes' full-rate DAQ2 stream and writes it to InfluxDB.

  daq2/<node_id>/samples ──▶ decode ──▶ waveform     raw voltage/current/neutral + daq2_packet
                                    └─▶ waveform_1s  per-second RMS/mean/min/max, daq2_ingest

MQTT callbacks only enqueue payloads; one worker decodes and builds line protocol, and one writer
batches it to InfluxDB. If InfluxDB falls behind, the oldest buffered data is dropped and counted
rather than letting memory grow without bound.
"""
import gzip
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

import daq2

log = logging.getLogger("daq2-ingest")


def env(name, default):
    return type(default)(os.environ.get(name, default))


MQTT_HOST = env("MQTT_HOST", "mosquitto")
MQTT_PORT = env("MQTT_PORT", 1883)
MQTT_TOPIC = env("MQTT_TOPIC", "daq2/+/samples")
INFLUX_URL = env("INFLUX_URL", "http://influxdb:8086").rstrip("/")
INFLUX_TOKEN = env("INFLUXDB_TOKEN", "")
INFLUX_ORG = env("INFLUX_ORG", "qut-microgrid")
RAW_BUCKET = env("RAW_BUCKET", "waveform")
RAW_RETENTION = env("RAW_RETENTION", "7d")
AGG_BUCKET = env("AGG_BUCKET", "waveform_1s")
AGG_RETENTION = env("AGG_RETENTION", "0")          # 0 = keep forever
TAI_UTC_OFFSET_NS = env("TAI_UTC_OFFSET_S", 37) * 1_000_000_000
# Packets the firmware marked not time-valid are kept if their first-frame time is within this
# of where the previous packet predicts, across at most this many frames (~1 s)
TIME_CONTINUITY_NS = env("TIME_CONTINUITY_NS", 10_000)
MAX_CONTINUITY_FRAMES = env("MAX_CONTINUITY_FRAMES", 32768)
# Packets more than this far from the server's clock are skipped. Catches a node publishing
# before its first PTP step, when its clock still counts from boot (1970).
MAX_CLOCK_SKEW_NS = env("MAX_CLOCK_SKEW_S", 86400) * 1_000_000_000
# "int": raw values as integer mV/mA, which took ~4.8x less disk than floats in testing, at the same precision
RAW_INTEGER = env("RAW_FORMAT", "int") == "int"
BATCH_LINES = env("BATCH_LINES", 20000)
FLUSH_SECONDS = env("FLUSH_SECONDS", 1.0)
MAX_BUFFERED_LINES = env("MAX_BUFFERED_LINES", 2_000_000)   # ~10 s of two nodes at full rate
STATS_SECONDS = env("STATS_SECONDS", 10)


def duration_seconds(text):
    """'7d', '12h', '30m', '90s' or a plain number of seconds; 0 means infinite."""
    text = str(text).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.packets = self.frames = self.bad_packets = self.invalid_time = self.time_flagged = 0
        self.implausible_time = 0
        self.gap_frames = self.mqtt_drops = self.buffer_drop_lines = self.write_errors = 0
        self.lines_written = 0
        self.write_ms = 0.0

    def add(self, **counts):
        with self.lock:
            for k, v in counts.items():
                setattr(self, k, getattr(self, k) + v)

    def take(self):
        with self.lock:
            snap = dict(vars(self)); snap.pop("lock")
            self.reset()
            return snap


stats = Stats()


# --- InfluxDB ---

def influx_request(method, path, body=None, headers=None):
    req = urllib.request.Request(INFLUX_URL + path, data=body, method=method)
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        return json.loads(data) if data and resp.headers.get_content_type() == "application/json" else None


def ensure_bucket(name, retention):
    """Creates the bucket if missing. An existing bucket's retention is left as it is."""
    q = urllib.parse.urlencode({"org": INFLUX_ORG, "name": name})
    try:
        if influx_request("GET", f"/api/v2/buckets?{q}").get("buckets"):
            return
    except urllib.error.HTTPError as e:
        if e.code != 404:   # filtering by a name that doesn't exist gives 404
            raise
    org = influx_request("GET", "/api/v2/orgs?" + urllib.parse.urlencode({"org": INFLUX_ORG}))
    seconds = duration_seconds(retention)
    body = {"orgID": org["orgs"][0]["id"], "name": name,
            "retentionRules": [{"type": "expire", "everySeconds": seconds}] if seconds else []}
    influx_request("POST", "/api/v2/buckets", json.dumps(body).encode(),
                   {"Content-Type": "application/json"})
    log.info("created bucket %s (retention %s)", name, retention if seconds else "infinite")


def wait_for_influx():
    while True:
        try:
            ensure_bucket(RAW_BUCKET, RAW_RETENTION)
            ensure_bucket(AGG_BUCKET, AGG_RETENTION)
            return
        except (urllib.error.URLError, OSError, KeyError, IndexError) as e:
            log.warning("InfluxDB not ready (%s), retrying", e)
            time.sleep(5)


class Writer(threading.Thread):
    """Batches lines per bucket and posts them gzipped. Retries failed writes in order."""

    def __init__(self):
        super().__init__(daemon=True, name="writer")
        self.cond = threading.Condition()
        self.pending = {RAW_BUCKET: [], AGG_BUCKET: []}

    def put(self, bucket, lines):
        with self.cond:
            self.pending[bucket].extend(lines)
            self._shed()
            if len(self.pending[bucket]) >= BATCH_LINES:
                self.cond.notify()

    def _shed(self):
        """Caller holds cond. Drops the oldest raw lines beyond MAX_BUFFERED_LINES; aggregates
        are small and long-lived, so they're kept."""
        excess = sum(map(len, self.pending.values())) - MAX_BUFFERED_LINES
        if excess > 0:
            shed = min(excess, len(self.pending[RAW_BUCKET]))
            del self.pending[RAW_BUCKET][:shed]
            stats.add(buffer_drop_lines=shed)

    def run(self):
        while True:
            with self.cond:
                self.cond.wait(FLUSH_SECONDS)
                batches, self.pending = self.pending, {b: [] for b in self.pending}
            for bucket, lines in batches.items():
                for i in range(0, len(lines), BATCH_LINES):
                    if not self.write(bucket, lines[i:i + BATCH_LINES]):
                        # Put back what wasn't written, ahead of anything newer
                        with self.cond:
                            self.pending[bucket][:0] = lines[i:]
                            self._shed()
                        time.sleep(1)
                        break

    def write(self, bucket, lines):
        q = urllib.parse.urlencode({"org": INFLUX_ORG, "bucket": bucket, "precision": "ns"})
        body = gzip.compress("\n".join(lines).encode(), compresslevel=1)
        start = time.perf_counter()
        try:
            influx_request("POST", f"/api/v2/write?{q}", body,
                           {"Content-Encoding": "gzip", "Content-Type": "text/plain; charset=utf-8"})
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            stats.add(write_errors=1)
            log.error("write to %s failed: %s %s", bucket, e.code, detail)
            if 400 <= e.code < 500 and e.code != 429:
                # Won't succeed on retry, so don't block on it. A partial write (422) has stored
                # the rest of the batch, so count only the points InfluxDB says it dropped.
                m = re.search(r"dropped (\d+) points", detail)
                stats.add(buffer_drop_lines=int(m.group(1)) if m else len(lines))
                return True
            return False
        except (urllib.error.URLError, OSError) as e:
            stats.add(write_errors=1)
            log.error("write to %s failed: %s", bucket, e)
            return False
        stats.add(lines_written=len(lines), write_ms=(time.perf_counter() - start) * 1000)
        return True


# --- Decoding ---

class Node:
    def __init__(self, node_id):
        self.node_id = node_id
        self.aggregator = daq2.SecondAggregator(node_id)
        self.next_index = None
        self.config_epoch = None
        self.ptp_epoch = None
        self.last_written = None   # (first_output_index, first_time_ns, period_q32_ns, config_epoch)


def time_is_continuous(node, packet):
    """Whether a packet's first-frame time is where the last written packet's time and frame
    period put it, to within TIME_CONTINUITY_NS.

    The firmware clears TIME_VALID whenever the PTP servo adjusted the clock rate during the
    packet, which it does on every Sync, about once a second. A rate adjustment only skews
    sample times by nanoseconds over one packet, so those packets are kept when their time is
    continuous. A step changes ptp_epoch, and a failed clock read breaks continuity; both are
    still skipped.
    """
    last = node.last_written
    if last is None or last[3] != packet.config_epoch:
        return False
    frames = packet.first_output_index - last[0]
    if not 0 < frames <= MAX_CONTINUITY_FRAMES:
        return False
    predicted = last[1] + ((frames * last[2]) >> 32)
    return abs(packet.first_time_ns - predicted) <= TIME_CONTINUITY_NS


def process(nodes, writer, topic, payload):
    parts = topic.split("/")
    node_id = parts[1] if len(parts) == 3 else "unknown"
    try:
        packet = daq2.decode(payload)
    except daq2.DecodeError as e:
        stats.add(bad_packets=1)
        log.warning("%s: %s", topic, e)
        return
    node = nodes.get(node_id) or nodes.setdefault(node_id, Node(node_id))
    stats.add(packets=1, frames=packet.frames)

    # Frame index gaps are data the node acquired but never delivered
    if node.config_epoch != packet.config_epoch:
        node.config_epoch, node.next_index, node.last_written = packet.config_epoch, None, None
        node.aggregator = daq2.SecondAggregator(node_id)
    epoch_changed = node.ptp_epoch != packet.ptp_epoch
    if epoch_changed:
        # The clock stepped: don't mix samples from either side of the step into one second
        node.ptp_epoch, node.last_written = packet.ptp_epoch, None
        node.aggregator = daq2.SecondAggregator(node_id)
    if node.next_index is not None and packet.first_output_index > node.next_index:
        stats.add(gap_frames=packet.first_output_index - node.next_index)
    node.next_index = packet.first_output_index + packet.frames

    if abs(packet.first_time_ns - TAI_UTC_OFFSET_NS - time.time_ns()) > MAX_CLOCK_SKEW_NS:
        # Not synchronised yet (or a wildly wrong clock): it would land years away from now
        stats.add(implausible_time=1)
        return
    if not packet.time_valid:
        if epoch_changed or not time_is_continuous(node, packet):
            # Its sample times can't be trusted, so it can't be placed on the time axis at all
            stats.add(invalid_time=1)
            return
        # Kept, and recorded as time_valid=0 in daq2_packet
        stats.add(time_flagged=1)
    node.last_written = (packet.first_output_index, packet.first_time_ns, packet.period_q32_ns,
                         packet.config_epoch)
    writer.put(RAW_BUCKET, daq2.raw_lines(packet, node_id, TAI_UTC_OFFSET_NS, integer=RAW_INTEGER) +
               [daq2.packet_line(packet, node_id, TAI_UTC_OFFSET_NS)])
    done = node.aggregator.add(packet, TAI_UTC_OFFSET_NS)
    if done:
        writer.put(AGG_BUCKET, done)


def worker(inbox, writer):
    nodes = {}
    while True:
        topic, payload = inbox.get()
        try:
            process(nodes, writer, topic, payload)
        except Exception:
            log.exception("failed to process packet from %s", topic)


def report_stats(inbox, writer):
    while True:
        time.sleep(STATS_SECONDS)
        s = stats.take()
        s["queue_depth"] = inbox.qsize()
        s["write_ms"] = round(s["write_ms"], 1)
        log.info("stats %s", " ".join(f"{k}={v}" for k, v in s.items()))
        fields = ",".join(f"{k}={v}" if isinstance(v, float) else f"{k}={v}i" for k, v in s.items())
        writer.put(AGG_BUCKET, [f"daq2_ingest {fields} {time.time_ns()}"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not INFLUX_TOKEN:
        raise SystemExit("INFLUXDB_TOKEN is not set")
    wait_for_influx()

    inbox = queue.Queue(maxsize=5000)   # ~5 s of two nodes at ~470 packets/s each
    writer = Writer()
    writer.start()
    threading.Thread(target=worker, args=(inbox, writer), daemon=True, name="worker").start()
    threading.Thread(target=report_stats, args=(inbox, writer), daemon=True, name="stats").start()

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        client.subscribe(MQTT_TOPIC, qos=0)
        log.info("subscribed to %s on %s:%s", MQTT_TOPIC, MQTT_HOST, MQTT_PORT)

    def on_message(_client, _userdata, msg):
        try:
            inbox.put_nowait((msg.topic, msg.payload))
        except queue.Full:
            stats.add(mqtt_drops=1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daq2-ingest")
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
