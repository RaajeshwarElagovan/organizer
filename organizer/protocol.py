"""JSON-lines protocol over a Unix stream socket."""
import json
import socket

from .paths import socket_path

MAX_MESSAGE = 32 * 1024 * 1024


class DaemonUnavailable(Exception):
    pass


def write_message(sock: socket.socket, obj) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def read_message(sock: socket.socket):
    buf = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if buf.endswith(b"\n"):
            break
        if len(buf) > MAX_MESSAGE:
            raise ValueError("message too large")
    if not buf:
        return None
    return json.loads(buf.decode("utf-8"))


def send_request(req: dict, timeout: float = 300.0) -> dict:
    path = socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError as e:
        s.close()
        raise DaemonUnavailable("%s: %s" % (path, e))
    try:
        write_message(s, req)
        resp = read_message(s)
    finally:
        s.close()
    if resp is None:
        raise DaemonUnavailable("daemon closed connection without a reply")
    return resp
