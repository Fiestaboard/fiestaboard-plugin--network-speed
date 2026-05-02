# Network Speed Setup Guide

Display your internet connection speed using a local Speedtest CLI check.

## Overview

The Network Speed plugin runs a Speedtest CLI speed test on the FiestaBoard host and displays the measured download speed, upload speed, and ping. Tests run in the background on a configurable interval. Requires the `speedtest-cli` Python package.

- API reference: https://github.com/sivel/speedtest-cli

### Prerequisites

Requires `speedtest-cli` (included in plugin `requirements.txt`).

## Quick Setup

1. **Enable** — Go to **Integrations** in your FiestaBoard settings and enable **Network Speed**.
2. **Configure** — Fill in the plugin settings (see Configuration Reference below).
3. **Template** — Add a page using the `network_speed` plugin variables:
   ```
   {{{ network_speed.status }}}
   ```
4. **View** — Navigate to your board page to see the live display.

## Template Variables

| Variable | Description | Example |
|---|---|---|
| `network_speed.download_mbps` | Download speed in Mbps | `250.4` |
| `network_speed.upload_mbps` | Upload speed in Mbps | `18.7` |
| `network_speed.ping_ms` | Latency in milliseconds | `14.3` |
| `network_speed.last_tested` | Timestamp of the last speed test | `2026-05-01 12:00` |

## Configuration Reference

| Setting | Name | Description | Default |
|---|---|---|---|
| `enabled` | Enabled |  | `False` |
| `refresh_seconds` | Refresh Interval (seconds) | How often to run a speed test. Tests take 20–60 seconds; minimum 1800s. | `3600` |

## Troubleshooting

- **speedtest-cli not installed** — rebuild the Docker container; `speedtest-cli` is in `requirements.txt`.
- **Test takes too long** — speed tests can take 20–60 seconds; this is normal.

