import paho.mqtt.client as mqtt
import json
import time
import random
import math

BROKER = "localhost"
PORT = 1883

# Nominal values per IEEE 1459-2010 notation
VOLTAGE_NOMINAL = 230.0   # Vrms line-to-neutral
CURRENT_NOMINAL = 10.0    # A
FREQUENCY = 50.0          # Hz

NODES = ["node_1", "node_2"]

# PTP simulation state per node
ptp_state = {
    "node_1": {"offset_ns": 50.0, "path_delay_ns": 1200.0, "locked": True},
    "node_2": {"offset_ns": 75.0, "path_delay_ns": 1350.0, "locked": True},
}

client = mqtt.Client()
client.connect(BROKER, PORT)
client.loop_start()

print("Simulator running... Ctrl+C to stop")
print("Publishing: voltage (va,vb,vc), current (ia,ib,ic), ptp_status per node")

def publish(topic, value):
    ts = time.time_ns()
    payload = json.dumps({"ts": ts, "v": round(value, 6)})
    client.publish(topic, payload)

def publish_string(topic, value_str):
    """For sync_state which is a string field — published as separate topic."""
    ts = time.time_ns()
    payload = json.dumps({"ts": ts, "v": value_str})
    client.publish(topic, payload)

t = 0
try:
    while True:
        ts_loop = time.time_ns()

        for node in NODES:
            # Phase angle offsets for three-phase (120 degrees apart)
            phase_a = math.sin(2 * math.pi * FREQUENCY * t * 0.01)
            phase_b = math.sin(2 * math.pi * FREQUENCY * t * 0.01 - 2*math.pi/3)
            phase_c = math.sin(2 * math.pi * FREQUENCY * t * 0.01 + 2*math.pi/3)

            # Small per-node offset to simulate real differences between nodes
            node_offset = 0.5 if node == "node_2" else 0.0

            # Voltage samples (V)
            publish(f"microgrid/{node}/voltage/va", VOLTAGE_NOMINAL * phase_a + node_offset + random.gauss(0, 0.05))
            publish(f"microgrid/{node}/voltage/vb", VOLTAGE_NOMINAL * phase_b + node_offset + random.gauss(0, 0.05))
            publish(f"microgrid/{node}/voltage/vc", VOLTAGE_NOMINAL * phase_c + node_offset + random.gauss(0, 0.05))

            # Current samples (A)
            publish(f"microgrid/{node}/current/ia", CURRENT_NOMINAL * phase_a + random.gauss(0, 0.02))
            publish(f"microgrid/{node}/current/ib", CURRENT_NOMINAL * phase_b + random.gauss(0, 0.02))
            publish(f"microgrid/{node}/current/ic", CURRENT_NOMINAL * phase_c + random.gauss(0, 0.02))

            # PTP status — simulate realistic sub-microsecond offset with slow drift
            ptp = ptp_state[node]
            ptp["offset_ns"] += random.gauss(0, 2.0)
            ptp["offset_ns"] = max(-500.0, min(500.0, ptp["offset_ns"]))  # clamp to ±500ns
            ptp["path_delay_ns"] += random.gauss(0, 0.5)
            ptp["path_delay_ns"] = max(800.0, min(2000.0, ptp["path_delay_ns"]))

            publish(f"microgrid/{node}/ptp_status/offset_ns",     ptp["offset_ns"])
            publish(f"microgrid/{node}/ptp_status/path_delay_ns", ptp["path_delay_ns"])
            # sync_state as numeric: 1=LOCKED, 2=ACQUIRING, 3=HOLDOVER
            sync_val = 1.0 if ptp["locked"] else 2.0
            publish(f"microgrid/{node}/ptp_status/sync_state", sync_val)

        t += 1
        time.sleep(0.1)  # 10 Hz per channel

except KeyboardInterrupt:
    print("\nStopped.")
    client.loop_stop()
    client.disconnect()
