"""Display your internet connection speed, measured against Cloudflare's speed-test endpoints."""

from __future__ import annotations

import datetime
import logging
import statistics
import threading
import time
from typing import Any, Dict, List

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

USER_AGENT = "FiestaBoard Network Speed Plugin (https://github.com/Fiestaboard/fiestaboard-plugin--network-speed)"

DOWN_URL = "https://speed.cloudflare.com/__down"
UP_URL = "https://speed.cloudflare.com/__up"

MIB = 1 << 20
CHUNK = 64 * 1024

DOWNLOAD_SIZES = (1048576, 5242880, 10485760, 26214400, 52428800, 99999999)
"""Byte counts ``__down`` is known to serve, largest first at use.

Cloudflare's speed endpoint refuses some byte counts outright: ``bytes=15000000``
is a deterministic ``403`` with a one-byte body while ``bytes=26214400`` is
fine, on a fresh connection, with any User-Agent. The rule is undocumented and
not monotonic in size, so the plugin only ever asks for values it has been
observed to serve, and steps down this list if one is refused.
"""

WARMUP_SECONDS = 0.5
"""Ignored at the start of a transfer, so TCP slow start is not counted.

Measured against one link, a 1 MiB transfer reported 157 Mbps and a 25 MiB
transfer reported 803 Mbps. The short sample was not a noisy version of the
right answer; it was mostly slow start.
"""

SAMPLE_SECONDS = 2.0
"""Length of the measured window once warmup is over."""

REQUEST_TIMEOUT = 30
MEASUREMENT_BUDGET_SECONDS = 90
"""Hard ceiling on one measurement. It runs off the render path, but a wedged
connection must not leave a thread running forever."""

PING_SAMPLES = 5

DEFAULT_REFRESH_SECONDS = 21600
MIN_REFRESH_SECONDS = 1800
RETRY_AFTER_FAILURE_SECONDS = 300

DEFAULT_MAX_TRANSFER_MIB = 25
MIN_MAX_TRANSFER_MIB = 5
MAX_MAX_TRANSFER_MIB = 100


def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _rate_bps(sampled: int, sample_start: float | None, total: int, started: float, ended: float) -> float:
    """Bits per second, preferring the post-warmup window.

    A link fast enough to finish the whole transfer inside the warmup never
    opens a sample window; fall back to the transfer as a whole rather than
    reporting zero.
    """
    if sample_start is not None and sampled > 0 and ended > sample_start:
        return sampled * 8 / (ended - sample_start)
    if ended > started:
        return total * 8 / (ended - started)
    return 0.0


class NetworkSpeedPlugin(PluginBase):
    """Network Speed plugin for FiestaBoard.

    A speed test takes tens of seconds. ``fetch_data`` is called on the render
    path, and :meth:`~src.plugins.registry.PluginRegistry.build_template_context`
    gives every plugin 15 seconds, so measuring inline cannot work -- the
    result would miss the deadline on every refresh. The measurement therefore
    runs on a background thread and ``fetch_data`` only ever reports what has
    already finished, which is what this plugin's docs always claimed it did.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: Dict[str, Any] | None = None
        self._error: str | None = None
        self._last_attempt: float | None = None

    # ── settings ────────────────────────────────────────────────────────────

    def _interval_seconds(self) -> float:
        """How long to wait between measurements.

        The manifest sets ``live_data``, so PluginBase does not cache for us --
        this plugin paces itself instead, because it must keep serving the
        previous result while the next measurement is still running.
        """
        raw = (self.config or {}).get("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            seconds = DEFAULT_REFRESH_SECONDS
        return max(seconds, MIN_REFRESH_SECONDS)

    def _max_transfer_bytes(self) -> int:
        raw = (self.config or {}).get("max_transfer_mb", DEFAULT_MAX_TRANSFER_MIB)
        try:
            mib = int(raw)
        except (TypeError, ValueError):
            mib = DEFAULT_MAX_TRANSFER_MIB
        return max(MIN_MAX_TRANSFER_MIB, min(mib, MAX_MAX_TRANSFER_MIB)) * MIB

    # ── the render path ─────────────────────────────────────────────────────

    def fetch_data(self) -> PluginResult:
        """Report the most recent completed measurement. Never blocks."""
        self._maybe_start_measurement()

        with self._lock:
            result = dict(self._result) if self._result else None
            error = self._error
            running = self._thread is not None and self._thread.is_alive()

        if result:
            return PluginResult(available=True, data=result)
        if running:
            return PluginResult(available=False, error="Speed test in progress; results appear when it finishes.")
        return PluginResult(available=False, error=error or "No speed test has completed yet.")

    def _maybe_start_measurement(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            if self._last_attempt is not None:
                due_after = self._interval_seconds() if self._result else RETRY_AFTER_FAILURE_SECONDS
                if time.monotonic() - self._last_attempt < due_after:
                    return

            self._last_attempt = time.monotonic()
            self._thread = threading.Thread(
                target=self._measure_and_store,
                name="network-speed-test",
                daemon=True,
            )
            self._thread.start()

    # ── the background thread ───────────────────────────────────────────────

    def _measure_and_store(self) -> None:
        try:
            data = self.measure(self._max_transfer_bytes())
        except requests.RequestException as e:
            logger.warning("Speed test failed: %s", e)
            with self._lock:
                self._error = f"Speed test failed: {e}"
            return
        except Exception as e:
            logger.exception("Speed test raised")
            with self._lock:
                self._error = f"Speed test failed: {e}"
            return

        with self._lock:
            self._result = data
            self._error = None

    @staticmethod
    def measure(max_transfer_bytes: int) -> Dict[str, Any]:
        """Run one speed test. Returns the plugin's four template variables.

        Uses only ``requests``, which FiestaBoard ships. ``speedtest-cli``,
        which this plugin used to require, is installed on no FiestaBoard
        instance and never will be -- the platform does not install plugin
        dependencies.
        """
        deadline = time.monotonic() + MEASUREMENT_BUDGET_SECONDS

        with requests.Session() as session:
            session.headers["User-Agent"] = USER_AGENT

            # Warm the connection so TLS setup is charged neither to the
            # latency samples nor to the first transfer.
            session.get(DOWN_URL, params={"bytes": 0}, timeout=REQUEST_TIMEOUT).raise_for_status()

            ping_ms = NetworkSpeedPlugin._measure_latency(session)
            download_bps = NetworkSpeedPlugin._measure_download(session, max_transfer_bytes, deadline)
            upload_bps = NetworkSpeedPlugin._measure_upload(session, max_transfer_bytes, deadline)

        return {
            "download_mbps": round(download_bps / 1_000_000, 1),
            "upload_mbps": round(upload_bps / 1_000_000, 1),
            "ping_ms": round(ping_ms, 1),
            "last_tested": _now_stamp(),
        }

    @staticmethod
    def _measure_latency(session: requests.Session) -> float:
        """Median HTTP round-trip time over a warm connection, in milliseconds."""
        samples = []
        for _ in range(PING_SAMPLES):
            started = time.monotonic()
            session.get(DOWN_URL, params={"bytes": 0}, timeout=REQUEST_TIMEOUT).raise_for_status()
            samples.append((time.monotonic() - started) * 1000)
        return statistics.median(samples)

    @staticmethod
    def request_sizes(max_transfer_bytes: int) -> List[int]:
        """Sizes to ask ``__down`` for, best first.

        The smallest known-good size that still covers the cap, then smaller
        ones as fallbacks in case Cloudflare refuses the first choice.
        """
        big_enough = [s for s in DOWNLOAD_SIZES if s >= max_transfer_bytes]
        preferred = min(big_enough) if big_enough else max(DOWNLOAD_SIZES)
        smaller = sorted((s for s in DOWNLOAD_SIZES if s < preferred), reverse=True)
        return [preferred, *smaller]

    @staticmethod
    def _measure_download(session: requests.Session, max_transfer_bytes: int, deadline: float) -> float:
        last_error: Exception | None = None
        for size in NetworkSpeedPlugin.request_sizes(max_transfer_bytes):
            try:
                return NetworkSpeedPlugin._time_download(session, size, max_transfer_bytes, deadline)
            except requests.HTTPError as e:
                logger.info("Speed test size %d refused (%s); trying a smaller one", size, e)
                last_error = e
        raise last_error if last_error else requests.RequestException("no usable download size")

    @staticmethod
    def _time_download(session: requests.Session, size: int, max_transfer_bytes: int, deadline: float) -> float:
        """Read at most *max_transfer_bytes*, timing only the post-warmup window.

        The request asks for a whole blob but the read stops as soon as there
        is enough to measure, so the interval setting bounds time and the cap
        bounds bytes -- neither depends on guessing a transfer size.
        """
        started = time.monotonic()
        sample_start: float | None = None
        sampled = 0
        total = 0

        with session.get(DOWN_URL, params={"bytes": size}, timeout=REQUEST_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(CHUNK):
                now = time.monotonic()
                total += len(chunk)
                if sample_start is None:
                    if now - started >= WARMUP_SECONDS:
                        sample_start = now
                else:
                    sampled += len(chunk)
                    if now - sample_start >= SAMPLE_SECONDS:
                        break
                if total >= max_transfer_bytes or now >= deadline:
                    break

        return _rate_bps(sampled, sample_start, total, started, time.monotonic())

    @staticmethod
    def _measure_upload(session: requests.Session, max_transfer_bytes: int, deadline: float) -> float:
        """Stream a payload, timing the post-warmup window.

        ``__up`` takes no size in the URL, so the 403 problem does not arise
        here. The body is a generator so a 100 MiB test does not cost 100 MiB
        of RAM, and it stops as soon as the sample window closes.
        """
        block = b"\0" * CHUNK
        started = time.monotonic()
        state: Dict[str, Any] = {"sent": 0, "sampled": 0, "sample_start": None}

        def body():
            while True:
                now = time.monotonic()
                if state["sent"] >= max_transfer_bytes or now >= deadline:
                    return
                if state["sample_start"] is None:
                    if now - started >= WARMUP_SECONDS:
                        state["sample_start"] = now
                else:
                    if now - state["sample_start"] >= SAMPLE_SECONDS:
                        return
                    state["sampled"] += len(block)
                state["sent"] += len(block)
                yield block

        session.post(UP_URL, data=body(), timeout=REQUEST_TIMEOUT).raise_for_status()

        return _rate_bps(state["sampled"], state["sample_start"], state["sent"], started, time.monotonic())

    # ── plumbing ────────────────────────────────────────────────────────────

    @property
    def plugin_id(self) -> str:
        return "network_speed"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        raw = config.get("max_transfer_mb")
        if raw is not None:
            try:
                mib = int(raw)
            except (TypeError, ValueError):
                errors.append("max_transfer_mb must be a whole number of MB")
            else:
                if not MIN_MAX_TRANSFER_MIB <= mib <= MAX_MAX_TRANSFER_MIB:
                    errors.append(
                        f"max_transfer_mb must be between {MIN_MAX_TRANSFER_MIB} and {MAX_MAX_TRANSFER_MIB}"
                    )
        return errors

    def cleanup(self) -> None:
        """The measurement thread is a daemon and holds nothing to release."""
        with self._lock:
            self._thread = None
