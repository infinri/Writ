"""Check a completed first-inventory report against the four live runbook fixtures."""
import argparse
from collections import Counter
import grp
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return Path(path).read_text().strip()


def unit(name):
    pid = int(subprocess.check_output(
        ["systemctl", "show", "--property=MainPID", "--value", name], text=True))
    require(pid > 1, f"{name} has no live MainPID")
    return Path("/proc") / str(pid)


def facts(proc):
    ticks = (proc / "stat").read_text().rsplit(") ", 1)[1].split()[19]
    status = dict(row.split(":", 1) for row in (proc / "status").read_text().splitlines())
    return [proc.name, ticks, *status["Uid"].split(), *status["Gid"].split(),
            *sorted(status["Groups"].split(), key=int)]


def observation():
    rows = ["writ-desktop-inventory/1", "end"]
    require(socket.gethostname() == "writ-ref-1", "wrong hostname")
    require(read("/etc/writ-disposable-vm").splitlines() ==
            ["writ-disposable-vm/1", "name=writ-ref-1"], "wrong guest marker")
    namespaces = [str(os.stat("/proc/self/ns/" + ns).st_ino) for ns in ("pid", "user", "net")]
    rows.append(" ".join(["scope", socket.gethostname(), read("/proc/sys/kernel/random/boot_id"),
                          *namespaces]))
    accounts = {}
    for role, name in (("service", "writ"), ("job", "writ-job"), ("agent", "writ-agent"),
                       ("desktop", "writ-human")):
        account = pwd.getpwnam(name)
        accounts[role] = account
        listed = sorted(group.gr_gid for group in grp.getgrall() if name in group.gr_mem)
        rows.append(" ".join(map(str, ["account", role, account.pw_uid, account.pw_gid, *listed])))

    agent = unit("writ-inventory-agent")
    desktop = unit("writ-inventory-desktop")
    agent_facts, desktop_facts = facts(agent), facts(desktop)
    for role, status in (("agent", agent_facts), ("desktop", desktop_facts)):
        account = accounts[role]
        require(status[2:10] == [str(account.pw_uid)] * 4 + [str(account.pw_gid)] * 4,
                f"unexpected {role} process identity")
        allowed = set(os.getgrouplist(account.pw_name, account.pw_gid))
        require(set(map(int, status[10:])) | {account.pw_gid} == allowed,
                f"unexpected {role} process groups")
    rows.append(" ".join(["process", *agent_facts]))
    inodes = set()
    for fd in (desktop / "fd").iterdir():
        link = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))
        if link:
            inodes.add(link[1])

    uid = str(accounts["desktop"].pw_uid)
    expected_inodes = set()
    for family, table, addr, mode in (
        ("-4", "tcp", "127.0.0.1", "v4"), ("-6", "tcp6", "::1", "v6-only")
    ):
        version = socket.AF_INET if family == "-4" else socket.AF_INET6
        packed = socket.inet_pton(version, addr)
        proc_hex = b"".join(packed[i:i+4][::-1] for i in range(0, len(packed), 4)).hex().upper()
        matches = [r for line in Path("/proc/net", table).read_text().splitlines()[1:]
                   if (r := line.split())[1] == f"{proc_hex}:{48124:04X}" and r[3] == "0A"]
        require(len(matches) == 1 and matches[0][7] == uid, f"{table} listener mismatch")
        inode = matches[0][9]
        diag = subprocess.check_output(["ss", family, "-H", "-ltne", "( sport = :48124 )"],
                                       text=True)
        require(len(diag.splitlines()) == 1 and f"ino:{inode}" in diag.split(), "ss inode mismatch")
        if family == "-6":
            require("v6only:1" in diag.split(), "IPv6-only binding unconfirmed")
        require(inode in inodes, "desktop process does not hold TCP listener")
        expected_inodes.add(inode)
        rows.extend([f"endpoint {uid} Tcp {inode} {addr} 48124 0 {mode}",
                     f"holder {inode} {desktop.name} {desktop_facts[1]}"])

    for kind, display, address in (("Abstract", "@writ-inventory", b"\0writ-inventory"),
                                   ("Unix", "/home/writ-human/inventory.sock",
                                    b"/home/writ-human/inventory.sock\0")):
        matches = [r for line in Path("/proc/net/unix").read_text().splitlines()[1:]
                   if len(r := line.split(maxsplit=7)) == 8 and r[7] == display]
        require(len(matches) == 1 and matches[0][3:6] == ["00010000", "0001", "01"],
                f"{kind} listener mismatch")
        inode = matches[0][6]
        require(inode in inodes, "desktop process does not hold Unix listener")
        expected_inodes.add(inode)
        rows.extend([f"endpoint {uid} {kind} {inode} 1 10 0 {address.hex()}",
                     f"holder {inode} {desktop.name} {desktop_facts[1]}"])
    # Some threads have private descriptor tables. Inspect every visible task twice.
    for _ in range(2):
        holders = set()
        for proc in Path("/proc").glob("[0-9]*"):
            tasks = list((proc / "task").iterdir())
            for task in tasks:
                for fd in (task / "fd").iterdir():
                    try:
                        target = fd.readlink()
                    except FileNotFoundError:
                        # Listing our own fd directory briefly opens the directory itself.
                        if proc.name == str(os.getpid()):
                            continue
                        raise
                    match = re.fullmatch(r"socket:\[(\d+)\]", str(target))
                    if match and match[1] in expected_inodes:
                        holders.add((proc.name, match[1]))
        require(holders == {(desktop.name, inode) for inode in expected_inodes},
                "missing or additional fixture endpoint holder")
    require(facts(agent) == agent_facts and facts(desktop) == desktop_facts, "process changed")
    return Counter(rows)


def compare(directory):
    require(read(directory / "inventory.exit") == "0", "inventory did not exit successfully")
    raw = (directory / "inventory.txt").read_text()
    require(raw.startswith("writ-desktop-inventory/1\n") and raw.endswith("\nend\n"),
            "incomplete inventory framing")
    rows = []
    for line in raw.splitlines():
        fields = line.split()
        if fields[:1] == ["endpoint"]:
            require(len(fields) == 9, "invalid endpoint")
            cookie_index = 8 if fields[2] == "Tcp" else 7
            require(re.fullmatch("[0-9a-f]{16}", fields[cookie_index]), "invalid socket cookie")
            require(fields[cookie_index] not in ("0" * 16, "f" * 16), "reserved socket cookie")
            del fields[cookie_index]
            line = " ".join(fields)
        rows.append(line)
    require(Counter(rows) == observation(), "report differs from live reference fixtures")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="private inventory evidence directory")
    try:
        compare(parser.parse_args().directory)
    except (KeyError, OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        sys.exit(f"inventory comparison unavailable: {error}")
    print("Matched scope, account and process credentials, start ticks, four endpoints and holders.")
