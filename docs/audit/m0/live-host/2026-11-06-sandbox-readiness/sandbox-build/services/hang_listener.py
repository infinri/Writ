#!/usr/bin/env python3
"""Fault E5: accept connections on the Writ daemon port and never reply. Usage: hang_listener.py [port]"""
import socket
import sys
import threading

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(64)
held = []
while True:
    conn, _ = srv.accept()
    held.append(conn)  # keep open, never respond
