"""An SSH stdio transport bound to one Windows interface; no route changes."""

import os
import socket
import struct
import sys
import threading


def main() -> None:
    address, interface, host, port = sys.argv[1:]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", int(interface)))
        connection.bind((address, 0))
        connection.settimeout(12)
        connection.connect((host, int(port)))
        connection.settimeout(None)

        def upload() -> None:
            try:
                while data := os.read(0, 65536):
                    connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=upload, daemon=True).start()
        while data := connection.recv(65536):
            view = memoryview(data)
            while view:
                view = view[os.write(1, view) :]


if __name__ == "__main__":
    main()
