import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class Publications:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload):
        self.messages.append((topic, payload))

    def flush(self):
        pass

    def value(self, topic):
        return next(payload for target, payload in reversed(self.messages) if target == topic)


@pytest.fixture
def publications():
    return Publications()


def frames(filename):
    return json.loads((Path(__file__).parent / "fixtures" / filename).read_text(encoding="utf-8"))


def advertisement(row):
    device = SimpleNamespace(address=row["address"], name=row["name"])
    adv = SimpleNamespace(manufacturer_data={row["manufacturer_id"]: bytes.fromhex(row["data"])},
                          local_name=row["name"], rssi=-42)
    return device, adv
