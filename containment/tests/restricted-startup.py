"""Exec the single Rust test on an accepted socket with openat2 returning ENOSYS."""
import ctypes
import errno
import os
import socket
import sys

lib = ctypes.CDLL("libseccomp.so.2")
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
number = lib.seccomp_syscall_resolve_name(b"openat2")
ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
if number < 0 or not ctx or lib.seccomp_rule_add(ctx, 0x50000 | errno.ENOSYS, number, 0):
    sys.exit("failed to construct openat2 errno rule")
if lib.seccomp_load(ctx):
    sys.exit("failed to install seccomp filter")
client, connection = socket.socketpair()
os.dup2(connection.fileno(), 0)
os.closerange(3, os.sysconf("SC_OPEN_MAX"))
os.environ["WRIT_RESTRICTED_TEST_CHILD"] = "1"
os.execv(sys.argv[1], [sys.argv[1], "--exact", "restricted_startup", "--nocapture"])
