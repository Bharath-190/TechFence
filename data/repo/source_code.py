"""Fake internal source file for the TaskFence demo (Acme Anvil Co)."""


def region_revenue(rows):
    total = 0
    for row in rows:
        total += row["revenue"]
    return total


def normalize_region(name):
    return (name or "").strip().lower()


# Fake placeholder credential for the demo. Never a real secret.
API_KEY = "sk-live-0000000000deadbeef0000000000beef"

REGION_CODES = {"north": "N", "south": "S", "east": "E", "west": "W", "central": "C"}


def main():
    print("demo only - no real logic here")
