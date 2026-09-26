# Orbinum Watcher

Independent external monitoring for an Orbinum Testnet validator.

**Live dashboard:** https://orbinum-watcher.xyz  
**Validator:** `robotek8-orbinum`

Orbinum Watcher continuously checks validator telemetry from outside the validator host, stores historical samples, calculates uptime and incident statistics, exposes a public status dashboard, and sends private Telegram alerts when the validator goes offline or recovers.

It also includes a passive Windows-side telemetry pipeline that observes validator and host load, detects sustained anomalies, groups them into stress events, and synchronizes compact event summaries to the VPS for the public Incident history.

> Community-built monitoring tooling. This repository is not an official Orbinum project.

## What it monitors

External uptime monitoring:

- Validator reachability
- Connected peer count
- Best block height
- Finalized block height
- External metrics latency
- 24h / 7d / 30d / all-time uptime
- Incident start, recovery and duration

Passive validator-host telemetry:

- Validator container CPU and memory usage
- Host CPU and memory usage
- Peer drops and low-peer periods
- Finality gap and sync lag
- Best/finalized block progression stalls
- Metrics latency degradation
- Container restart/down signals
- Validator network and disk I/O deltas

The external collector currently samples every 60 seconds. Windows-side passive telemetry is collected independently at a shorter interval and does not control the validator.

## Incident history semantics

The dashboard groups the last 30 days of external samples into observed incidents:

- **Metrics unavailable**: fetching metrics failed. This can be a tunnel or network
  failure; it does not prove that the validator stopped.
- **No peers / Metrics incomplete / Degraded**: metrics were reachable but the
  recorded sample was unhealthy.
- **No observations**: more than 180 seconds passed without a sample. This is
  missing collector coverage, not measured validator downtime.

A stale incident ends at its last observation with recovery **unknown**. A later
sample resumes observations; only a healthy sample without a coverage gap marks
an incident recovered. A change of symptom creates a separate entry. Durations
are estimates between sample timestamps, clipped at the 30-day boundary; failures
and recoveries can occur between polls. The most recent ten external entries are merged with optional load events; the dashboard shows the twenty newest combined entries.

This changes incident presentation only. Existing uptime percentages remain
sample-based and are not the official Orbinum telemetry uptime. The database
schema, collector, Telegram notifications and validator settings are unchanged.
`/api/status` retains the original incident fields and adds `kind`, `ending`,
`started_ts`, `ended_ts` and `duration_seconds`. `ended_ts` is null for an ongoing
observation; `ending=unknown` never claims recovery.

Offline verification:

```bash
python -m unittest discover -s tests -p 'test_incident_history.py'
```

## Web source and deployment

`web.py` contains the production dashboard, including detailed load events and
incident observation gaps. See [web deployment and rollback](docs/WEB_DEPLOYMENT.md)
for required assets, read-only data mounts, validation and targeted updates.

## Architecture

```text
Orbinum validator host
        |
        | Prometheus metrics :9615
        v
private reverse SSH tunnel
        |
        v
VPS :19615 (loopback only)
        |
        +--> collector --> SQLite uptime history
        |                    |
        |                    +--> Telegram alert bot
        |                    |
        |                    +--> public web dashboard
        |                               |
        |                               v
        +---------------------------> Caddy / HTTPS
                                         |
                                         v
                              orbinum-watcher.xyz

Windows validator host
        |
        +--> passive telemetry agent
                 |
                 +--> local SQLite telemetry history
                 |
                 +--> anomaly detector
                 |
                 +--> grouped stress-event report
                 |
                 v
          isolated SFTP-only account
                 |
                 v
          VPS events.json snapshot
                 |
                 v
          dashboard Incident history
```

The public dashboard never talks directly to the validator. Prometheus metrics remain private and are transported through an SSH tunnel. Stress-event transport uses a separate SFTP-only account so the validator P2P tunnel does not need shell or file-transfer permissions.

The local telemetry database remains the source of truth for passive host observations. Only compact event summaries are synchronized to the VPS.

## Components

### `monitor.py`

One-shot collector intended to run from a systemd timer. It reads Orbinum Prometheus metrics and writes a timestamped sample to SQLite.

A sample contains:

```text
timestamp
status
peers
best block
finalized block
latency
error
```

### `bot.py`

Private Telegram bot for operational alerts and quick status checks.

The private bot exposes a persistent mobile button panel:

```text
[ 🟢 Status ]      [ 📊 Uptime ]
[ 🚨 Incidents ]   [ 🧭 Diagnostics ]
[       🌐 Dashboard       ]
```

The buttons provide the same read-only views as the legacy slash commands, which
remain available for compatibility. The Dashboard button returns a URL button
instead of exposing monitoring internals in Telegram.

It also sends automatic offline, degraded and recovery notifications.

### `agent/windows_telemetry.py`

Passive Windows telemetry collector. It reads Docker stats, Docker inspect state, validator Prometheus metrics and basic host CPU/RAM statistics into a local SQLite database.

It does not restart, stop or reconfigure the validator, Docker or SSH tunnels.

### `analysis/anomaly_detector.py`

Read-only anomaly detector for stored telemetry. It detects sustained CPU/RAM load, peer loss, finality/sync lag, block stalls, metrics degradation, container down state and restarts.

### `analysis/stress_event_report.py`

Groups nearby anomaly signals into higher-level stress events and calculates useful event context such as peak CPU, minimum peers, maximum finality/sync gap, block advancement, restarts, network I/O and disk I/O.

### `agent/windows_event_sync.py`

Builds compact stress-event snapshots from the local telemetry database and uploads them through the dedicated SFTP-only account using an atomic temporary-file rename.

The sync path is independent from the validator P2P reverse tunnel.

### `web.py`

Small read-only HTTP dashboard. It shows current validator state, uptime windows, a 24-hour availability timeline and Incident history.

The deployed Incident history merges externally observed downtime with passive stress/load events such as finality lag, peer drop, block stall and validator restart. Detailed stress metrics are expandable per event.

The production Incident history uses a compact five-column layout with event metrics rendered below the event row, avoiding horizontal scrolling on the normal dashboard width.

### `tests/synthetic_replay.py`

Synthetic replay scenarios for the anomaly detector. Healthy data is expected to produce no events, while injected CPU bursts, finality lag, sync lag, peer collapse, block stalls, telemetry failures and restarts must be detected.

### `tests/stress_event_report_replay.py`

Synthetic validation for grouping anomaly signals into higher-level stress events and recovery summaries.

### `Dockerfile`

Runs the public dashboard as an isolated container. The SQLite monitoring directory should be mounted read-only.

## Status model

Current public availability is derived from externally observed telemetry:

```text
ONLINE
  metrics reachable
  peers > 0
  required block metrics present

DEGRADED
  metrics reachable
  but required health conditions are not satisfied

OFFLINE
  metrics unavailable or monitoring data becomes stale
```

Passive stress-event analysis is complementary to uptime monitoring. A host-side load event does not automatically mean downtime; the event report records what changed and whether block/finality progression continued or recovered without a validator restart.

## Uptime semantics

The dashboard does not pretend that a new monitor already has 7 or 30 days of history.

- 24-hour uptime is shown together with the observed duration while the first day is still being collected.
- 7-day and 30-day windows show **Collecting data** until enough history exists.
- Missing historical periods are treated as unknown, not as downtime.

## Quick start

### Collector

```bash
python3 monitor.py
python3 monitor.py report
```

Default paths / endpoints in the reference deployment:

```text
Metrics: http://127.0.0.1:19615/metrics
Database: /var/lib/orbinum-monitor/uptime.db
```

### Passive Windows telemetry

```powershell
python C:\OrbinumWatcher\windows_telemetry.py --once --no-write
python C:\OrbinumWatcher\analysis\anomaly_detector.py --seconds 900
python C:\OrbinumWatcher\analysis\stress_event_report.py
```

Event synchronization:

```powershell
python C:\OrbinumWatcher\windows_event_sync.py --upload
```

Continuous synchronization can run separately from the telemetry collector:

```powershell
python C:\OrbinumWatcher\windows_event_sync.py --upload --loop --interval 30 --quiet
```

### Web

```bash
ORBINUM_DB=/var/lib/orbinum-monitor/uptime.db \
ORBINUM_WEB_HOST=127.0.0.1 \
ORBINUM_WEB_PORT=8787 \
python3 web.py
```

Health endpoint:

```bash
curl http://127.0.0.1:8787/health
```

Status endpoint:

```bash
curl http://127.0.0.1:8787/api/status
```

### Docker dashboard

```bash
docker build -t orbinum-watcher .

docker run --rm \
  -p 127.0.0.1:8787:8787 \
  -e ORBINUM_WEB_HOST=0.0.0.0 \
  -e ORBINUM_WEB_PORT=8787 \
  -e ORBINUM_DB=/data/uptime.db \
  -v /var/lib/orbinum-monitor:/data:ro \
  orbinum-watcher
```

### Telegram

Set secrets outside the repository:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_PAIR_CODE='...'
python3 bot.py
```

Do **not** commit the bot token or pairing code.

## Deployment notes

The live deployment uses:

- Orbinum validator on a separate host
- private reverse SSH tunnel for metrics
- Python collector + SQLite on a VPS
- systemd timer at 60-second cadence
- passive Windows telemetry with local SQLite history
- read-only anomaly/stress analysis
- isolated SFTP-only stress-event synchronization
- private Telegram bot
- Dockerized web dashboard
- Caddy reverse proxy with automatic HTTPS

The complete stress-event path has been validated end to end with a controlled synthetic `Finality lag` event: local event data reached the VPS snapshot, appeared through `/api/status`, rendered in public Incident history with expandable metrics, and was then removed by restoring the real event snapshot. The validator and its reverse tunnels were not restarted for this test.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## Security

- Prometheus metrics are not exposed publicly.
- The public dashboard only receives read-only access to monitoring history and synchronized event summaries.
- The SFTP event-sync account has no shell and no TCP forwarding.
- Validator P2P tunneling and stress-event transport use separate accounts.
- Telegram credentials live outside the repository.
- The monitoring stack does not submit transactions or modify validator state.
- Passive telemetry and analysis do not stop, restart or reconfigure the validator.

## License

No license has been selected yet. All rights remain with the repository owner until a license is added.


### Universal Telegram infrastructure panel

The home-side Telegram process can be replaced with the read-only
`agent/linux_ops_bot.py` panel. It keeps a single Telegram long-polling client
and summarizes the whole personal stack instead of controlling services.

Buttons:

```text
[ 🟢 Infrastructure ]
[ 🛰 Orbinum ] [ ⚡ Rialo ]
[ 🖥 VPS ]     [ 🌿 Canopy ]
[ 🚨 Alerts ]  [ 📊 Dashboard ]
```

The panel reads host CPU/RAM/disk/uptime locally, Orbinum RPC and the external
watcher API, Rialo systemd state plus the latest balance-guard journal entry,
VPS reachability, the Orbinum reverse P2P port, and Canopy port 9001.

It intentionally exposes no start/stop/restart actions.\n\nWhen `ORBINUM_VALIDATOR_ACCOUNT` is set, the Linux panel also reads the chain\'s `validatorSet.approvedValidators` and `session.validators` storage. It shows `CANDIDATE`, `APPROVED / WAITING`, or `ACTIVE VALIDATOR` in the Orbinum view and sends a one-time Telegram notification when the account is approved, enters the active set, or later leaves it. The last observed lifecycle state is persisted locally so service restarts do not repeat old notifications. This is read-only: no extrinsics are submitted.
