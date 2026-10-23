"""scripts/sim_parallel.py: sharded sim runs stay fail-closed (no pytest, no docker)."""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.sim_parallel import partition

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sim_parallel.py"

FAKE = """\
import sys
from pathlib import Path
IDS = ["tests/a.py::t%d" % i for i in range(7)]
argv = sys.argv[1:]
if "--collect-only" in argv:
    print("\\n".join(IDS)); print("7 tests collected"); sys.exit(0)
xml = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
ran = [a for a in argv if a.startswith("tests/")]
if "DROP" in __import__("os").environ and ran:
    ran = ran[:-1]
cases = "".join('<testcase classname="a" name="%s"/>' % r.split("::")[1] for r in ran)
Path(xml).write_text('<testsuites><testsuite name="s">%s</testsuite></testsuites>' % cases)
"""


def _run(tmp_path, env_extra=None):
    fake = tmp_path / "fake_pytest.py"
    fake.write_text(FAKE)
    out = tmp_path / "sim.xml"
    env = {"PATH": "/usr/bin:/bin", **(env_extra or {})}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--shards", "3", "--junit", str(out), "--", sys.executable, str(fake)],
        capture_output=True, text=True, env=env, check=False)
    return done, out


def test_partition_covers_every_id_once_in_order_within_shard():
    ids = [f"t{i}" for i in range(10)]
    groups = partition(ids, 3)
    assert sorted(i for g in groups for i in g) == sorted(ids)
    assert all(g == sorted(g, key=ids.index) for g in groups)
    assert len(partition(ids[:2], 5)) == 2  # never an empty shard


def test_merged_junit_holds_every_collected_testcase(tmp_path):
    done, out = _run(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    names = sorted(c.get("name") for c in ET.parse(out).getroot().iter("testcase"))
    assert names == [f"t{i}" for i in range(7)]


def test_a_shard_that_silently_drops_a_test_fails_the_run(tmp_path):
    done, _ = _run(tmp_path, {"DROP": "1"})
    assert done.returncode != 0
    assert "collected 7" in done.stderr
