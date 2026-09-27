import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.configure_oneil_execution import publish

ROOT = Path(__file__).resolve().parents[1]


def test_shadow_configuration_and_off_worker_smoke_never_authorize_live(tmp_path):
    config, health = tmp_path / "config.json", tmp_path / "health.json"
    command = [sys.executable, str(ROOT / "tools/configure_oneil_execution.py"), "--mode", "SHADOW",
               "--accounts", "primary", "--output", str(config), "--since", "2026-09-27T00:00:00Z"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["orders_submitted"] == 0
    assert json.loads(config.read_text())["live_approval"] is None
    assert subprocess.run(command, capture_output=True).returncode != 0
    checksum = hashlib.sha256(config.read_bytes()).hexdigest()
    subprocess.run([sys.executable, str(ROOT / "tools/configure_oneil_execution.py"), "--mode", "OFF",
                    "--output", str(config), "--expected-hash", checksum], check=True, capture_output=True)
    result = subprocess.run([sys.executable, str(ROOT / "tools/run_oneil_execution.py"), "--once",
                             "--config", str(config), "--health", str(health)],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["status"] == "OFF"
    assert json.loads(health.read_text())["technical_readiness"]["live_ready"] is False
    assert list(tmp_path.glob("config.json.before-*"))


def test_live_mode_refuses_without_approval_and_health_cannot_overwrite_config(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"mode":"OFF"}')
    old = config.read_bytes()
    result = subprocess.run([sys.executable, str(ROOT / "tools/configure_oneil_execution.py"),
                             "--mode", "LIVE", "--output", str(config)], capture_output=True)
    assert result.returncode != 0 and config.read_bytes() == old
    result = subprocess.run([sys.executable, str(ROOT / "tools/run_oneil_execution.py"), "--once",
                             "--config", str(config), "--health", str(config)], capture_output=True)
    assert result.returncode != 0 and config.read_bytes() == old


def test_mode_writer_requires_compare_and_swap_and_json(tmp_path):
    path = tmp_path / "config.json"
    checksum = publish(path, {"mode": "OFF"})
    with pytest.raises(ValueError):
        publish(path, {"mode": "SHADOW"}, expected_hash="wrong")
    publish(path, {"mode": "SHADOW"}, expected_hash=checksum)
    with pytest.raises(ValueError):
        publish(path, {"mode": "OFF"}, expected_hash=checksum)
    with pytest.raises(ValueError):
        publish(tmp_path / "holdings.sqlite", {})
