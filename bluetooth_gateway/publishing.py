from __future__ import annotations

import json
import logging
import socket
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import Config, Sensor, topic_safe

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish_sensor(publisher, base: str, sensor: Sensor, values: dict, rssi=None) -> None:
    data = {
        "timestamp": utc_now(), "name": sensor.name, "address": sensor.address,
        "protocol": sensor.protocol, **values,
    }
    if rssi is not None:
        data["rssi"] = rssi
    device_topic = f"{base}/{sensor.identifier}"
    publish_values(publisher, device_topic, data)
    publisher.publish(f"{device_topic}/availability", "online")


def publish_values(publisher, topic: str, data: dict) -> None:
    publisher.publish(topic, json.dumps(data, ensure_ascii=False, allow_nan=False))
    for key, value in data.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            publisher.publish(f"{topic}/{key}", json.dumps(value, ensure_ascii=False, allow_nan=False))


def publish_victron(publisher, base: str, reading: dict) -> None:
    payload = reading["payload"]
    name = reading["name"]
    model_name = payload.get("model_name", name)
    data = {
        "timestamp": reading.get("timestamp", utc_now()), "name": name,
        "address": reading["address"], "rssi": reading["rssi"],
        "model_name": model_name, **payload,
    }
    device_topic = f"{base}/{topic_safe(name, ascii_names=False)}"
    publish_values(publisher, device_topic, data)
    model_topic = f"{base}/{topic_safe(model_name, ascii_names=False)}"
    if model_topic != device_topic:
        publisher.publish(model_topic, json.dumps(data, ensure_ascii=False, allow_nan=False))


class MqttPublisher:
    """Bounded latest-value buffer. The MQTT network thread owns reconnects.

    Failed publications remain pending, including when Paho's queue is full.
    Reconnect replays the latest retained value of each topic. No event backlog
    grows for every advertisement while the broker is unavailable.
    """

    def __init__(self, config: Config, client=None, max_topics: int = 4096):
        self.config = config
        self.client = client or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bluetooth-mqtt")
        self.lock = threading.RLock()
        self.latest: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.connected = False
        self.closing = False
        self.max_topics = max_topics
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.will_set(config.status_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.max_queued_messages_set(1000)
        if config.mqtt_username:
            self.client.username_pw_set(config.mqtt_username, config.mqtt_password)

    @property
    def status_topics(self):
        return (self.config.status_topic, f"{self.config.victron_topic}/status",
                f"{self.config.temperature_topic}/status")

    def start(self):
        self.client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        with self.lock:
            self.connected = reason_code == 0
            if not self.connected:
                logger.warning("MQTT connection refused")
                return
            for topic in self.status_topics:
                self.latest[topic] = "offline" if self.closing else "online"
            self.pending.update(self.latest)
        logger.info("MQTT connected")
        self.flush()

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self.lock:
            self.connected = False
        if not self.closing:
            logger.warning("MQTT disconnected; retaining latest readings for reconnect")

    def publish(self, topic: str, payload: str):
        with self.lock:
            if topic not in self.latest and len(self.latest) >= self.max_topics:
                logger.error("MQTT topic buffer full; check configured device identities")
                return
            self.latest[topic] = payload
            self.pending[topic] = payload
        self.flush()

    def flush(self):
        with self.lock:
            if not self.connected:
                return
            for topic, payload in list(self.pending.items()):
                try:
                    info = self.client.publish(topic, payload, qos=1, retain=True)
                except (OSError, RuntimeError, ValueError):
                    self.connected = False
                    logger.warning("MQTT publication deferred")
                    return
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    return
                del self.pending[topic]

    def close(self):
        with self.lock:
            self.closing = True
        # Lifecycle publications bypass pending telemetry so a full reconnect
        # backlog cannot indefinitely delay a clean offline announcement.
        infos = []
        acknowledged = True
        if self.connected:
            for topic in self.status_topics:
                try:
                    infos.append(self.client.publish(topic, "offline", qos=1, retain=True))
                except (OSError, RuntimeError, ValueError):
                    acknowledged = False
            for info in infos:
                try:
                    info.wait_for_publish(timeout=2)
                    acknowledged = acknowledged and info.is_published()
                except (OSError, RuntimeError, ValueError):
                    acknowledged = False
        if acknowledged:
            self.client.disconnect()
        else:
            # If offline could not be acknowledged (including a full queue),
            # close the transport without MQTT DISCONNECT so the broker uses
            # our Last Will. This also unblocks an unresponsive network loop.
            transport = self.client.socket()
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                transport.close()
            else:
                self.client.disconnect()
        self.client.loop_stop()
