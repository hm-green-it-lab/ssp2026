# helper/tunnel.py
"""
Reverse SSH port forwarding, so the SUT can reach the controller's collector.

The collector runs on the controller, but the SUT-side exporters have to reach
it, and a direct connection is not always possible:

- with podman or Docker Desktop on Windows the published port lives inside a
  WSL2 VM and is exposed to Windows *only* through loopback forwarding, so
  nothing is listening on the controller's LAN address at all;
- controllers behind NAT, on a different VLAN, or with a restrictive host
  firewall have the same problem for less exotic reasons.

A reverse forward avoids all of it by reusing the SSH connection that is
already open: sshd listens on the SUT's own loopback and pipes each connection
back to the controller, which then connects to the collector over *its*
loopback -- the one path that works. The SUT-side exporters simply post to
``http://127.0.0.1:4318``.

The cost is one extra hop plus SSH encryption for the telemetry stream. That
traffic is batched and small, and it never touches the request path -- the
exporter threads are not the threads serving transactions -- so it does not
distort the latency being measured.
"""

from __future__ import annotations

import select
import socket
import threading

import paramiko

# Bind on the SUT's loopback only: the tunnel is for the SUT's own processes,
# and binding the wildcard would additionally require GatewayPorts on sshd.
BIND_ADDRESS = "127.0.0.1"

# The controller end connects here. A hostname rather than an IPv4 literal, so
# it works whether the container runtime published to 127.0.0.1 or ::1.
LOCAL_HOST = "localhost"


def _pump(channel: paramiko.Channel, sock: socket.socket) -> None:
    """Copy bytes both ways until either side closes."""
    try:
        while True:
            readable, _, _ = select.select([channel, sock], [], [], 5)
            if channel in readable:
                data = channel.recv(16384)
                if not data:
                    break
                sock.sendall(data)
            if sock in readable:
                data = sock.recv(16384)
                if not data:
                    break
                channel.sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        try:
            channel.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class ReverseTunnel:
    """A remote port forward: ``SUT:remote_port`` -> ``controller:local_port``."""

    def __init__(self, client: paramiko.SSHClient, remote_port: int, local_port: int) -> None:
        self.client = client
        self.remote_port = remote_port
        self.local_port = local_port
        self.active = False

    def _handle(self, channel: paramiko.Channel, origin, server) -> None:
        """Accept one forwarded connection and hand it to a worker thread.

        This **must return immediately**: paramiko invokes the handler from the
        transport's own thread, so blocking here stalls the entire SSH
        connection -- every exec channel on it stops receiving data, and the
        session deadlocks rather than merely running slowly.
        """
        worker = threading.Thread(
            target=self._serve, args=(channel,), daemon=True, name="tunnel-pump"
        )
        worker.start()

    def _serve(self, channel: paramiko.Channel) -> None:
        """Connect to the collector and pump until either end closes."""
        try:
            sock = socket.create_connection((LOCAL_HOST, self.local_port), timeout=10)
        except OSError as exc:
            print(
                f"[tunnel] could not reach the collector at "
                f"{LOCAL_HOST}:{self.local_port} on the controller: {exc}"
            )
            channel.close()
            return
        _pump(channel, sock)

    def start(self) -> None:
        """Ask sshd to listen on the SUT and forward back to us."""
        transport = self.client.get_transport()
        if transport is None:
            raise RuntimeError("[tunnel] SSH transport is not connected")

        try:
            transport.request_port_forward(BIND_ADDRESS, self.remote_port, handler=self._handle)
        except paramiko.SSHException as exc:
            raise RuntimeError(
                f"[tunnel] the SUT refused to forward port {self.remote_port}: {exc}\n"
                "Something may already be listening on it, or sshd has "
                "AllowTcpForwarding disabled."
            ) from exc

        self.active = True
        print(
            f"[tunnel] SUT 127.0.0.1:{self.remote_port} -> controller "
            f"{LOCAL_HOST}:{self.local_port}"
        )

    def stop(self) -> None:
        """Tear the forward down; safe to call more than once."""
        if not self.active:
            return
        transport = self.client.get_transport()
        if transport is not None:
            try:
                transport.cancel_port_forward(BIND_ADDRESS, self.remote_port)
            except (paramiko.SSHException, OSError):
                pass
        self.active = False
        print(f"[tunnel] closed SUT 127.0.0.1:{self.remote_port}")

    def __enter__(self) -> "ReverseTunnel":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()
