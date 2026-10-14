# uG-Historian

Time-series historian for the microgrid DAQ testbed. STM32 DAQ nodes publish PTP-timestamped samples over MQTT. Telegraf and `daq2-ingest` write them into InfluxDB, and Grafana displays them.

```
                         ┌─▶ Telegraf ─────▶ telemetry     (5 s JSON snapshots)
DAQ nodes ──MQTT──▶ Mosquitto                   InfluxDB v2 ──Flux──▶ Grafana
 (STM32)            :1883└─▶ daq2-ingest ──▶ waveform     (full ~30 kHz)     :3000
                                          └▶ waveform_1s  (per-second RMS, power)
```

All five services run as Docker containers on a single historian server, managed by `docker-compose.yml`.

Tested on **Ubuntu Server 24.04.4 LTS**.

| Service   | Image                   | Host port            | Purpose                              |
|-----------|-------------------------|----------------------|--------------------------------------|
| mosquitto | `eclipse-mosquitto:2`   | `127.0.0.1:1883`     | MQTT broker                          |
| influxdb  | `influxdb:2`            | `127.0.0.1:8086`     | Time-series database + admin UI      |
| telegraf  | `telegraf:latest`       | none                 | MQTT → InfluxDB bridge (JSON snapshots) |
| daq2-ingest | built from `daq2-ingest/` | none             | MQTT → InfluxDB bridge (full-rate binary stream) |
| grafana   | `grafana/grafana:latest`| `0.0.0.0:3000`       | Dashboards                           |

Design documents (LaTeX + PDF) are in [`documentation/`](documentation/).

---

## 1. Install Docker

Skip this if `docker compose version` already works.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Run docker without sudo (log out and back in afterwards)
sudo usermod -aG docker $USER
```

Check it after logging back in:

```bash
docker run --rm hello-world
docker compose version
```

## 2. Clone the repository

```bash
git clone https://github.com/DYLZO4/uG-Historian.git
cd uG-Historian
```

(Use `git@github.com:DYLZO4/uG-Historian.git` if you have SSH keys set up on GitHub.)

## 3. Create the `.env` file

Secrets are read from a `.env` file in the repo root. This file is git-ignored and **must be created by hand**:

```bash
cat > .env <<EOF
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=change-me-please
INFLUXDB_TOKEN=$(openssl rand -hex 32)
GRAFANA_ADMIN_PASSWORD=change-me-too
EOF
chmod 600 .env
cat .env
```

| Variable                  | Used by              | Notes |
|---------------------------|----------------------|-------|
| `INFLUXDB_ADMIN_USER`     | InfluxDB             | Username for the InfluxDB web UI. |
| `INFLUXDB_ADMIN_PASSWORD` | InfluxDB             | **Minimum 8 characters**, or InfluxDB setup fails. |
| `INFLUXDB_TOKEN`          | InfluxDB, Telegraf, daq2-ingest, Grafana | Admin API token, used by everything that reads or writes InfluxDB. Any long random string works. |
| `GRAFANA_ADMIN_PASSWORD`  | Grafana              | Password for the Grafana `admin` user. |
| `RAW_RETENTION`           | daq2-ingest          | Optional, default `7d`. How long full-rate data is kept. Size it to your disk: see [section 10](#10-full-rate-waveform-data-daq2-ingest). |

> **These values are only applied the first time the containers start.** InfluxDB and Grafana store their users and tokens in Docker volumes. Changing `.env` later does **not** change the password or token. See [Resetting](#resetting-everything) if you need to start over.

## 4. Settings you may need to change

Everything else is hard-coded and consistent across files. Only change these if you know why:

| Setting | Where | Default | When to change |
|---------|-------|---------|----------------|
| Mosquitto host binding | `docker-compose.yml` → `mosquitto.ports` | `127.0.0.1:1883:1883` | **Change to `"1883:1883"` before connecting real DAQ boards.** With the default, only clients on the server itself (e.g. the test message in step 6) can connect. The `rohan-local` branch already has this change. |
| InfluxDB org | `docker-compose.yml`, `telegraf/telegraf.conf`, `grafana/provisioning/datasources/influxdb.yml` | `qut-microgrid` | Must match in all three files. |
| InfluxDB bucket | same three files | `telemetry` | Must match in all three files. |
| `telemetry` retention (JSON snapshots and PTP status) | `docker-compose.yml` → `DOCKER_INFLUXDB_INIT_RETENTION` | `90d` | Only applies on first start. Change it later in the InfluxDB UI (Load Data → Buckets). Full-rate retention is `RAW_RETENTION` (step 3). |
| MQTT topic filter | `telegraf/telegraf.conf` → `topics` | `microgrid/#` | Only if the topic scheme changes. |
| Telegraf flush interval | `telegraf/telegraf.conf` → `[agent] flush_interval` | `1s` | Keeps PTP status as current as Node View's 1 s refresh. Raise it only if write load matters more. |

## 5. Start the stack

```bash
docker compose up -d
docker compose ps
```

All five containers should show `running`. On first start InfluxDB takes ~10–20 s to initialise, and Telegraf may log a few connection errors until InfluxDB is ready. That's expected.

Useful log commands:

```bash
docker compose logs -f telegraf    # MQTT → InfluxDB bridge for JSON (most useful for debugging)
docker compose logs -f daq2-ingest # full-rate bridge: stats line every 10 s
docker compose logs -f influxdb
docker compose logs -f mosquitto
docker compose logs -f grafana
```

## 6. Send a test message

Before connecting real nodes, check the pipeline by publishing one burst in the same format the DAQ firmware uses (see [section 8](#8-data-model-whats-actually-in-influxdb)). Run this **on the server**, because the broker only listens on `localhost` by default:

```bash
docker exec mosquitto mosquitto_pub -t microgrid/node_test/voltage/phase_a -m \
  "{\"ts\":$(date +%s%N),\"sync\":1,\"dt_ns\":1000000,\"v\":[0,71,135,186,219,230,219,186,135,71,0,-71,-135,-186,-219,-230,-219,-186,-135,-71]}"
```

To confirm messages are reaching the broker, run this in a second terminal before publishing:

```bash
docker exec -it mosquitto mosquitto_sub -t 'microgrid/#' -v
```

Then check `docker compose logs telegraf` for errors. The point should appear in InfluxDB under `node_id = node_test`. It stays in the bucket until retention expires, so filter it out of dashboards or use a test bucket.

## 7. Log in to Grafana

### Log in

Open **`http://<server-ip>:3000`** in a browser (find the IP with `hostname -I`).

- **Username:** `admin`
- **Password:** the `GRAFANA_ADMIN_PASSWORD` from `.env`

(If `GRAFANA_ADMIN_PASSWORD` was left empty, Grafana's default is `admin` / `admin`, and it will prompt you to set a new password.)

If the page doesn't load from another machine, see [Troubleshooting](#troubleshooting).

### Data source and dashboards

Both are provisioned from `grafana/provisioning/`, so there's nothing to set up by hand:

- **InfluxDB (Flux)** data source (`datasources/influxdb.yml`), with the token taken from `INFLUXDB_TOKEN` in `.env`. Check it under **Connections → Data sources → InfluxDB (Flux) → Save & test**. You should see *"datasource is working"*. Its uid is pinned to `PF6CD3C7EE64CF7D1`, so the dashboard and `grafana_export.py` can reference it.
- **Node View** dashboard (`dashboards/node-view.json`), in the **Microgrid** folder: everything an operator needs to know about one node. The top bar has three selectors: `node_id`, **Phase** (3-phase or phase A, B or C) and **Voltage / current** (RMS or peak). It refreshes every second by default; a full refresh takes well under a second.

| Part | What it shows |
|------|---------------|
| Top boxes | **Status**: online if its newest point is under 30 s old. **Avg power (10 s)** and **PF** (power factor) for the selected phase. **PTP**: *locked* (within 1 µs for 8 Syncs, needed for cross-node comparisons), *tracking* (servo running, within 100 µs) or *unlocked*. |
| Power | Real power P and apparent power S for the selected phase, per second, on one shared axis so the gap between them shows the power factor |
| Voltage / Current | RMS or peak, per second. **Zoom in to 5 s or less** (drag across the graph) and they switch to the raw waveform for that moment, one sample per millisecond, the finest Grafana's time axis resolves. The legend says which is shown (`phase_a RMS`, `phase_a samples`). Raw data only goes back as far as `RAW_RETENTION`. Voltages are phase-to-earth with the default shield profile. |
| PTP | Offset from the grandmaster, path delay, frequency correction (one point per Sync), and the firmware's sample-timing uncertainty |
| Full-rate waveform | The latest ~100 ms of voltage and current at the full ~30 kHz, ending ~2 s ago so every write has landed. Ignores the dashboard time range. |
| Diagnostics (collapsed) | **Completeness**: frames stored as a share of frames the node acquired over the minute ending 5 s ago (the newest seconds are skipped because the writer threads can store batches out of order). Also `daq2-ingest` loss counters across all nodes, frames per second over time, and **ingest latency** (median and 99th percentile from sample to MQTT arrival and to storage, see [Latency](#latency)). |

The **Phase** selector applies to everything phase-specific: power, power factor, and the voltage, current and waveform graphs show only the selected phase, or all three for 3-phase. Each phase keeps its colour (A green, B yellow, C blue) whichever is shown.

The full-rate panels show *DAQ2 off* or stay empty until a node publishes the DAQ2 stream.

- **Node Compare** dashboard (`dashboards/node-compare.json`), also in **Microgrid**: two nodes side by side, for viewing the same-signal test and two-node runs. It only displays data; test results are computed offline from the stored samples. The top bar selects **Node A**, **Node B**, the **Input** (voltage or current) and its **Phase** (A, B or C). It refreshes every 5 s by default.

| Part | What it shows |
|------|---------------|
| Top boxes | **Status**, **PTP** and **Completeness** for each node, as in Node View |
| Overlay | Both nodes' samples over the same 60 ms at the full ~30 kHz; with the same signal the traces lie on top of each other |
| Per second | RMS and DC mean of the selected input for both nodes (a steady RMS gap is a gain difference between the boards), and frames per second |
| Bottom | PTP offset of both nodes, and the `daq2-ingest` loss counters and latency across all nodes |

**Changing the dashboard:** you can edit it in Grafana, but provisioned dashboards are reloaded from the file, so changes are lost on restart unless you save them back. Use **Share → Export → Save to file** and replace the dashboard's file in `grafana/provisioning/dashboards/`, then commit it.

## 8. Data model (what's actually in InfluxDB)

Every 5 s, each DAQ node publishes one 50 Hz cycle per channel: 20 samples at ~1 kHz, taken from its ~30 kHz acquisition stream. Each channel is a separate message on `microgrid/<node_id>/<subsystem>/<metric>`:

```json
{"ts": 1791345068887514368, "sync": 1, "dt_ns": 998424, "v": [231.45, 229.80, ...]}
```

| Payload field | Meaning |
|---------------|---------|
| `ts`    | Unix time (UTC) in ns of the first sample, from the node's PTP clock. Currents are sampled one ADC slot after voltages, so their `ts` is slightly later. |
| `sync`  | `1` if the node's PTP servo was locked when the burst was captured, else `0`. Sent as an integer because Telegraf's JSON parser drops booleans. |
| `dt_ns` | Spacing between samples in ns (the nearest whole number of ~30 kHz frames to 1 ms, e.g. `998424`). |
| `v`     | 20 samples in V or A, rounded to 2 decimal places. |

Telegraf flattens the `v` array, so each message becomes one burst point:

| InfluxDB column | Value | Example |
|-----------------|-------|---------|
| `_measurement`  | always `mqtt_consumer` (Telegraf default) | `mqtt_consumer` |
| `node_id` tag   | topic segment 2 | `node_01`, `node_02` |
| `subsystem` tag | topic segment 3 | `voltage`, `current` |
| `metric` tag    | topic segment 4 | `phase_a`, `phase_b`, `phase_c` |
| `topic` tag     | full topic | `microgrid/node_01/voltage/phase_a` |
| `_field`        | `sync`, `dt_ns`, `v_0` … `v_19` | `v_7` |
| `_time`         | the `ts` from the payload (the node's PTP time, **not** the time the server received it) | |

Sample `k` was taken at `_time + k * dt_ns`. A Starlark processor in `telegraf.conf` does that expansion as data arrives: alongside each burst point it writes 20 points in the **`samples`** measurement, one per sample at its own timestamp, with the same tags and fields `v` and `sync`. Query those for waveforms:

```flux
from(bucket: "telemetry")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "samples" and r.node_id == "node_01")
  |> filter(fn: (r) => r.subsystem == "voltage" and r.metric == "phase_a" and r._field == "v")
```

The original burst points (`mqtt_consumer`, `v_0` … `v_19`) are still written, so existing dashboards keep working. Data stored before the processor was added only exists in burst form.

This JSON path is the low-rate fallback. For the full ~30 kHz data, see [section 10](#10-full-rate-waveform-data-daq2-ingest).

Node liveness is judged in Grafana from the time since each node's last sample. The firmware has no heartbeat or Last Will messages.

Only trust cross-node comparisons where `sync` is `1` on both nodes.

**PTP status.** On every Sync (about once a second), each node also publishes its servo's state on `microgrid/<node_id>/ptp_status/servo`:

```json
{"ts": 1791696297692880523, "offset_ns": 18274, "path_delay_ns": 15497, "freq_ppb": 18010, "state": 2, "locked": 0, "stepped": 0, "utc_offset_s": 37, "utc_announced": 1}
```

| Field | Meaning |
|-------|---------|
| `ts` | The grandmaster's time for that Sync, as Unix UTC ns (same TAI offset as the bursts) |
| `offset_ns` | Measured clock offset from the grandmaster |
| `path_delay_ns` | Mean path delay used for it |
| `freq_ppb` | Frequency correction the servo is applying, in parts per billion (the oscillator error it's cancelling) |
| `state` | `0` no Sync yet, `1` estimating frequency, `2` PI loop running |
| `locked` | `1` once 8 offsets in a row are within 1 µs |
| `stepped` | `1` if this update stepped the clock (offset over 1 ms) |
| `utc_offset_s` | TAI − UTC subtracted from the node's timestamps |
| `utc_announced` | `1` if that offset came from the grandmaster's Announce, `0` if it's the 37 s default |

Telegraf stores these as one `mqtt_consumer` point per Sync with `subsystem = ptp_status`, `metric = servo`, and those fields.

**Firmware version.** Once per connection, after its first PTP clock step, each node publishes the firmware it runs (`git describe` at build time, ending in `-dirty` for uncommitted changes) on `microgrid/<node_id>/status/firmware`, retained:

```json
{"ts": 1791782417800117923, "firmware": "8338d6b"}
```

Check every node with `docker exec mosquitto mosquitto_sub -t 'microgrid/+/status/firmware' -v -W 2`. Telegraf keeps `firmware` as a string field (`json_string_fields` in `telegraf.conf`; other strings are dropped), with `subsystem = status`, `metric = firmware`.

The firmware can also publish every frame at the full ~30 kHz as a binary packet on `daq2/<node_id>/samples`. That stream is handled by `daq2-ingest`, not Telegraf: see [section 10](#10-full-rate-waveform-data-daq2-ingest).

## 9. Connecting real DAQ nodes

1. Make sure the Mosquitto port is exposed to the LAN (see [step 4](#4-settings-you-may-need-to-change)), then `docker compose up -d`.
2. Set the broker address in the firmware's `app/node_config.h` (`BROKER_IP_ADDR0..3`, port `1883`), along with each node's `NODE_ID` and static IP. **No username/password is needed.** `mosquitto/config/mosquitto.conf` currently has `allow_anonymous true`.
3. The firmware publishes the burst payload from [section 8](#8-data-model-whats-actually-in-influxdb) at QoS 0, client ID `mqttc-<node_id>`. Anything else publishing into `microgrid/#` must follow the same rules:
   - `ts` **must** be Unix time in nanoseconds. Wrong units put points in 1970 or the far future, where they won't show up in Grafana.
   - Numbers and arrays of numbers only. Telegraf's JSON parser drops booleans and strings.
   - The PTP clock runs on TAI. The firmware converts to UTC with the TAI − UTC offset the grandmaster announces (37 s until it has), so the historian server's clock should be on UTC (e.g. NTP) for Grafana's time ranges to line up.

## 10. Full-rate waveform data (daq2-ingest)

Nodes built with `MQTT_PUBLISH_DAQ2=ON` (CMake option in the firmware, on by default) also publish every acquisition packet, 64 frames at ~30 kHz, about 470 packets/s per node, as binary on `daq2/<node_id>/samples`. `daq2-ingest` decodes them and writes **every sample** to InfluxDB.

> Verified on hardware: a node sustains this (~470 packets/s, 6.3 Mbit/s) alongside acquisition and PTP with no drops. After flashing, confirm it on the node's UART: `daq2_drop` on the `MQTT` line and `drop`/`ov` on the `DAQ` line should stay at 0.

### What's stored

| Bucket | Retention | Measurement | Fields | One point per |
|--------|-----------|-------------|--------|---------------|
| `waveform` | `RAW_RETENTION` (default `7d`) | `voltage` | `a`, `b`, `c` | frame (~33 µs) |
| | | `current` | `a`, `b`, `c` | frame |
| | | `neutral` | `v`, `i` (neutral mode only) | frame |
| | | `daq2_packet` | `sequence`, `first_index`, `frames`, `locked`, `time_valid`, `ptp_epoch`, `config_epoch`, `uncertainty_ns` | packet (64 frames) |
| `waveform_1s` | forever | `voltage`, `current`, `neutral` | `<f>_rms`, `<f>_mean`, `<f>_min`, `<f>_max` for each field, plus `frames`, `locked` | UTC second |
| | | `power` | `p_<ph>` real power (W), `s_<ph>` apparent power (VA), `pf_<ph>` power factor, for `<ph>` = `a`, `b`, `c`, `total`; plus `frames`, `locked` | UTC second |
| | | `daq2_ingest` | the service's stats counters | 10 s |

All points are tagged `node_id`, except `daq2_ingest`, which is tagged `worker`.

- **Units.** Raw values are **integers in mV and mA** (`RAW_FORMAT=int`, the default), because InfluxDB compresses integers far better than floats. The precision is the same 0.001 V/A either way. Divide by 1000 for V and A. The `waveform_1s` values are floats in V and A.
- **Timestamps.** Each point is at its exact sample time. Voltages and currents are sampled in different ADC slots, so `current` points are ~16.6 µs after the matching `voltage` points. Don't join them on exact `_time`.
- **Only trustworthy timestamps are written.** The firmware clears its time-valid flag whenever the PTP servo adjusts the clock rate, about once a second. Those packets are kept if their time is within 10 µs of where the previous packet predicts, since a rate adjustment only skews times by nanoseconds. They're recorded with `time_valid=0` in `daq2_packet` and counted as `time_flagged`. Packets across a clock step, or with a timestamp jump, are skipped and counted as `invalid_time`. `locked` records whether the PTP servo was locked.
- **Power** follows IEEE 1459: real power P = mean(v × i) per phase, apparent power S = V_rms × I_rms, and PF = P / S; totals are the sums of the phases. Each ADC samples its current one slot (16.64 µs) after its voltage, which would put a 0.3° phase error into P (~0.9% at PF 0.5), so each current is interpolated to its voltage's sampling instant first. Values are only as accurate as the (currently nominal) channel gains.
- **Per-second aggregates** are aligned to UTC seconds. `frames` is ~30048 for a complete second. Fewer means a partial second (start-up, a gap or a restart). `locked` is `1` only if every packet in that second was locked.

### Querying

Use `waveform_1s` for anything longer than a few seconds. A single raw channel is 30,000 points per second, which Grafana can't plot over long ranges. Raw data is for zooming in:

```flux
from(bucket: "waveform")
  |> range(start: -2s)
  |> filter(fn: (r) => r._measurement == "voltage" and r.node_id == "node_01" and r._field == "a")
  |> map(fn: (r) => ({r with _value: float(v: r._value) / 1000.0}))
```

Long-term RMS:

```flux
from(bucket: "waveform_1s")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "voltage" and r._field == "a_rms")
```

Use `daq2_packet` to check data quality. Gaps in `first_index` within one `config_epoch` mean frames the node acquired but never delivered.

### Disk space

Measured with synthetic 50 Hz data plus realistic noise: raw data takes about **1.6 bytes per value** as integers (the default), or ~7.9 bytes as floats. Two nodes at full rate store 360,000 values/s:

| Raw retention (2 nodes) | Disk for `waveform` |
|-------------------------|---------------------|
| 1 day   | ~51 GB |
| 7 days (default) | ~360 GB |
| 14 days | ~710 GB |
| 30 days | ~1.5 TB |

Scale linearly with the number of nodes. `waveform_1s` is tiny by comparison (~1 MB/day). Real signals may compress somewhat differently, so check actual usage after the first day with `docker exec influxdb du -sh /var/lib/influxdb2/engine/data` and leave headroom: InfluxDB needs free space for compactions.

`RAW_RETENTION` only applies when the `waveform` bucket is first created. To change it later:

```bash
docker exec influxdb influx bucket update --id $(docker exec influxdb influx bucket list --name waveform --hide-headers | cut -f1) --retention 14d
```

### Other settings

These have defaults in `daq2-ingest/ingest.py` and can be overridden in the service's `environment:` in `docker-compose.yml`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `RAW_FORMAT` | `int` | `int` (mV/mA integers) or `float` (V/A) for raw values |
| `AGG_RETENTION` | `0` | Retention of `waveform_1s`; `0` keeps it forever |
| `TAI_UTC_OFFSET_S` | `37` | Subtracted from the nodes' TAI timestamps when a packet doesn't carry the grandmaster's announced offset (before the node's first Announce) |
| `TIME_CONTINUITY_NS` | `10000` | How far a not-time-valid packet's time may be from the predicted time and still be kept |
| `MAX_CLOCK_SKEW_S` | `86400` | Packets further than this from the server's clock are skipped (`implausible_time`), e.g. a node publishing before its first PTP step, when its clock still reads 1970 |
| `INGEST_WORKERS` | CPU cores, max 8 | Decode/write processes. Each node is assigned to the least-loaded one and stays there. One full-rate node per worker has headroom; two is at the limit. |
| `WRITE_THREADS` | `4` | Parallel InfluxDB connections per worker |
| `MAX_BUFFERED_LINES` | `2000000` | Raw lines each worker holds in memory if InfluxDB falls behind before the oldest are dropped |
| `WORKER_QUEUE_PACKETS` | `5000` | Packets queued per worker (~10 s of one node) before the receiver drops them (`mqtt_drops`) |
| `BATCH_LINES` / `FLUSH_SECONDS` | `20000` / `1.0` | Write batching |
| `SHUTDOWN_SECONDS` | `8` | Time allowed on stop to drain queues and write everything buffered |

### Capacity

`daq2-ingest` runs as one receiver process (MQTT only) plus `INGEST_WORKERS` worker processes, so it uses multiple cores. Each worker's stats (covering the nodes assigned to it) appear in the logs and in `daq2_ingest` tagged `worker=<n>`, and the receiver's as `worker=receiver`.

**Stopping and restarting.** `docker compose stop`/`restart`/`up -d` stop the service cleanly in under a second: it disconnects from MQTT, each worker processes what's queued, writes the second in progress (a partial `frames` count) and everything still buffered, then exits (`SHUTDOWN_SECONDS`, default 8 s, within Docker's 10 s). The log ends with `worker <n> stopped, everything written` per worker and `stopped`. `init: true` in `docker-compose.yml` is required for this: it puts a minimal init in front of Python as PID 1, which would otherwise ignore the stop signal until Docker kills it after 10 s. Data published while the service is down is still lost, as nodes publish at QoS 0.

Measured on the home test server (12 threads, 7.7 GB RAM), with one real node plus synthetic ones:

| Nodes at full rate | Result | CPU used |
|-----|--------|----------|
| 1 | No loss | ingest ~0.5 core, InfluxDB ~0.5 |
| 5 | No loss over 2 minutes, queues flat | ingest ~3.2 cores, InfluxDB ~7.8 |
| 7 | ~9% of packets dropped: the server ran out of CPU | load average 16 on 12 threads |

Budget roughly **1.5 cores of InfluxDB plus 0.6 cores of ingest per node**, plus ~25 GB/day of disk. More nodes need more cores, not configuration changes.

### Latency

Every 10 s, each worker's `daq2_ingest` point also records how long packets took to arrive and to be stored. Each delay is measured from the packet's newest sample, in milliseconds:

| Field | Delay from the newest sample to |
|-------|---------------------------------|
| `latency_mqtt_p50_ms`, `_p99_ms`, `_max_ms` | the packet arriving over MQTT (node processing, network, broker) |
| `latency_stored_p50_ms`, `_p99_ms`, `_max_ms` | InfluxDB confirming the write that completes the packet (adds the worker queue, batching and the write) |

A sample also waits up to one packet (64 frames, ~2.1 ms) on the node before its packet is built.

Both delays compare the node's PTP time with the server's clock, so the server must follow the same grandmaster (`ptp4l` + `phc2sys`). Values aren't clamped, so a negative latency means the server's clock isn't synchronised. To pull a test window:

```flux
from(bucket: "waveform_1s")
  |> range(start: 2026-10-13T00:00:00Z, stop: 2026-10-14T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "daq2_ingest" and r._field =~ /^latency_/)
```

### Testing

Unit tests, run in a throwaway container:

```bash
cd daq2-ingest
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -q -r requirements.txt pytest && python -m pytest -q"
```

Load test: publish synthetic packets for two nodes at the real rate, then watch `docker compose logs -f daq2-ingest`:

```bash
docker run --rm --network ug-historian_default -v "$PWD/daq2-ingest":/app -w /app -e PYTHONPATH=/app \
  python:3.12-slim sh -c "pip install -q -r requirements.txt && python tests/load.py --host mosquitto --nodes 2 --seconds 60"
```

The load test writes to nodes `load_01`/`load_02`, which stay in the buckets until retention expires.

Demo data: `tests/mock_node.py` publishes one realistic node (three-phase 230 V supply feeding a small commercial board, with PTP servo status) for screenshots and demos. `--steady fridge,pump` holds a constant load; leave it out for appliances switching on and off with motor inrush (the load model is `tests/feeder.py`). `--backfill 600` first publishes 10 minutes of history at twice real time. Use `--tai-utc-offset 0` against a grandmaster that sends UTC (the home ptp4l setup).

```bash
docker run --rm --network ug-historian_default -v "$PWD/daq2-ingest":/app -w /app -e PYTHONPATH=/app \
  python:3.12-slim sh -c "pip install -q -r requirements.txt && python tests/mock_node.py --host mosquitto --node node_02 --backfill 600 --steady fridge,pump"
```

---

## Accessing the InfluxDB UI

InfluxDB is bound to `127.0.0.1` on purpose, so it isn't reachable directly from the LAN. Use an SSH tunnel from your own machine:

```bash
ssh -L 8086:localhost:8086 <user>@<server-ip>
```

Then open `http://localhost:8086` and log in with `INFLUXDB_ADMIN_USER` / `INFLUXDB_ADMIN_PASSWORD`. **Data Explorer** is handy for checking that data is arriving.

## Day-to-day operations

```bash
docker compose ps                 # status
docker compose restart telegraf   # e.g. after editing telegraf.conf
docker compose up -d --build daq2-ingest   # after changing daq2-ingest code
docker compose logs -f daq2-ingest          # stats line every 10 s
docker compose down               # stop everything (data is kept)
docker compose up -d              # start again
docker compose pull && docker compose up -d   # update images
```

Data lives in the Docker volumes `ug-historian_influxdb-data` and `ug-historian_grafana-data` (prefix = lower-cased folder name; check with `docker volume ls`), plus `mosquitto/data/` and `mosquitto/log/` in the repo.

### Resetting everything

**This deletes all stored data, dashboards and users.** Use it if you changed `.env` and need the new passwords/token applied:

```bash
docker compose down -v
docker compose up -d
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Grafana "Save & test" fails with *unauthorized* | Wrong token. It must match `INFLUXDB_TOKEN` **as it was at first start**. If you've changed `.env` since, either reset or copy the token from the InfluxDB UI (Load Data → API Tokens) into `.env` and run `docker compose up -d grafana`. |
| Telegraf logs `401 Unauthorized` | Same token mismatch as above. Fix `.env`, then `docker compose up -d telegraf`. |
| Grafana login fails with the `.env` password | `GRAFANA_ADMIN_PASSWORD` was changed after first start. Reset it with `docker exec -it grafana grafana cli admin reset-admin-password <new>`. |
| InfluxDB container keeps restarting | `INFLUXDB_ADMIN_PASSWORD` is shorter than 8 characters. Fix it, then `docker compose down -v && docker compose up -d`. |
| Can't reach Grafana / MQTT from another machine | Check the port binding (step 4). If `ufw` is enabled: `sudo ufw allow 3000/tcp` and `sudo ufw allow 1883/tcp`. Note that Docker-published ports can bypass `ufw` rules, so don't rely on `ufw` alone to *block* them. |
| Data in mosquitto_sub but not in Grafana | Check `docker compose logs telegraf` for JSON parse errors. Check that `ts` is in nanoseconds and the Grafana time range covers it. |
| `daq2-ingest` stats show `mqtt_drops` (on the `receiver` line) or `buffer_drop_lines` above 0 | A worker or InfluxDB isn't keeping up. Check `queue_max` on the receiver line, the server's CPU (`docker stats`) and disk I/O. More nodes than the server can handle (see *Capacity* in section 10) causes this. |
| `daq2-ingest` stats show `implausible_time` above 0 | The node's clock is far from the server's, usually for the first ~15 s after it boots, before its first PTP step. If it persists, check the grandmaster and the server's NTP. |
| `daq2-ingest` stats show `invalid_time` above 0 | The node's PTP clock stepped or its timestamp jumped, so those packets were skipped. Normal for a few seconds after a node boots. (`time_flagged` at ~1 per second is normal: those are kept.) |
| `daq2-ingest` stats show `gap_frames` above 0 | The node acquired data it never delivered. Check the node's UART for `daq2_drop`/`drop`. |

## Not yet implemented

- MQTT authentication/ACLs: the broker allows anonymous clients.
- The `telemetry_1m` bucket and `downsample_1m` Flux task for the JSON snapshots. Long-term aggregates come from `daq2-ingest` (`waveform_1s`) instead.
- A separate read-only InfluxDB token for Grafana and a write-only token for Telegraf. Both currently use the admin token.

## Repository layout

```
.
├── docker-compose.yml                         # the whole stack
├── .env                                       # secrets (you create this; git-ignored)
├── mosquitto/config/mosquitto.conf            # broker config
├── telegraf/telegraf.conf                     # MQTT → InfluxDB mapping
├── grafana/provisioning/datasources/          # auto-provisioned Grafana data source
├── grafana/provisioning/dashboards/           # auto-provisioned Node View and Node Compare dashboards
├── daq2-ingest/                               # full-rate DAQ2 decoder service (Python)
│   ├── daq2.py                                # packet decoding, line protocol, 1 s aggregates
│   ├── ingest.py                              # MQTT subscriber and batched InfluxDB writer
│   └── tests/                                 # unit tests, a full-rate load generator, a demo node
└── documentation/                             # MQTT and InfluxDB design docs (LaTeX)
```
