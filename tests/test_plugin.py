"""Tests for the network_speed plugin."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, Mock

import pytest

from plugins.network_speed import NetworkSpeedPlugin
from src.plugins.base import PluginResult

MANIFEST = json.loads("""
{
    "id": "network_speed",
    "name": "Network Speed",
    "version": "0.1.0",
    "settings_schema": {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "title": "Enabled",
                "default": false
            },
            "refresh_seconds": {
                "type": "integer",
                "title": "Refresh Interval (seconds)",
                "description": "How often to run a speed test. Tests take 20\u201360 seconds; minimum 1800s.",
                "default": 3600,
                "minimum": 1800
            }
        },
        "required": []
    }
}
""")

SAMPLE_RESPONSE = json.loads("""
{
    "download": 250400000,
    "upload": 18700000,
    "ping": 14.3,
    "server": {
        "name": "Test Server",
        "country": "United States"
    }
}
""")


@pytest.fixture
def plugin():
    return NetworkSpeedPlugin(MANIFEST)


@pytest.fixture
def configured_plugin():
    p = NetworkSpeedPlugin(MANIFEST)
    p.config = json.loads("""
{}
""")
    return p


class TestNetworkSpeedPlugin:

    def test_plugin_id(self, plugin):
        assert plugin.plugin_id == "network_speed"

    def test_manifest_valid(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            m = json.load(f)
        for field in ("id", "name", "version"):
            assert field in m

    def test_fetch_data_success(self, configured_plugin):
        from unittest.mock import MagicMock
        mock_st = MagicMock()
        mock_instance = MagicMock()
        mock_instance.results.dict.return_value = {
            "download": 95_000_000,
            "upload": 20_000_000,
            "ping": 12.5,
        }
        mock_st.Speedtest.return_value = mock_instance
        import sys
        with patch.dict(sys.modules, {"speedtest": mock_st}):
            result = configured_plugin.fetch_data()

        assert result.available is True
        assert result.error is None
        assert result.data is not None
        assert "download_mbps" in result.data, "missing variable: download_mbps"
        assert "upload_mbps" in result.data, "missing variable: upload_mbps"
        assert "ping_ms" in result.data, "missing variable: ping_ms"
        assert "last_tested" in result.data, "missing variable: last_tested"

    @pytest.mark.skip(reason="plugin does not use requests.get")
    def test_fetch_data_network_error(self, configured_plugin):
        pass

    @pytest.mark.skip(reason="plugin does not use requests.get")
    def test_fetch_data_bad_json(self, configured_plugin):
        pass

