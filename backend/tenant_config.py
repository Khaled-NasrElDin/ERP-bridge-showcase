import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("tenants.json")


def load_tenants():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)
