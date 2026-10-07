# uG-Historian

Time-series historian for the microgrid DAQ testbed. STM32 DAQ nodes publish PTP-timestamped samples over MQTT. Telegraf bridges them into InfluxDB, and Grafana displays them.

```
DAQ nodes ──MQTT──▶ Mosquitto ──▶ Telegraf ──▶ InfluxDB v2 ──Flux──▶ Grafana
 (or sim.py)         :1883                       :8086                 :3000
```

All four services run as Docker containers on a single historian server, managed by `docker-compose.yml`.

Tested on **Ubuntu Server 24.04.4 LTS**.

| Service   | Image                   | Host port            | Purpose                              |
|-----------|-------------------------|----------------------|--------------------------------------|
| mosquitto | `eclipse-mosquitto:2`   | `127.0.0.1:1883`     | MQTT broker                          |
| influxdb  | `influxdb:2`            | `127.0.0.1:8086`     | Time-series database + admin UI      |
| telegraf  | `telegraf:latest`       | none                 | MQTT → InfluxDB bridge               |
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
| `INFLUXDB_TOKEN`          | InfluxDB, Telegraf   | Admin API token. Telegraf writes with it, and you paste it into Grafana in step 7. Any long random string works. |
| `GRAFANA_ADMIN_PASSWORD`  | Grafana              | Password for the Grafana `admin` user. |

> **These values are only applied the first time the containers start.** InfluxDB and Grafana store their users and tokens in Docker volumes. Changing `.env` later does **not** change the password or token. See [Resetting](#resetting-everything) if you need to start over.

## 4. Settings you may need to change

Everything else is hard-coded and consistent across files. Only change these if you know why:

| Setting | Where | Default | When to change |
|---------|-------|---------|----------------|
| Mosquitto host binding | `docker-compose.yml` → `mosquitto.ports` | `127.0.0.1:1883:1883` | **Change to `"1883:1883"` before connecting real DAQ boards.** With the default, only clients on the server itself (e.g. `sim.py`) can connect. The `rohan-local` branch already has this change. |
| InfluxDB org | `docker-compose.yml`, `telegraf/telegraf.conf`, `grafana/provisioning/datasources/influxdb.yml` | `qut-microgrid` | Must match in all three files. |
| InfluxDB bucket | same three files | `telemetry` | Must match in all three files. |
| Raw data retention | `docker-compose.yml` → `DOCKER_INFLUXDB_INIT_RETENTION` | `90d` | Only applies on first start. Change it later in the InfluxDB UI (Load Data → Buckets). |
| MQTT topic filter | `telegraf/telegraf.conf` → `topics` | `microgrid/#` | Only if the topic scheme changes. |
| Telegraf flush interval | `telegraf/telegraf.conf` → `[agent] flush_interval` | `10s` | Lower it (e.g. `1s`) for more real-time dashboards. |

## 5. Start the stack

```bash
docker compose up -d
docker compose ps
```

All four containers should show `running`. On first start InfluxDB takes ~10–20 s to initialise, and Telegraf may log a few connection errors until InfluxDB is ready. That's expected.

Useful log commands:

```bash
docker compose logs -f telegraf    # MQTT → InfluxDB bridge (most useful for debugging)
docker compose logs -f influxdb
docker compose logs -f mosquitto
docker compose logs -f grafana
```

## 6. Send test data with the simulator

`sim/sim.py` publishes fake 3-phase voltage/current and PTP status for `node_1` and `node_2` at 10 Hz. Run it **on the server**, because it connects to `localhost:1883`.

```bash
sudo apt-get install -y python3-paho-mqtt
python3 sim/sim.py
```

> Use the apt package (paho-mqtt 1.6). The script uses the paho-mqtt 1.x API and fails with `pip install paho-mqtt` (2.x). If you must use pip, install it in a venv with `pip install "paho-mqtt<2"`.

To confirm messages are reaching the broker, run this in a second terminal:

```bash
docker exec -it mosquitto mosquitto_sub -t 'microgrid/#' -v
```

Leave the simulator running while you set up Grafana.

## 7. Log in to Grafana and connect InfluxDB

### Log in

Open **`http://<server-ip>:3000`** in a browser (find the IP with `hostname -I`).

- **Username:** `admin`
- **Password:** the `GRAFANA_ADMIN_PASSWORD` from `.env`

(If `GRAFANA_ADMIN_PASSWORD` was left empty, Grafana's default is `admin` / `admin`, and it will prompt you to set a new password.)

If the page doesn't load from another machine, see [Troubleshooting](#troubleshooting).

### Set up the InfluxDB data source

A data source called **InfluxDB (Flux)** is provisioned automatically from `grafana/provisioning/datasources/influxdb.yml`. **However, its token is empty.** The Grafana container isn't given `INFLUXDB_TOKEN` in `docker-compose.yml`, so the `${INFLUXDB_TOKEN}` placeholder resolves to nothing. Fill it in by hand:

1. Go to **Connections → Data sources → InfluxDB (Flux)**.
   (If it's missing, click **Add new data source → InfluxDB** instead.)
2. Check or set these fields:

   | Field              | Value                    |
   |--------------------|--------------------------|
   | Query language     | `Flux`                   |
   | URL                | `http://influxdb:8086`   |
   | Basic auth         | off                      |
   | Organization       | `qut-microgrid`          |
   | Token              | `INFLUXDB_TOKEN` from `.env` |
   | Default Bucket     | `telemetry`              |

   Use `http://influxdb:8086`, **not** `localhost`. Grafana reaches InfluxDB over the internal Docker network, by container name.
3. Click **Save & test**. You should see *"datasource is working. N buckets found"*.

> **Permanent fix (recommended):** add `- INFLUXDB_TOKEN=${INFLUXDB_TOKEN}` under `grafana.environment` in `docker-compose.yml`, then run `docker compose up -d grafana`. The provisioned data source then gets the token automatically, and steps 1–3 aren't needed. Without it, the provisioned data source can lose its token again when Grafana restarts.

### Build a first panel

1. **Dashboards → New → New dashboard → Add visualization**, and select **InfluxDB (Flux)**.
2. Paste a query, e.g. phase-A voltage for both nodes:

   ```flux
   from(bucket: "telemetry")
     |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
     |> filter(fn: (r) => r._measurement == "mqtt_consumer")
     |> filter(fn: (r) => r.subsystem == "voltage" and r.metric == "va")
     |> filter(fn: (r) => r._field == "v")
     |> keep(columns: ["_time", "_value", "node_id"])
     |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
   ```

3. Set the time range to *Last 5 minutes* and refresh. You should see the simulated sine waves.

## 8. Data model (what's actually in InfluxDB)

An MQTT message on `microgrid/<node_id>/<subsystem>/<metric>` with payload `{"ts": <unix_ns>, "v": <float>}` becomes:

| InfluxDB column | Value | Example |
|-----------------|-------|---------|
| `_measurement`  | always `mqtt_consumer` (Telegraf default) | `mqtt_consumer` |
| `node_id` tag   | topic segment 2 | `node_1` |
| `subsystem` tag | topic segment 3 | `voltage`, `current`, `ptp_status` |
| `metric` tag    | topic segment 4 | `va`, `ib`, `offset_ns` |
| `topic` tag     | full topic | `microgrid/node_1/voltage/va` |
| `_field`        | `v` | `v` |
| `_time`         | the `ts` from the payload (the node's PTP time, **not** the time the server received it) | |

> This differs from the design document (`documentation/InfluxDB`), which puts the subsystem in `_measurement` and uses a `measurement` tag. Write queries against the table above.

## 9. Connecting real DAQ nodes

1. Make sure the Mosquitto port is exposed to the LAN (see [step 4](#4-settings-you-may-need-to-change)), then `docker compose up -d`.
2. Point the firmware at `<server-ip>:1883`. **No username/password is needed.** `mosquitto/config/mosquitto.conf` currently has `allow_anonymous true`.
3. Publish to `microgrid/<node_id>/<subsystem>/<metric>` with a JSON payload `{"ts": <unix_ns>, "v": <number>}`, QoS 0.
   - `ts` **must** be Unix time in nanoseconds. Wrong units put points in 1970 or the far future, where they won't show up in Grafana.
   - Only single-sample payloads are supported. The burst format (`"v": [...]`) from the MQTT design doc is not handled by Telegraf yet.

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
| Grafana "Save & test" fails with *unauthorized* | Wrong token. It must match `INFLUXDB_TOKEN` **as it was at first start**. If you've changed `.env` since, either reset or copy the token from the InfluxDB UI (Load Data → API Tokens). |
| Grafana "Save & test" fails with *connection refused* | URL is `localhost`; it must be `http://influxdb:8086`. |
| Telegraf logs `401 Unauthorized` | Same token mismatch as above. Fix `.env`, then `docker compose up -d telegraf`. |
| Grafana login fails with the `.env` password | `GRAFANA_ADMIN_PASSWORD` was changed after first start. Reset it with `docker exec -it grafana grafana cli admin reset-admin-password <new>`. |
| InfluxDB container keeps restarting | `INFLUXDB_ADMIN_PASSWORD` is shorter than 8 characters. Fix it, then `docker compose down -v && docker compose up -d`. |
| Can't reach Grafana / MQTT from another machine | Check the port binding (step 4). If `ufw` is enabled: `sudo ufw allow 3000/tcp` and `sudo ufw allow 1883/tcp`. Note that Docker-published ports can bypass `ufw` rules, so don't rely on `ufw` alone to *block* them. |
| Data in mosquitto_sub but not in Grafana | Check `docker compose logs telegraf` for JSON parse errors. Check that `ts` is in nanoseconds and the Grafana time range covers it. |
| `sim.py` fails with `ValueError: Unsupported callback API version` | paho-mqtt 2.x is installed. Use the apt package or `paho-mqtt<2` (see step 6). |

## Not yet implemented (vs. the design docs)

- MQTT authentication/ACLs: the broker allows anonymous clients.
- The `telemetry_1m` downsampled bucket and the `downsample_1m` Flux task.
- A separate read-only InfluxDB token for Grafana and a write-only token for Telegraf. Both currently use the admin token.
- Burst payload ingestion.
- Provisioned Grafana dashboards. Dashboards are built by hand and stored in the `grafana-data` volume.

## Repository layout

```
.
├── docker-compose.yml                         # the whole stack
├── .env                                       # secrets (you create this; git-ignored)
├── mosquitto/config/mosquitto.conf            # broker config
├── telegraf/telegraf.conf                     # MQTT → InfluxDB mapping
├── grafana/provisioning/datasources/          # auto-provisioned Grafana data source
├── sim/sim.py                                 # fake DAQ node publisher for testing
└── documentation/                             # MQTT and InfluxDB design docs (LaTeX)
```
