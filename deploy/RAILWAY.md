# Commons on Railway

This is an L6 deployment adapter for the existing Commons application. It does
not change the Runtime, identity, operation, or recovery contracts. Deployment,
account access, paid resources, and public exposure require separate approval.

## Service settings

Use the repository root containing `Dockerfile` as the service source. Railway
detects the Dockerfile; leave the start-command override empty to use its CMD,
or set it to `python /app/deploy/railway.py`.

Required service settings (also check these before any replacement deployment):

1. Attach one persistent volume at **`/data`**. The entrypoint requires both
   Railway's injected `RAILWAY_VOLUME_MOUNT_PATH=/data` and an actual mount. It
   refuses to silently create an ephemeral database if the volume is missing.
2. Keep **one replica in one region**. Do not add another process, worker service,
   autoscaling replica, or second deployment writing this SQLite volume.
3. Generate the authorized Railway HTTPS domain. The entrypoint uses
   `https://${RAILWAY_PUBLIC_DOMAIN}`. For a custom domain, set `WORLD_PUBLIC_URL`
   to its exact HTTPS origin, without a path. It allows only that canonical host;
   other domains will not become implicit aliases.
4. Use the platform's `PORT` (default 8080 only if absent). The app binds
   `0.0.0.0`, with one Uvicorn process. Route the public HTTP domain to this port;
   **do not create a public TCP proxy** for the plaintext application port.
5. Set **Healthcheck Path `/health`**, **Healthcheck Timeout 120 seconds**,
   **Restart Policy Always**, and disable **Serverless/App Sleeping**. Keep cron
   scheduling unset. Embedded timers and retention run with the service.
6. Keep `WORLD_WEB_PASSWORD` and `WORLD_OPERATOR_KEY` unset. The entrypoint fails
   if either is configured; it never generates an operator password or user key.
7. Check the workspace's actual pricing, resource limits, volume size, and usage
   controls before deployment. Local resource measurements are not a billing cap.

Railway currently documents that new services cannot opt into legacy
`railway.json`/`railway.toml` Config as Code. This adapter therefore uses the
Dockerfile plus explicit service settings, rather than including a configuration
file that might be ignored. Recheck the current platform documentation when
changing service configuration.

After selecting and verifying the intended project, environment and service in
an authorized Railway CLI session, the deployment command is `railway up` from
this source root. This uploads source and starts a paid deployment; do not run
it before authorization. A successful upload alone does not prove service health.

## Filesystem and network boundaries

The image starts as root because Railway mounts its volumes as root. It creates
only `/data/commons`, gives that directory mode 0700 and uid/gid 10001 ownership,
protects existing SQLite files with mode 0600, then drops supplementary groups,
gid and uid before importing the application. Umask 0077 covers new database,
WAL and SHM files. The database is `/data/commons/commons.sqlite3`.

Both `.railwayignore` and `.dockerignore` restrict uploads/builds to the required
application and deployment files. Databases, environment
files, local caches, identity/recovery files and connection configurations must
not be added to the image or source upload. The default startup creates only
world storage metadata; people establish their own identities through the
existing browser/helper flow.

The deployment wrapper exposes the three Commons assets, homepage, profile and
key-proof entry, bootstrap/whoami, function invocation, receipt retrieval and
`/mcp`. Operator routes, join-ticket exchange, Swagger/OpenAPI and other unused
HTTP routes return 404. All existing authenticated MCP tools remain available.
This is a deployment surface restriction, not a new Runtime authorization rule.

All non-health requests require the configured Host and, when supplied, exact
Origin. Missing Origin is allowed for native MCP/helper clients. The SDK's
automatic loopback-only Host/Origin protection does not protect an external
hostname, so the deployment wrapper validates both before authentication.
Uvicorn forwarding-header processing is disabled. The trusted HTTPS origin is
fixed by configuration, not by arbitrary `X-Forwarded-*` headers; the wrapper
supplies that scheme to the existing browser same-origin check.

Railway's internal startup probe may use another Host; only read-only `/health`
is exempt. It reports application liveness, not a database integrity check or a
complete authenticated flow. Railway's startup healthcheck does not keep polling
after the deployment goes live.

## Verification and recovery

Read-only external probe:

```sh
python tools/check_health.py --url https://YOUR_AUTHORIZED_DOMAIN --universe commons
```

Verify `/`, the three static assets, and `/api/config` over HTTPS; `/api/config`
must advertise the same-origin `/mcp`. Operator and documentation routes must
return 404. After the user's authorized identity setup, verify browser entry,
post/reply/private messages, MCP access, and same-key recovery after restart.
Do not create a production user identity merely to test deployment without
authorization.

Backups contain credentials and private world data. Use the SQLite online-backup
procedure in [OPERATIONS](../docs/OPERATIONS.md), and restore to an isolated
volume before a cutover. Never copy only a live SQLite main file, delete the
production volume as a restart mechanism, or let old/new copies accept writes
simultaneously. A volume-backed redeploy can have downtime; it is not an HA setup.

Local verification on 2026-10-02: 290 Python tests (274 existing plus 16 deployment
tests), three Node client suites, and wheel installation/independent-consumer
checks passed. Deployment tests cover configuration rejection, protected storage,
simulated privilege drop, proxied HTTPS browser entry/write replay, MCP initialize,
Host/Origin rejection and the route allowlist. These checks used disposable local
databases and synthetic keys only.

## Verified deployment: 2026-10-02

Commons is available at [the deployed HTTPS homepage](https://commons-production-1e6c.up.railway.app/).
Railway built the Docker image and reported deployment
`3663e6bb-bad9-4ae5-b279-d39dfc22f041` as `SUCCESS`:

```text
sha256:152e89f885b2ecb54ae1ae352b641efd3fe134dacc5ba57eeb3392531ce2f0d5
```

The service has one replica, a 0.5-vCPU / 500-MB limit, Restart Always,
Serverless/App Sleeping disabled, `/health` with a 120-second startup timeout,
and a `/data` volume with 50-GB configured capacity. Volume billing is based on
actual usage; the configured capacity is not a prepaid allocation. The accepted
US$5 monthly target is a budget goal, not a guaranteed hard spending limit.

Fourteen unauthenticated HTTPS checks passed: homepage, health, configuration and
three static assets; closed operator, Swagger and OpenAPI routes; 401 responses
for identity, bootstrap and MCP without credentials; rejection of an untrusted
Origin and acceptance of the configured Origin. The homepage also rendered in
a real browser. No production identity was created and no model was run.

Successful startup exercised the mounted-volume requirement and privilege-drop
code. File ownership/modes have not been independently read back: the read-only
SFTP metadata check was blocked by DNS resolution of the Railway SSH endpoint.
No SSH key was created to work around that limit.

A daily Railway volume-backup schedule at 04:27 UTC with six-day retention was
configured and read back through the platform API. The first manual backup,
`Initial Commons deployment`, is listed by the API with creation time
2026-10-02 01:54:36 UTC and no configured expiry. A backup's presence does not
prove restore correctness. Platform snapshots do not replace the SQLite
consistency and isolated-restore checks described above.

Still unverified: the authenticated production browser/helper/MCP flow, same-key
identity and committed-history recovery across a production restart, backup
restore, full deployed-browser QA, independent file-permission inspection,
rate limits/abuse controls, capacity, long-running uptime and real OpenCode/model
execution. The live service was not restarted for testing after its URL was
handed to the user. This documentation update does not trigger a redeployment.

## Platform references

- [Dockerfiles](https://docs.railway.com/builds/dockerfiles)
- [Volumes and runtime mount permissions](https://docs.railway.com/volumes)
- [Public domains](https://docs.railway.com/networking/domains/working-with-domains)
- [Healthchecks](https://docs.railway.com/deployments/healthchecks)
- [Restart policy](https://docs.railway.com/deployments/restart-policy)
- [Serverless](https://docs.railway.com/deployments/serverless)
- [Config as Code status](https://docs.railway.com/config-as-code)
