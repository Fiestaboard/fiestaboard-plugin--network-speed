# Network Speed Setup Guide

Display your internet connection speed, measured against Cloudflare's speed-test endpoints.

## Overview

The Network Speed plugin measures download speed, upload speed, and latency from the FiestaBoard host by transferring data against `speed.cloudflare.com`. Tests run on a background thread on a configurable interval; the board always shows the last completed result and never waits for a test to finish.

- Measurement endpoint: https://speed.cloudflare.com/

### Prerequisites

None. No API key, no account, and no extra packages to install — the plugin uses only `requests`, which FiestaBoard already ships. It does need outbound HTTPS access to `speed.cloudflare.com`.

## Quick Setup

1. **Enable** — Go to **Integrations** in your FiestaBoard settings and enable **Network Speed**. The first test starts immediately and takes a few seconds.
2. **Configure** — Optionally adjust the test interval and max transfer size (see Configuration Reference below). The defaults are fine for an unmetered connection.
3. **Template** — Add a page using the `network_speed` plugin variables:
   ```
   NETWORK SPEED
   Down: {{network_speed.download_mbps}} Mbps
   Up:   {{network_speed.upload_mbps}} Mbps
   Ping: {{network_speed.ping_ms}} ms
   ```
4. **View** — Navigate to your board page to see the live display.

## Template Variables

| Variable | Description | Example |
|---|---|---|
| `network_speed.download_mbps` | Download speed in Mbps | `250.4` |
| `network_speed.upload_mbps` | Upload speed in Mbps | `18.7` |
| `network_speed.ping_ms` | HTTP round-trip latency in milliseconds | `14.3` |
| `network_speed.last_tested` | When the displayed measurement was taken | `2026-05-01 12:00` |

## Configuration Reference

| Setting | Name | Description | Default |
|---|---|---|---|
| `enabled` | Enabled |  | `False` |
| `refresh_seconds` | Test Interval (seconds) | How often to run a speed test. Minimum 1800. | `21600` |
| `max_transfer_mb` | Max Transfer Size (MB) | Largest transfer per direction, 5–100. | `25` |

At the defaults a test moves up to 50 MB and runs four times a day. On a metered connection, lower `max_transfer_mb` and raise `refresh_seconds`.

## Troubleshooting

- **"Speed test in progress"** — normal for the first few seconds after enabling. The variables appear once the first test completes.
- **"No speed test has completed yet"** — the first test failed. Check the Integrations page for the underlying error; the plugin retries after 5 minutes rather than on every render.
- **`last_tested` looks old** — that is the point of the field: it shows when the displayed numbers were measured. At the default interval it will be up to 6 hours old. Lower `refresh_seconds` if you want fresher numbers, at the cost of more data.
- **The number looks low on a fast connection** — raise `max_transfer_mb`. A short transfer measures TCP slow start rather than your link; the plugin discards the first half-second for this reason, but on a very fast link a 25 MB cap can still cut the measurement short.
- **Results differ from speedtest.net** — they are different tests against different servers. Cloudflare's endpoint is anycast and usually closer to you, so download figures often read higher.
