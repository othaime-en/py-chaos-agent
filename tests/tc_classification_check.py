"""
Run inside a throwaway network namespace by test_network_destinations.py.

Applies the filters build_delay_commands generates to a real interface and sends
real traffic, to check that exactly the selected destinations are classified into
the delayed band. HTB stands in for prio + netem, because only the classifier is
under test and not every kernel has those qdiscs. IPv6 traffic is not sent (not
every sandbox has IPv6 sockets); its filter syntax is still installed.
"""

import os
import re
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.failures.network import build_delay_commands  # noqa: E402

DESTINATIONS = [
    {"cidr": "127.0.0.1/32", "port": 9001, "protocol": "tcp"},
    {"cidr": "::1/128", "port": 9002, "protocol": "tcp"},  # syntax only
    {"cidr": "127.0.0.1/32", "port": 9004, "protocol": "udp"},
    {"cidr": "127.0.0.2/32"},
]


def sh(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED:", " ".join(command), "\n", result.stderr)
        sys.exit(1)
    return result.stdout


def band_packets():
    out = sh("tc -s class show dev lo".split())
    found = {}
    for m in re.finditer(r"class htb (1:\d+).*?Sent \d+ bytes (\d+) pkt", out, re.S):
        found[m.group(1)] = int(m.group(2))
    return found


def measure(send):
    before = band_packets()
    send()
    time.sleep(0.3)
    after = band_packets()
    return {k: after.get(k, 0) - before.get(k, 0) for k in ("1:2", "1:3")}


def serve_tcp(host, port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
                conn.recv(100)
                conn.close()
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()


def tcp(host, port):
    def send():
        for _ in range(3):
            c = socket.create_connection((host, port))
            c.sendall(b"x")
            c.close()

    return send


def udp(host, port):
    def send():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(3):
            s.sendto(b"x", (host, port))

    return send


def main():
    sh("ip link set lo up".split())
    commands = build_delay_commands("lo", 100, DESTINATIONS)
    # HTB instead of prio + netem; the filters are applied exactly as generated.
    sh("tc qdisc add dev lo root handle 1: htb default 2".split())
    sh("tc class add dev lo parent 1: classid 1:2 htb rate 10gbit".split())
    sh("tc class add dev lo parent 1: classid 1:3 htb rate 10gbit".split())
    for command in commands[2:]:
        sh(command)

    for port in (9001, 9003, 9004):
        serve_tcp("127.0.0.1", port)
    serve_tcp("127.0.0.2", 9005)
    udp_sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sink.bind(("127.0.0.1", 9004))
    time.sleep(0.2)

    cases = [
        ("tcp 127.0.0.1:9001 (selected)", tcp("127.0.0.1", 9001), True),
        ("tcp 127.0.0.1:9003 (other port)", tcp("127.0.0.1", 9003), False),
        ("tcp 127.0.0.1:9004 (rule is udp only)", tcp("127.0.0.1", 9004), False),
        ("udp 127.0.0.1:9004 (selected)", udp("127.0.0.1", 9004), True),
        ("udp 127.0.0.1:9003 (other port)", udp("127.0.0.1", 9003), False),
        ("tcp 127.0.0.2:9005 (whole host)", tcp("127.0.0.2", 9005), True),
    ]
    wrong = 0
    for name, send, delayed in cases:
        seen = measure(send)
        ok = (seen["1:3"] > 0) == delayed
        wrong += not ok
        print(
            "PASS" if ok else "FAIL",
            name,
            "-> delayed band",
            seen["1:3"],
            "default band",
            seen["1:2"],
        )
    print("RESULT:", "all classified correctly" if not wrong else f"{wrong} wrong")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
