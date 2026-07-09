"""Small network helpers shared across drivers and the client aggregator."""
import socket
import time
from concurrent.futures import ThreadPoolExecutor

_RDNS_CACHE = {}  # ip -> (ts, hostname); negative results cached too


def _rdns_one(ip):
    """One blocking reverse-DNS lookup with a short timeout. Returns '' when the
    host has no PTR record or the lookup fails."""
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(0.8)
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(old)


def resolve_hostnames(ips, ttl=3600):
    """Resolve many IPs to short hostnames at once, cached (positive+negative).

    Lookups run concurrently so resolving a large client list — some without a
    PTR record — costs about one timeout total rather than one per host.
    Returns {ip: hostname} ('' when unknown)."""
    now = time.time()
    out, todo = {}, []
    for ip in {(i or "").strip() for i in ips if (i or "").strip()}:
        hit = _RDNS_CACHE.get(ip)
        if hit and (now - hit[0]) < ttl:
            out[ip] = hit[1]
        else:
            todo.append(ip)
    if todo:
        with ThreadPoolExecutor(max_workers=min(16, len(todo))) as ex:
            for ip, name in zip(todo, ex.map(_rdns_one, todo)):
                _RDNS_CACHE[ip] = (time.time(), name)
                out[ip] = name
    return out


def is_private_ip(ip):
    """True for RFC1918 / link-local IPv4 — used to keep WAN-side ARP peers out
    of the LAN client list."""
    ip = (ip or "").strip()
    if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("169.254."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False
