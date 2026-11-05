"""Exercise the private loopback and compare it with a reachable runner canary."""
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import sys
from threading import Thread
from urllib.request import build_opener, ProxyHandler

BODY = b"symphony-network-probe"


class Canary(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


def read_canary(host, port):
    with build_opener(ProxyHandler({})).open(f"http://{host}:{port}/", timeout=3) as response:
        if response.read() != BODY:
            raise RuntimeError("Wrong canary response")


def blocked(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            raise RuntimeError("Network escaped to " + host)
    except OSError as exc:
        if exc.errno != errno.ENETUNREACH:
            raise RuntimeError("Expected no route, not an unresponsive endpoint") from exc


def isolate(gateway, port, marker):
    interfaces = [name for _, name in socket.if_nameindex()]
    if interfaces != ["lo"] or Path("/proc/net/route").read_text().splitlines()[1:]:
        raise RuntimeError("Unexpected network interface or IPv4 route")
    with HTTPServer(("127.0.0.1", 0), Canary) as own:
        server = Thread(target=own.handle_request, daemon=True)
        server.start()
        read_canary("127.0.0.1", own.server_port)
        server.join(3)
    blocked(gateway, port)
    blocked("1.1.1.1", 443)
    if any(Path(path).exists() for path in (marker, "/var/run/docker.sock", "/home/runner/work")):
        raise RuntimeError("Runner marker, control socket or host workspace visible")
    names = {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "ACTIONS_RUNTIME_TOKEN"}
    if names.intersection(os.environ) or os.geteuid() == 0:
        raise RuntimeError("Unexpected credential environment or user")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if int(status["CapEff"], 16) or int(status["NoNewPrivs"]) != 1:
        raise RuntimeError("Capabilities or privilege escalation available")
    print("ISOLATION_OK " + json.dumps({
        "interfaces": interfaces, "own_http": True, "host_canary": "ENETUNREACH",
        "internet": "ENETUNREACH", "runner_marker_visible": False, "credentials_visible": False,
        "uid": os.geteuid(), "effective_capabilities": 0, "no_new_privileges": True,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    action, host, value, *extra = sys.argv[1:]
    if action == "serve":
        with HTTPServer((host, 0), Canary) as http:
            Path(value).write_text(str(http.server_port))
            http.serve_forever()
    elif action == "control":
        read_canary(host, int(value))
        print("Positive control: runner canary reached from the regular Docker bridge.")
    elif action == "isolate":
        isolate(host, int(value), *extra)
    else:
        sys.exit("Unknown probe")
