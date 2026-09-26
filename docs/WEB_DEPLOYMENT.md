# Web dashboard deployment and rollback

`web.py` is the production dashboard source. It was synchronized from the running
VPS on 2026-09-26 after the incident-history fix. Make future changes in GitHub
before deploying; do not maintain a separate edited server version.

## Inputs that are not code

- Existing SQLite samples: `/var/lib/orbinum-monitor/uptime.db`.
- Optional passive load events: `/var/lib/orbinum-monitor/events.json`.
- Existing branding assets: `/opt/orbinum-monitor/static` (icons, favicon, OG image).

The database and static directory must be mounted read-only at `/data` and
`/app/static`. The example Compose file includes both mounts. Assets are not
bundled in this repository: preserve the existing directory on upgrades. The
server reads events from `/data/events.json` by default; missing or invalid JSON
is treated as no optional events. `/health` only checks the web process; verify
`/api/status` too. Never copy database files, credentials, `.env`, or validator
keys into GitHub or a Docker build context.

## Validate a candidate

From an isolated checkout of the reviewed commit:

```sh
python3 -m py_compile web.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

The tests create temporary data and bind only to loopback. No production node,
Docker socket, credentials or network service is required.

## Existing VPS: targeted update

Run as the authorized administrator. Substitute the reviewed checkout and a
unique deployment ID. Verify that the Compose service uses `node-orbinum-web` as
its image before continuing. These commands affect only `orbinum-web`.

```sh
set -eu
checkout=/path/to/reviewed/orbinum-watcher
deploy_id=YYYYMMDD-HHMMSS
backup=/root/orbinum-web-backup-$deploy_id
mkdir -m 700 "$backup"
cp -p /opt/orbinum-monitor/web.py "$backup/web.py"
docker inspect --format '{{.Image}}' orbinum-web > "$backup/image-id"
docker image tag "$(cat "$backup/image-id")" "node-orbinum-web:rollback-$deploy_id"
```

Build in an isolated context containing only the source. Use the running image
as the base for a code-only update, keeping its Python/runtime dependencies:

```sh
mkdir "$backup/build"
cp "$checkout/web.py" "$backup/build/web.py"
printf 'FROM node-orbinum-web:rollback-%s\nCOPY web.py /app/web.py\n' "$deploy_id" > "$backup/build/Dockerfile"
docker build -t "node-orbinum-web:candidate-$deploy_id" "$backup/build"
```

Before deployment, run a candidate smoke check with the existing database mounted
**read-only**. This starts no server and does not contact the validator:

```sh
docker run --rm --network none \
  -v /var/lib/orbinum-monitor:/data:ro \
  -e ORBINUM_DB=/data/uptime.db \
  "node-orbinum-web:candidate-$deploy_id" \
  python3 -c 'import web; d=web.snapshot(); print(d["ui_state"], len(d["incidents"]))'
```

Then switch only the web service:

```sh
docker tag "node-orbinum-web:candidate-$deploy_id" node-orbinum-web
cp "$checkout/web.py" /opt/orbinum-monitor/web.py
docker compose -f /root/node/docker-compose.yml \
  -f /root/node/docker-compose.monitoring.yml \
  up -d --no-deps --no-build orbinum-web
curl --fail --show-error --silent https://orbinum-watcher.xyz/health
curl --fail --show-error --silent https://orbinum-watcher.xyz/api/status
```

Verify sample age, the displayed state, incident types and the browser layout.
Compare with the pre-deployment state: an existing node outage is not a web
regression. Confirm that other containers retained their start times. Do not run
`compose down`, recreate the validator, or restart the collector for a web update.

## Rollback

Use the same deployment ID and backup directory:

```sh
set -eu
docker tag "node-orbinum-web:rollback-$deploy_id" node-orbinum-web
cp "$backup/web.py" /opt/orbinum-monitor/web.py
docker compose -f /root/node/docker-compose.yml \
  -f /root/node/docker-compose.monitoring.yml \
  up -d --no-deps --no-build orbinum-web
curl --fail --show-error --silent https://orbinum-watcher.xyz/api/status
```

Neither update nor rollback migrates the database. Keep the rollback image and
source until the deployment has been verified. Runtime/base-image upgrades are a
separate change and should use the repository Dockerfile with their own testing.
