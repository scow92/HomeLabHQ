# Configuration

For OPNsense WireGuard endpoint discovery, health monitoring and assisted
replacement using NordVPN candidates, see [VPN Endpoints](vpn-endpoints.md).
It is opt-in per OPNsense device.

Compute and its optional Ansible controller are configured entirely in the web
interface. See [Compute and Ansible maintenance](compute.md) for controller,
inventory, approved-playbook, mapping, reboot, and Docker strategy setup.

HomelabHQ reads configuration from environment variables at process startup.
The supplied Compose file contains the recommended production defaults.

## Server and storage

| Variable | Default | Description |
|---|---:|---|
| `HLHQ_PORT` | `8770` | Main HTTP or HTTPS listen port. |
| `HLHQ_ICON_HTTP_PORT` | `8771` | Plain-HTTP companion used only to serve Home Screen icons with the generated self-signed certificate. Set to `0` to disable it and remove the matching published port. |
| `HLHQ_DATA_DIR` | `/data` | Main document, history, locks, backups, and `secrets/` directory. |
| `HLHQ_WEB_DIR` | `../web` | Static web application directory. The image sets this to `/app/web`. |
| `HLHQ_MAX_JSON_BODY_BYTES` | `1048576` | Maximum accepted JSON request-body size in bytes. |
| `HLHQ_HTTP_REQUEST_TIMEOUT` | `30` | Idle socket timeout in seconds for accepted HTTP connections. Values below `1` are raised to `1`. |
| `HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS` | off | Allows a non-container local process to open a store containing device credentials. Intended only for deliberate development recovery. |

## TLS and proxies

| Variable | Default | Description |
|---|---:|---|
| `HLHQ_TLS` | off | `auto`, `1`, `true`, or `yes` enables HTTPS and generates a self-signed certificate if trusted certificate paths are absent. |
| `HLHQ_TLS_HOSTS` | — | Comma-separated DNS names and IP addresses added to a generated certificate's Subject Alternative Name list. |
| `HLHQ_TLS_CERT` | — | Path to a supplied certificate. Must be set with `HLHQ_TLS_KEY`. |
| `HLHQ_TLS_KEY` | — | Path to the supplied certificate's private key. Must be set with `HLHQ_TLS_CERT`. |
| `HLHQ_EXTERNAL_HTTPS` | off | Marks session cookies `Secure` when a reverse proxy always provides the externally visible HTTPS connection. |
| `HLHQ_TRUST_PROXY` | off | Honors `X-Real-IP` and `X-Forwarded-Proto`. Enable only when a trusted reverse proxy removes client-supplied forwarding headers and sets its own. |

The Compose deployment sets `HLHQ_TLS=auto` and reads `HLHQ_TLS_HOSTS` from the
Git-ignored `.env` file. To use extra certificate names, copy the example and
set every LAN name or IP address used to reach the service before its first
start:

```bash
cp .env.example .env
# Edit .env, for example: HLHQ_TLS_HOSTS=192.168.1.10,homelabhq.lan
```

If the generated certificate already exists, back up the complete data
directory, stop HomelabHQ, and remove only `secrets/tls_cert.pem` and
`secrets/tls_key.pem` before restarting it.

To use a trusted certificate, mount it read-only and set the certificate paths,
or place `nm.crt` and `nm.key` in the optional `/certs` mount. The included
helper creates a locally trusted mkcert pair:

```bash
./scripts/setup-mkcert.sh 192.168.1.10 homelabhq.lan
```

When a reverse proxy terminates TLS and forwards plain HTTP to HomelabHQ, set
`HLHQ_EXTERNAL_HTTPS=1`. Alternatively, set `HLHQ_TRUST_PROXY=1` if the proxy
removes client-supplied forwarding headers and sets `X-Forwarded-Proto: https`.
Either setting keeps the browser session cookie restricted to HTTPS.

## Polling and client discovery

| Variable | Default | Description |
|---|---:|---|
| `HLHQ_POLL_INTERVAL` | `60` | Seconds between background device poll cycles. |
| `HLHQ_POLL_TIMEOUT` | `10` | Timeout in seconds for one device poll. |
| `HLHQ_OFFLINE_AFTER` | `5` | Consecutive failed polls required before an offline transition is notified. Recovery is immediate after a successful poll. |
| `HLHQ_CLIENT_SCAN_INTERVAL` | `300` | Minimum seconds between background Access-roster refreshes. Values below `60` are raised to `60`. |
| `HLHQ_CLIENT_OFFLINE_AFTER` | `600` | Seconds without observation before an Access-roster client is considered offline. Values below `60` are raised to `60`. |
| `HLHQ_CLIENT_RECORD_RETENTION_DAYS` | `180` | Days to retain unseen offline Access-roster records. `0` retains them indefinitely. |

`HLHQ_OFFLINE_AFTER` is a count of poll failures, not a duration. With default
settings, notification occurs after approximately five poll intervals.

## Retention and safety limits

| Variable | Default | Description |
|---|---:|---|
| `HLHQ_MAX_SESSIONS` | `10000` | Maximum retained active sessions. Expired and then oldest sessions are pruned. |
| `HLHQ_MAX_AUTH_FAILURE_KEYS` | `10000` | Maximum client-address entries retained by the in-memory login throttle. Values below `100` are raised to `100`. |
| `HLHQ_MAX_PUSH_SUBSCRIPTIONS_PER_USER` | `20` | Maximum retained web-push subscriptions per user. |
| `HLHQ_MAX_SSH_HOST_KEYS` | `1024` | Maximum remembered SSH trust-on-first-use host-key records. |
| `HLHQ_MAX_COMPUTE_JOBS` | `500` | Maximum persisted Compute maintenance jobs. Completed jobs are pruned oldest-first; active jobs are retained. Values below `10` are raised to `10`. |
| `HLHQ_MAX_ANSIBLE_OUTPUT_BYTES` | `200000` | Maximum sanitized stdout and stderr characters retained per Compute maintenance job, preserving both the beginning and PLAY RECAP tail. Values below `10000` are raised to `10000`. |
| `HLHQ_MAX_ANSIBLE_INVENTORY_BYTES` | `5000000` | Maximum sanitized `ansible-inventory --list` characters accepted for parsing. It is never lower than the job-output limit. |
| `HLHQ_VAPID_SUB` | `mailto:admin@example.com` | VAPID subject used for web push. Use an address on a domain you control; reserved names such as `.local` can be rejected by push providers. |

## Local development

Create the environment and install locked runtime dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt
HLHQ_DATA_DIR=./data HLHQ_TLS=auto python3 backend/app.py
```

Open <https://localhost:8770>. Omit `HLHQ_TLS=auto` for plain HTTP.

Use local mode only with empty or test data. It runs under your normal account,
so any other process running as that account can read the same files. HomelabHQ
refuses to start locally when the selected data directory already contains
device credentials unless `HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS=1` is explicitly
set.
