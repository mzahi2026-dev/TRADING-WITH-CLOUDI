import socket
from urllib3.util import connection

# Global fix: Force IPv4 on Windows to prevent dual-stack IPv6 DNS/handshake hangs on Binance APIs
_orig_create_connection = connection.create_connection

def _force_ipv4(address, *args, **kwargs):
    host, port = address
    addrinfo = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    af, socktype, proto, canonname, sa = addrinfo[0]
    sock = socket.socket(af, socktype, proto)
    sock.connect(sa)
    return sock

connection.create_connection = _force_ipv4
