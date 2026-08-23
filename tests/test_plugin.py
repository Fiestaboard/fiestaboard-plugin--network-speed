"""Tests for the network_speed plugin."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from plugins.network_speed import (
    CHUNK,
    DOWNLOAD_SIZES,
    MAX_MAX_TRANSFER_MIB,
    MIN_MAX_TRANSFER_MIB,
    WARMUP_SECONDS,
    NetworkSpeedPlugin,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((REPO_ROOT / "manifest.json").read_text())
MIB = 1 << 20


@pytest.fixture
def plugin():
    return NetworkSpeedPlugin(MANIFEST)


@pytest.fixture
def configured_plugin():
    p = NetworkSpeedPlugin(MANIFEST)
    p.config = {}
    return p


@pytest.fixture
def measured_plugin(configured_plugin):
    """A plugin that already holds a completed measurement."""
    configured_plugin._result = {
        "download_mbps": 250.4,
        "upload_mbps": 18.7,
        "ping_ms": 14.3,
        "last_tested": "2026-05-01 12:00",
    }
    configured_plugin._last_attempt = time.monotonic()
    return configured_plugin


class TestFetchDataIsNonBlocking:
    """The whole point of the rewrite.

    ``build_template_context`` gives every plugin 15 seconds, and a real speed
    test takes longer than that. Measuring inline meant the result was never
    delivered, so the measurement has to happen off the render path.
    """

    def test_fetch_data_does_not_wait_for_the_measurement(self, configured_plugin):
        release = threading.Event()
        entered = threading.Event()

        def slow_measure(_max_bytes):
            entered.set()
            release.wait(timeout=30)
            return {"download_mbps": 1.0, "upload_mbps": 1.0, "ping_ms": 1.0, "last_tested": "x"}

        try:
            with patch.object(NetworkSpeedPlugin, "measure", staticmethod(slow_measure)):
                started = time.monotonic()
                result = configured_plugin.fetch_data()
                elapsed = time.monotonic() - started

                assert entered.wait(timeout=5), "measurement never started"
                assert elapsed < 1.0, f"fetch_data blocked {elapsed:.1f}s on the speed test"
                assert result.available is False
                assert "in progress" in result.error
        finally:
            release.set()

    def test_fetch_data_serves_the_previous_result_while_a_test_runs(self, measured_plugin):
        """A refresh must not blank the board for the duration of the test."""
        release = threading.Event()

        def slow_measure(_max_bytes):
            release.wait(timeout=30)
            return {"download_mbps": 9.9, "upload_mbps": 9.9, "ping_ms": 9.9, "last_tested": "later"}

        measured_plugin._last_attempt = time.monotonic() - 100000  # due for a re-test
        try:
            with patch.object(NetworkSpeedPlugin, "measure", staticmethod(slow_measure)):
                result = measured_plugin.fetch_data()
                assert result.available is True
                assert result.data["download_mbps"] == 250.4
                assert measured_plugin._thread.is_alive()
        finally:
            release.set()

    def test_completed_measurement_becomes_the_reported_data(self, configured_plugin):
        payload = {"download_mbps": 95.0, "upload_mbps": 20.0, "ping_ms": 12.5, "last_tested": "2026-05-01 12:00"}
        with patch.object(NetworkSpeedPlugin, "measure", staticmethod(lambda _b: payload)):
            configured_plugin.fetch_data()
            configured_plugin._thread.join(timeout=5)
            result = configured_plugin.fetch_data()

        assert result.available is True
        assert result.data == payload


class TestMeasurementScheduling:
    def test_only_one_measurement_runs_at_a_time(self, configured_plugin):
        release = threading.Event()
        starts = []

        def slow_measure(_max_bytes):
            starts.append(1)
            release.wait(timeout=30)
            return {"download_mbps": 1.0, "upload_mbps": 1.0, "ping_ms": 1.0, "last_tested": "x"}

        try:
            with patch.object(NetworkSpeedPlugin, "measure", staticmethod(slow_measure)):
                for _ in range(20):
                    configured_plugin.fetch_data()
                time.sleep(0.2)
                assert len(starts) == 1, f"{len(starts)} concurrent speed tests were started"
        finally:
            release.set()

    def test_a_failed_test_is_not_retried_on_every_render(self, configured_plugin):
        """Without a backoff, an offline box would run a speed test per tick."""
        attempts = []

        def failing_measure(_max_bytes):
            attempts.append(1)
            raise requests.ConnectionError("offline")

        with patch.object(NetworkSpeedPlugin, "measure", staticmethod(failing_measure)):
            for _ in range(10):
                configured_plugin.fetch_data()
                if configured_plugin._thread:
                    configured_plugin._thread.join(timeout=5)

        assert len(attempts) == 1, f"{len(attempts)} speed tests were started after a failure"
        result = configured_plugin.fetch_data()
        assert result.available is False
        assert "offline" in result.error

    def test_measurement_is_not_repeated_before_the_interval_elapses(self, configured_plugin):
        attempts = []

        def quick_measure(_max_bytes):
            attempts.append(1)
            return {"download_mbps": 1.0, "upload_mbps": 1.0, "ping_ms": 1.0, "last_tested": "x"}

        with patch.object(NetworkSpeedPlugin, "measure", staticmethod(quick_measure)):
            configured_plugin.fetch_data()
            configured_plugin._thread.join(timeout=5)
            for _ in range(10):
                configured_plugin.fetch_data()
            time.sleep(0.1)

        assert len(attempts) == 1

    def test_interval_is_floored_at_the_manifest_minimum(self, configured_plugin):
        configured_plugin.config = {"refresh_seconds": 5}
        assert configured_plugin._interval_seconds() == 1800

    def test_interval_falls_back_to_the_default_when_unset(self, configured_plugin):
        assert configured_plugin._interval_seconds() == 21600

    def test_interval_survives_a_junk_value(self, configured_plugin):
        configured_plugin.config = {"refresh_seconds": "soon"}
        assert configured_plugin._interval_seconds() == 21600



class TestRequestSizes:
    """Cloudflare refuses some byte counts, so only observed-good ones are asked for."""

    def test_only_known_good_sizes_are_requested(self):
        for cap in range(MIN_MAX_TRANSFER_MIB, MAX_MAX_TRANSFER_MIB + 1):
            for size in NetworkSpeedPlugin.request_sizes(cap * MIB):
                assert size in DOWNLOAD_SIZES

    def test_never_requests_a_size_cloudflare_refuses(self):
        """bytes=15000000 and bytes=100000000 are deterministic 403s."""
        assert 15000000 not in DOWNLOAD_SIZES
        assert 100000000 not in DOWNLOAD_SIZES

    def test_first_choice_covers_the_cap(self):
        assert NetworkSpeedPlugin.request_sizes(25 * MIB)[0] == 26214400
        assert NetworkSpeedPlugin.request_sizes(5 * MIB)[0] == 5242880

    def test_fallbacks_are_progressively_smaller(self):
        sizes = NetworkSpeedPlugin.request_sizes(25 * MIB)
        assert sizes[1:] == sorted(sizes[1:], reverse=True)
        assert all(s < sizes[0] for s in sizes[1:])

    def test_a_cap_above_every_known_size_uses_the_largest(self):
        assert NetworkSpeedPlugin.request_sizes(100 * MIB)[0] == max(DOWNLOAD_SIZES)

    def test_max_transfer_is_clamped_to_the_documented_range(self, configured_plugin):
        configured_plugin.config = {"max_transfer_mb": 5000}
        assert configured_plugin._max_transfer_bytes() == 100 * MIB
        configured_plugin.config = {"max_transfer_mb": 1}
        assert configured_plugin._max_transfer_bytes() == 5 * MIB
        configured_plugin.config = {}
        assert configured_plugin._max_transfer_bytes() == 25 * MIB

    def test_validate_config_rejects_an_out_of_range_transfer_size(self, plugin):
        assert plugin.validate_config({"max_transfer_mb": 500})
        assert plugin.validate_config({"max_transfer_mb": "big"})
        assert plugin.validate_config({"max_transfer_mb": 25}) == []
        assert plugin.validate_config({}) == []


class TestMeasure:
    """measure() against a fake link of known speed, so the arithmetic is checked."""

    @staticmethod
    def _session(download_mbps=100.0, upload_mbps=20.0, ping_seconds=0.02, refuse=()):
        """A fake Session backed by a clock that advances in proportion to bytes.

        A correct implementation must recover ``download_mbps`` /
        ``upload_mbps`` regardless of how much it chooses to transfer.
        """
        clock = {"t": 0.0}
        info = {"requested": [], "downloaded": 0, "uploaded": 0}

        def cost(size, mbps):
            clock["t"] += size * 8 / (mbps * 1_000_000)

        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.headers = {}

        def get(url, params=None, timeout=None, stream=False):
            size = int((params or {}).get("bytes", 0))
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False

            if size == 0:
                clock["t"] += ping_seconds
                response.raise_for_status.return_value = None
                response.iter_content.return_value = iter(())
                return response

            info["requested"].append(size)
            if size in refuse:
                response.raise_for_status.side_effect = requests.HTTPError("403 Client Error: Forbidden")
                response.iter_content.return_value = iter(())
                return response

            response.raise_for_status.return_value = None

            def chunks(chunk_size):
                remaining = size
                while remaining > 0:
                    take = min(chunk_size, remaining)
                    remaining -= take
                    cost(take, download_mbps)
                    info["downloaded"] += take
                    yield b"\0" * take

            response.iter_content.side_effect = chunks
            return response

        def post(url, data=None, timeout=None):
            for chunk in data:
                cost(len(chunk), upload_mbps)
                info["uploaded"] += len(chunk)
            response = MagicMock()
            response.raise_for_status.return_value = None
            return response

        session.get.side_effect = get
        session.post.side_effect = post
        return session, clock, info

    @staticmethod
    def _run(session, clock, cap_mib=25):
        with patch("plugins.network_speed.requests.Session", return_value=session), patch(
            "plugins.network_speed.time.monotonic", side_effect=lambda: clock["t"]
        ):
            return NetworkSpeedPlugin.measure(cap_mib * MIB)

    def test_measure_recovers_the_true_link_speed(self):
        session, clock, _ = self._session(download_mbps=100.0, upload_mbps=20.0)
        data = self._run(session, clock, cap_mib=100)

        assert data["download_mbps"] == pytest.approx(100.0, rel=0.05)
        assert data["upload_mbps"] == pytest.approx(20.0, rel=0.05)
        assert data["ping_ms"] == 20.0
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", data["last_tested"])

    def test_slow_start_is_excluded_from_the_result(self):
        """Regression guard for the warmup window.

        A fake link at a constant rate cannot distinguish an implementation
        that skips warmup from one that does not -- both report the same
        number. So make the first half-second of each transfer crawl. Only an
        implementation that discards that period reports the steady-state rate.

        SLOW_START is deliberately a local constant rather than WARMUP_SECONDS:
        reusing the production constant would make the fake move in lockstep
        with the mutation and the test would pass either way.
        """
        SLOW_START = 0.5
        session, clock, _ = self._session(download_mbps=100.0, upload_mbps=100.0)
        real_get = session.get.side_effect

        def slow_start_get(url, params=None, timeout=None, stream=False):
            response = real_get(url, params=params, timeout=timeout, stream=stream)
            if int((params or {}).get("bytes", 0)) == 0:
                return response
            inner = response.iter_content.side_effect

            def chunks(chunk_size):
                begin = clock["t"]
                for chunk in inner(chunk_size):
                    if clock["t"] - begin < SLOW_START:
                        clock["t"] += 0.15  # crawl while the window ramps up
                    yield chunk

            response.iter_content.side_effect = chunks
            return response

        session.get.side_effect = slow_start_get
        data = self._run(session, clock, cap_mib=100)
        assert data["download_mbps"] == pytest.approx(100.0, rel=0.05)

    def test_neither_direction_exceeds_the_configured_cap(self):
        """A fast link must stop at the cap, not keep pulling for the window.

        The cap is 6 MiB, which sits between the known-good request sizes, so
        the blob on the wire is 10 MiB and the cap has to do real work. Without
        it a 10 Gbps link would read the whole blob, and would upload for the
        full sample window -- gigabytes.
        """
        session, clock, info = self._session(download_mbps=10_000.0, upload_mbps=10_000.0)
        self._run(session, clock, cap_mib=6)

        cap = 6 * MIB
        assert info["requested"][0] > cap, "the blob must be larger than the cap for this to test anything"
        assert info["downloaded"] <= cap + CHUNK, f"downloaded {info['downloaded']} against a {cap} cap"
        assert info["uploaded"] <= cap + CHUNK, f"uploaded {info['uploaded']} against a {cap} cap"
    def test_a_slow_link_transfers_far_less_than_the_cap(self):
        """The sample window bounds time; the cap only bounds a fast link."""
        session, clock, info = self._session(download_mbps=2.0, upload_mbps=1.0)
        data = self._run(session, clock, cap_mib=100)

        assert data["download_mbps"] == pytest.approx(2.0, rel=0.1)
        assert info["downloaded"] < 5 * MIB, "a 2 Mbps link should not move 5 MiB in 2.5s"

    def test_a_refused_size_falls_back_to_a_smaller_one(self):
        """speed.cloudflare.com 403s some byte counts; that must not fail the test."""
        first = NetworkSpeedPlugin.request_sizes(25 * MIB)[0]
        session, clock, info = self._session(refuse=(first,))
        data = self._run(session, clock, cap_mib=25)

        assert info["requested"][0] == first
        assert len(info["requested"]) > 1, "no fallback was attempted"
        assert info["requested"][1] < first
        assert data["download_mbps"] > 0

    def test_every_size_refused_surfaces_the_error(self):
        session, clock, _ = self._session(refuse=tuple(DOWNLOAD_SIZES))
        with pytest.raises(requests.HTTPError):
            self._run(session, clock)

    def test_measure_sends_a_timeout_on_every_request(self):
        session, clock, _ = self._session()
        self._run(session, clock)

        for call in list(session.get.call_args_list) + list(session.post.call_args_list):
            assert call.kwargs.get("timeout"), f"request without a timeout: {call}"


class TestPlumbing:
    def test_plugin_id(self, plugin):
        assert plugin.plugin_id == "network_speed"

    def test_manifest_valid(self):
        for field in ("id", "name", "version"):
            assert field in MANIFEST

    def test_manifest_declares_live_data(self):
        """fetch_data paces itself and must be called every tick to do so."""
        assert MANIFEST.get("live_data") is True

    def test_reported_values_fit_their_declared_max_lengths(self, measured_plugin):
        result = measured_plugin.fetch_data()
        simple = MANIFEST["variables"]["simple"]
        for name, value in result.data.items():
            assert len(str(value)) <= simple[name]["max_length"], f"{name}={value!r} overflows its declared max_length"

    def test_cleanup_is_safe(self, configured_plugin):
        configured_plugin.cleanup()


class TestRuntimeDependencies:
    """FiestaBoard never installs a plugin's requirements.txt.

    A plugin that declares one is broken on every real install -- that is what
    Fiestaboard/FiestaBoard#1690 caught here.
    """

    def test_no_undeclarable_third_party_dependency(self):
        req = REPO_ROOT / "requirements.txt"
        if not req.exists():
            return

        import importlib.util

        overrides = {"pyyaml": "yaml", "speedtest-cli": "speedtest", "beautifulsoup4": "bs4"}
        for line in req.read_text().splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            dist = re.match(r"^[A-Za-z0-9._-]+", line).group(0)
            module = overrides.get(dist.lower(), dist.replace("-", "_"))
            assert importlib.util.find_spec(module) is not None, (
                f"requirements.txt declares {dist!r}, which FiestaBoard does not ship "
                f"and does not install. The plugin cannot work on a real install."
            )

    def test_source_does_not_import_speedtest(self):
        source = (REPO_ROOT / "plugins" / "network_speed" / "__init__.py").read_text()
        assert "import speedtest" not in source
