# NordVPN WireGuard Endpoint Manager

The OPNsense device-detail **VPN Endpoints** section helps an owner manage one
existing NordVPN WireGuard peer. It discovers United Kingdom NordVPN WireGuard
recommendations, performs an RDAP registration lookup for each endpoint, and
prioritises candidates whose registered owner matches the profile defaults
(`PacketHub` / `Packethub`). This is intended for installations where an exit
provider matters to a service such as Ring.

It is based on the endpoint/public-key retrieval approach described by the
[SYSADMIN102 OPNsense NordVPN guide](https://sysadmin102.com/2025/01/opnsense-wireguard-nordvpn-setup/),
but HomeLabHQ implements its own bounded HTTP client and does not retrieve the
NordLynx private key. Existing OPNsense private keys are never read, rotated,
returned to the browser, or logged.

## Configure

1. Configure the NordVPN WireGuard peer and instance in OPNsense first.
2. Open the OPNsense device, select **VPN Endpoints**, then **Configure**.
3. Select the peer, optionally record the associated instance and gateway UUID,
   and enable the profile. The defaults are United Kingdom, London, 20
   candidates, five-minute handshake warning, and hourly discovery.
4. Use **Refresh candidates** to inspect the current discovery set. Preferred,
   rejected, and unknown ownership are visible separately. Use Ring result to
   record a manually tested `Verified`, `Failed`, `Assumed from provider`, or
   `Unknown` result and an optional operator note through the API.

Owner patterns are case-insensitive, whole-word normalised matches: `Hydra`
matches “Hydra Communications”, but not “NotHydra”. PacketHub ownership is a
preference only. It is neither a permanent network-ownership guarantee nor
proof that Ring accepts a particular exit IP; HomeLabHQ never stores Ring
credentials or calls private Ring APIs.

## Monitoring and alerts

The poller reads OPNsense’s live WireGuard status and treats a recent
authenticated handshake as the tunnel-health signal. Gateway/dpinger status is
shown only as supporting information. It alerts the device owner after two
successive observations of a stale handshake, an endpoint missing from fresh
discovery, an active endpoint becoming rejected/unknown, or no preferred
replacement. This debounce prevents a single failed discovery request from
spamming notifications.

Discovery contacts only `https://api.nordvpn.com` and address ownership only
`https://rdap.org`. Both integrations have fixed destinations, TLS, explicit
connect/read timeouts, no redirect following, bounded response sizes, and no
user-provided outbound URL. RDAP failures produce an `Unknown` candidate; they
do not stop NordVPN discovery. The NordVPN API response and RDAP response are
untrusted input and are strictly parsed. No payload dumps are logged.

HomeLabHQ retains bounded, owner-scoped candidate history (100 entries per
device): endpoint/hostname, public-key fingerprint, owner/ASN, load, seen
times, compatibility state, and redacted switch results. The current bounded
candidate set holds a public server key only until a confirmed switch can use
it; no private WireGuard material is persisted by this feature.

## Test and switch / recovery

**Test and switch** always shows old and new endpoint values and requires an
explicit confirmation. It snapshots the complete OPNsense peer before writing
the endpoint, port, and public key. If the selected gateway's configured
`gateway` or `monitor` field exactly equals the old endpoint, it snapshots and
updates that field too; tunnel-address gateways are left alone. HomeLabHQ then
saves the peer, applies OPNsense WireGuard configuration, and waits up to 12
seconds for a fresh authenticated handshake. Failure causes restoration of the
complete peer snapshot and any changed gateway snapshot, followed by another
apply and rollback-status report.

OPNsense currently exposes `wireguard/service/reconfigure`, whose controller
reconfigures the WireGuard service rather than one peer. HomeLabHQ uses that
documented operation because no narrower public API is available; it does not
restart the firewall or issue shell commands. The controller also has no
documented per-peer “send traffic now” action, so HomeLabHQ does not guess a
ping/command route to force a handshake. Ensure policy-routed traffic exists
during a switch so the peer can negotiate. If both switch and rollback report
failure, disable the profile, use the OPNsense GUI to restore the saved known
working endpoint/public key from your own configuration backup, apply
WireGuard, and verify a fresh handshake before re-enabling HomeLabHQ.

To disable the feature, open Configure and turn off endpoint management. This
stops discovery and alerting and does not alter the current firewall peer.
