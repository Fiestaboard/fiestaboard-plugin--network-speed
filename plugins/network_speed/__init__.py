"""Display your internet connection speed using a local Speedtest CLI check."""

from __future__ import annotations

import logging
from typing import Any, Dict, List
import requests
import datetime
import threading

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

USER_AGENT = "FiestaBoard Network Speed Plugin (https://github.com/Fiestaboard/fiestaboard-plugin--network-speed)"


class NetworkSpeedPlugin(PluginBase):
    """Network Speed plugin for FiestaBoard."""

    @property
    def plugin_id(self) -> str:
        return "network_speed"

    def fetch_data(self) -> PluginResult:
        try:
            import speedtest as st_module

            def run_test():
                s = st_module.Speedtest(secure=True)
                s.get_best_server()
                s.download()
                s.upload()
                return s.results.dict()

            result = run_test()
            download_mbps = round(result["download"] / 1_000_000, 1)
            upload_mbps = round(result["upload"] / 1_000_000, 1)
            ping_ms = round(result["ping"], 1)
            last_tested = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

            return PluginResult(
                available=True,
                data={
                    "download_mbps": download_mbps,
                    "upload_mbps": upload_mbps,
                    "ping_ms": ping_ms,
                    "last_tested": last_tested,
                },
            )
        except ImportError:
            return PluginResult(
                available=False,
                error="speedtest-cli package not installed",
            )
        except Exception as e:
            logger.exception("Error running speed test")
            return PluginResult(available=False, error=str(e))

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        return []

    def cleanup(self) -> None:
        pass
