# WireGuard VPN Endpoint Manager

The VPN Endpoint Manager is a WireGuard endpoint discovery,
ownership-classification, health-monitoring and assisted replacement feature
for manually managed VPN endpoints. It remains part of an OPNsense device’s
detail view. One OPNsense device can have up to ten independently configured
manager profiles, each bound to one existing WireGuard peer and instance.

NordVPN is currently the only candidate-discovery source. HomeLabHQ retrieves
public WireGuard server addresses and public keys from NordVPN’s public API,
then uses RDAP to look up the registered network organisation and ASN for each
address. It does not retrieve or change the tunnel’s private key.

The endpoint/public-key retrieval approach is informed by the
[SYSADMIN102 OPNsense NordVPN guide](https://sysadmin102.com/2025/01/opnsense-wireguard-nordvpn-setup/).
That guide is implementation background; HomeLabHQ uses its own bounded clients
and OPNsense integration.

## Configure profiles

Configure and test the WireGuard instance and peer in OPNsense first. In
HomeLabHQ, open that OPNsense device, find **VPN Endpoint**, and select **+** to
add the first managed endpoint. Give each profile a concise name such as the
country or tunnel purpose. Use the profile tabs to switch between managed
endpoints; **More** → **Settings** edits the selected profile.

Settings are grouped by purpose:

- **Tunnel** selects the OPNsense WireGuard instance and peer and, when needed,
  records an associated gateway UUID. Each peer can therefore retain its own
  country, discovery preferences and monitoring state.
- **Discovery** selects the NordVPN country, optional preferred city, candidate
  limit and discovery interval.
- **Network preferences** accepts optional preferred and excluded owner
  patterns and controls whether unknown owners are included.
- **Monitoring** sets the stale-handshake warning threshold.
- **Compatibility targets** defines optional manual validation checks.
- **Notes** stores operator context for the profile.

New profiles have no preferred owner patterns, no excluded owner patterns and
no compatibility targets. Owner patterns use case-insensitive, whole-word
normalised matching. No hosting organisation, ASN or exit address is preferred,
excluded or claimed compatible unless the user records the relevant rule or
manual validation.

Existing installations with one `vpnEndpointProfile` are read as a stable
`default` profile. The first profile write moves it to the multi-profile list
without replacing its configured peer, owner patterns, targets or notes.
Removing a manager requires confirmation and deletes only HomeLabHQ’s profile
and its bounded candidate/validation history; it does not change OPNsense.

For example, a user may prefer one hosting network because a required service
accepts its exit addresses, while excluding another network whose exits do not
meet their requirements. Those are local operational choices, not HomeLabHQ
recommendations.

## Endpoint and candidate states

The compact default view shows tunnel health, ownership classification,
hostname, endpoint address, recent handshake state and a validation summary
when targets exist. Peer identifiers, byte counters, exact timestamps,
discovery metadata and other diagnostics remain under **Details**.

Ownership classifications are:

- **Preferred** — matches a user-configured preferred owner pattern.
- **Excluded** — matches a user-configured exclusion pattern.
- **Eligible** — has the required metadata and matches neither list.
- **Unknown** — ownership or required metadata could not be established.

The UI separately reports **Active**, **Stale** and **Unhealthy** runtime or
history conditions. These labels are textual; colour is only supplementary.

**Find replacement** opens a focused panel. It shows the top three preferred or
eligible candidates first, with the remaining eligible set behind **Show all
candidates** and excluded or unknown results under **Other candidates**. A
candidate shows hostname, owner or ASN, city, load, endpoint and any configured
validation summary.

**More** → **Refresh from OPNsense** rereads the selected peer’s currently
configured endpoint and handshake status without changing it. Applying a
replacement remains an explicit **Use** → **Apply and verify** operation.

## Manual compatibility targets

A compatibility target is a user-named service, application or operational
requirement to check through an endpoint. A target can represent, for example,
a streaming service, smart-home cloud service, corporate portal, banking site,
gaming service, email provider or custom application. HomeLabHQ ships no
predefined target.

Each target has a stable ID, name, optional description, validation state, last
validation timestamp and optional note. The manual states are **Verified**,
**Failed**, **Assumed** and **Unknown**. Use **View checks** on a candidate to
record a result. Removing a target with saved validation history requires
confirmation.

Ownership and ASN data do not prove that a service accepts an endpoint.
HomeLabHQ does not collect third-party credentials, automate third-party login,
probe private or undocumented APIs, or send arbitrary user-configured requests.
Automated validation requires a separate future security design and is not part
of this feature.

Development builds of the original feature stored candidate checks in the
schema-v2 fields `compatibility`, `compatibilityAt` and `compatibilityNote` and
used `Rejected` as a classification. During the compatibility period,
HomeLabHQ reads that shape, converts actual saved check data into one neutral
**Imported compatibility check**, and normalises `Rejected` to `Excluded`.
An `Unknown` placeholder by itself does not create a target. The next profile or
discovery write persists neutral candidate validations, and the next profile
write persists neutral profile fields, without resetting existing owner
patterns or overwriting the rest of the profile.

## Health monitoring

A recent authenticated WireGuard handshake is the primary tunnel-health
signal. Gateway or dpinger status is supporting diagnostic information only and
is never treated as proof of a healthy WireGuard session.

The background poller can report a stale handshake, an active endpoint missing
from fresh discovery, an active endpoint with excluded or unknown ownership,
or the absence of a preferred candidate when the user actually configured
preferred patterns. Alerts require two successive observations so a single
discovery failure does not create a notification.

## Assisted replacement and rollback

Selecting **Use** shows the current and replacement ownership and endpoint
values. **Apply and verify** then:

1. snapshots the complete OPNsense peer configuration and associated gateway
   configuration, when configured;
2. changes the endpoint address, port and public server key;
3. applies OPNsense WireGuard configuration;
4. waits up to 12 seconds for a new authenticated handshake; and
5. restores the complete peer and any changed gateway snapshot if verification
   fails.

Gateway address or monitor fields are changed only when they exactly equal the
old endpoint. Tunnel-address gateways are left unchanged. Switch and rollback
results are reported explicitly. A successful rollback means OPNsense accepted
the complete restored configuration and WireGuard reconfiguration; the UI and
logs separately report whether a restored-tunnel handshake has been observed.
Failures before the first configuration write are reported as unchanged and do
not trigger an unnecessary rollback. OPNsense's expanded peer-relation response
is normalised back to its documented comma-separated SET representation before
an endpoint is applied or restored.
Safe failure stages and redacted driver errors are written to HomeLabHQ's
structured Logs view. HomeLabHQ does not perform automatic or unattended
endpoint failover.

OPNsense exposes `wireguard/service/reconfigure`, which applies the WireGuard
service rather than one peer. No narrower documented public controller is
currently available. OPNsense also has no documented per-peer operation to
force traffic and trigger a handshake, so policy-routed traffic must exist while
verification runs.

## External services, retained data and security

The feature contacts only these fixed external destinations:

- `https://api.nordvpn.com` for candidate discovery;
- `https://rdap.org` to select the authoritative address registry;
- the official AFRINIC, APNIC, ARIN, LACNIC or RIPE RDAP endpoint selected for
  that public address; and
- the owner-configured OPNsense device through HomelabHQ’s existing encrypted
  device credential and connection boundary.

NordVPN and RDAP clients use TLS, fixed destinations, explicit timeouts and
bounded responses. The RDAP client accepts a maximum of three registry referrals
only when each uses HTTPS and names an official, allowlisted regional registry
host. Arbitrary targets, referral loops and longer chains remain blocked. Their
payloads are treated as untrusted input and are not dumped into logs. RDAP
failure produces unknown ownership instead of a compatibility conclusion.
OPNsense credentials, private keys and complete rollback snapshots are never
returned to the browser or stored in candidate history.

HomeLabHQ retains at most 100 owner-scoped candidate and switch-history entries
across all manager profiles on a device. Candidate and discovery state carries
the stable profile ID so tunnels cannot consume one another’s candidates or
validations. Retained fields include public endpoint metadata, public-key
fingerprints, owner/ASN observations, seen times, manual target validations and
redacted switch outcomes. The bounded current candidate set temporarily retains
the public server key needed for a confirmed switch. No private WireGuard key is
persisted by this feature.

## Limitations and manual recovery

Candidate discovery is NordVPN-specific, ownership data can be incomplete or
stale, and a fresh handshake proves tunnel authentication rather than
third-party service acceptance. Manual targets are observations made by the
operator, not guarantees.

If both replacement and rollback fail, disable endpoint management and use the
OPNsense GUI to restore the known-working endpoint and public key from your
configuration backup. Apply WireGuard configuration, verify a fresh handshake,
then re-enable the HomeLabHQ profile. Disabling the profile stops discovery and
alerts; it does not mutate the current peer.
