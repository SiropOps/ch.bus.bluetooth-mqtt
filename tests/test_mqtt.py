from types import SimpleNamespace
from unittest.mock import Mock

import paho.mqtt.client as mqtt

from bluetooth_gateway.config import Config
from bluetooth_gateway.publishing import MqttPublisher


def client():
    result = Mock()
    result.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, wait_for_publish=Mock(), is_published=lambda: True)
    return result


def test_mqtt_outage_retains_latest_and_replays_on_connect():
    transport = client()
    publisher = MqttPublisher(Config(), transport)
    publisher.start()
    transport.connect_async.assert_called_once_with("127.0.0.1", 1883, keepalive=60)
    for i in range(10000):
        publisher.publish("van/test/value", str(i))
    assert len(publisher.latest) == len(publisher.pending) == 1
    transport.publish.assert_not_called()
    publisher.on_connect(transport, None, None, 0, None)
    transport.publish.assert_any_call("van/test/value", "9999", qos=1, retain=True)
    transport.publish.assert_any_call("van/bluetooth/status", "online", qos=1, retain=True)
    assert not publisher.pending
    publisher.on_disconnect(transport, None, None, 1, None)
    publisher.publish("van/test/value", "10000")
    publisher.on_connect(transport, None, None, 0, None)
    transport.publish.assert_any_call("van/test/value", "10000", qos=1, retain=True)


def test_mqtt_full_queue_retries_without_losing_latest():
    transport = client()
    publisher = MqttPublisher(Config(), transport)
    publisher.connected = True
    transport.publish.return_value.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    publisher.publish("van/test/value", "1")
    publisher.publish("van/test/value", "2")
    assert publisher.pending == {"van/test/value":"2"}
    transport.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    publisher.flush()
    assert not publisher.pending
    transport.publish.assert_called_with("van/test/value", "2", qos=1, retain=True)


def test_mqtt_exception_never_prints_credentials(caplog):
    transport = client()
    publisher = MqttPublisher(Config(), transport)
    publisher.connected = True
    transport.publish.side_effect = OSError("private-password")
    publisher.publish("van/test/value", "1")
    assert publisher.pending and not publisher.connected
    assert "private-password" not in caplog.text


def test_retained_will_and_clean_shutdown():
    transport = client()
    publisher = MqttPublisher(Config(), transport)
    transport.will_set.assert_called_once_with("van/bluetooth/status", "offline", qos=1, retain=True)
    publisher.on_connect(transport, None, None, 0, None)
    publisher.close()
    transport.publish.assert_any_call("van/bluetooth/status", "offline", qos=1, retain=True)
    transport.disconnect.assert_called_once()
    transport.loop_stop.assert_called_once()


def test_topic_buffer_is_bounded():
    publisher = MqttPublisher(Config(), client(), max_topics=2)
    for i in range(100):
        publisher.publish(f"van/test/{i}", str(i))
    assert len(publisher.latest) == len(publisher.pending) == 2


def test_unacknowledged_offline_closes_transport_to_preserve_last_will():
    transport = client()
    transport.publish.return_value.is_published = lambda: False
    publisher = MqttPublisher(Config(), transport)
    publisher.connected = True
    publisher.close()
    transport.disconnect.assert_not_called()
    transport.socket.return_value.shutdown.assert_called_once()
    transport.socket.return_value.close.assert_called_once()
    transport.loop_stop.assert_called_once()


def test_shutdown_network_error_still_closes_socket_and_thread(caplog):
    transport = client()
    transport.publish.side_effect = OSError("private-password")
    publisher = MqttPublisher(Config(), transport)
    publisher.connected = True
    publisher.close()
    transport.socket.return_value.close.assert_called_once()
    transport.loop_stop.assert_called_once()
    assert "private-password" not in caplog.text
