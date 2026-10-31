import socket
from urllib3.util import connection

# Global network resilience fix:
# 1. Force IPv4 to prevent Windows dual-stack hang
# 2. Filter out dead AWS IPs (e.g. 16.76.25.130) that blackhole from regional routing
_orig_getaddrinfo = socket.getaddrinfo
DEAD_IPS = {"16.76.25.130"}

def _patched_getaddrinfo(host, port, *args, **kwargs):
    res = _orig_getaddrinfo(host, port, *args, **kwargs)
    if host and "demo-fapi" in str(host):
        filtered = [r for r in res if len(r) >= 5 and len(r[4]) >= 1 and r[4][0] not in DEAD_IPS]
        if filtered:
            return filtered
    return res

socket.getaddrinfo = _patched_getaddrinfo

def _force_ipv4(address, *args, **kwargs):
    host, port = address
    addrinfo = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    af, socktype, proto, canonname, sa = addrinfo[0]
    sock = socket.socket(af, socktype, proto)
    sock.connect(sa)
    return sock

connection.create_connection = _force_ipv4
