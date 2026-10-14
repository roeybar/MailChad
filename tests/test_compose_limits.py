"""Every compose service keeps a hard memory limit and no CPU pin."""
from __future__ import annotations
import re
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "docker-compose.yml": {"terminal": "1g"},
    "docker-compose.dev.yml": {"dynamodb-local": "512m", "localstack": "768m",
                               "cloud": "512m", "ep-v3-dispatcher": "384m", "terminal": "1g"},
}
CPU_KEYS = {"cpuset", "cpus", "cpu_count", "cpu_percent", "cpu_quota", "cpu_period", "deploy"}

def services(name):
    """Service name -> {four-space key: raw value}, read line by line (no yaml dependency)."""
    out, current, in_services = {}, None, False
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_services, current = line == "services:", None
            continue
        if not in_services:
            continue
        m = re.fullmatch(r"  ([A-Za-z0-9._-]+):", line)
        if m:
            current = out.setdefault(m.group(1), {})
            continue
        m = re.fullmatch(r"    ([a-z_]+):\s*(.*)", line)
        if m and current is not None:
            current[m.group(1)] = m.group(2)
    return out

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_parser_sees_every_service(name):
    # This fails if a service is added to either file without adding it to EXPECTED.
    assert set(services(name)) == set(EXPECTED[name])

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_service_has_its_mem_limit(name):
    # This fails if any mem_limit line is deleted or its value changes.
    found = services(name)
    for svc, limit in EXPECTED[name].items():
        assert found[svc].get("mem_limit") == limit, (name, svc)

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_service_pins_cpu(name):
    # This fails if cpuset, cpus or deploy is added to any service.
    for svc, keys in services(name).items():
        assert not CPU_KEYS & set(keys), (name, svc, sorted(CPU_KEYS & set(keys)))

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_line_parser_agrees_with_yaml(name):
    # This fails if a service uses a shape the line parser misses.
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert {s: str(v.get("mem_limit")) for s, v in doc["services"].items()} == \
           {s: k.get("mem_limit") for s, k in services(name).items()}
