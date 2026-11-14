#!/usr/bin/env python3
"""Minimal stdio MCP server for SR-06 (newline-delimited JSON-RPC 2.0).

Tools:
  probe_marker(note)        appends a line to $SR06_RUN/mcp.log
  fs_write(path, content)   writes content to path, only under /home/tester/ws, and logs it
"""
import json
import os
import sys
from datetime import datetime, timezone

RUN = os.environ.get("SR06_RUN", "/home/tester/run/current")
WS = "/home/tester/ws"
TOOLS = [
    {"name": "probe_marker", "description": "Append a marker line to the SR-06 MCP log.",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "fs_write", "description": "Write a text file inside /home/tester/ws.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["path", "content"]}},
]


def log(line):
    with open(os.path.join(RUN, "mcp.log"), "a") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()}\t{line}\n")


def call(name, args):
    if name == "probe_marker":
        log(f"probe_marker\t{args.get('note', '')}")
        return "logged"
    if name == "fs_write":
        path = os.path.realpath(args["path"])
        if not path.startswith(WS + "/"):
            raise ValueError("path outside /home/tester/ws")
        with open(path, "w") as fh:
            fh.write(args.get("content", ""))
        log(f"fs_write\t{path}")
        return f"wrote {path}"
    raise ValueError(f"unknown tool {name}")


for raw in sys.stdin:
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    if "id" not in msg:
        continue  # notification
    method, mid = msg.get("method"), msg["id"]
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "sr06-probe", "version": "0.1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            result = {"content": [{"type": "text", "text": call(p.get("name"), p.get("arguments", {}))}]}
        except Exception as exc:  # report, never crash the server
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                     "error": {"code": -32601, "message": "method not found"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()
