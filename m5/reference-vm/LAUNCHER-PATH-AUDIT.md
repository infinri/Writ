# Launcher path resolution on the Noble reference guest

The owner observed errno 38 at the proc fd directory open on 2026-11-21. The original launcher digest verified after restoration. The client sent no request. systemd 255 applies a dedicated unconditional openat2 errno rule for RestrictSUIDSGID, separately from the explicit syscall blacklist. The rule applies across fork, uid change and exec, including helper cleanup self-exec, private bubblewrap and the eventual workload. A successful hello tests admission and startup only.

## Inventory

The rows cover the 17 original direct call sites in containment, grouped by the operation they protect. The original revisions of the four changed modules are identical in the packaged binary source 50f7628 and reference-script source f20bb88.

| Original site | Resolution requirements and disposition |
| --- | --- |
| fdplan::list_own_fds | Empty resolve mask, read-only directory, CLOEXEC, no creation. Direct openat follows the deliberate proc self link. Dir still owns the enumerator fd and excludes only that fd. |
| fdplan::dev_null | No symlinks anywhere. Open the single directory component /dev with O_PATH, DIRECTORY and NOFOLLOW, then the literal null with NOFOLLOW on the pinned parent. Move the parent high first so reserving descriptors 3 and 4 still works. The private /dev mount is intentional. |
| Cgroups::open (2), RealOps::join_job (3) | Absolute cgroup mount with no ordinary or magic links, then relative unit and job/cgroup.procs beneath pinned parents with no links. Walk ordinary names only, with NOFOLLOW at every open, DIRECTORY on intermediate opens and CLOEXEC throughout. Root and mount topology are controlled by the reference unit. Linux 6.8 cgroup v2 forbids directory rename, so a pinned child cannot be reparented outside the unit. Removed directories remain pinned by identity. These sites never required NO_XDEV. The walker is private to the cgroup module. |
| Cgroups::read, write, job_exists (3) | Same beneath and no-link guarantees for memory.max, controller setup, job limits, membership, counters, kill and freeze. Single-name and nested accesses share the cgroup walk. Existing callers retain their error behavior. |
| Job::prepare, remove_partial (2) | BENEATH and NO_SYMLINKS for exactly one component of an already-open parent. job- plus a validated instance contains no slash, and work is a literal. DIRECTORY and NOFOLLOW on the direct open are sufficient. Mount crossing was permitted. |
| source::copy absolute source root | NO_SYMLINKS and NO_MAGICLINKS over the entire path. The configured read-only bind mount is intentional. Retained with the source-copy transaction pending the decision below. |
| source::open_beneath | BENEATH, NO_SYMLINKS, NO_MAGICLINKS and NO_XDEV for run-ID, source descendants, destination work and subdirectories, and the later chown walk. The before/after inode checks, type, ACL, ownership and access-mode checks apply in addition. Retained. |
| source::copy exclusive destination file creation | Same four flags, relative to the verified destination directory, with CREATE, EXCL and NOFOLLOW. Retained with the copy transaction. |
| helper::open_work | Absolute work path, no ordinary or magic links, directory fd obtained before fchmod. The path intentionally enters the jobs mount. Retained with cleanup. |
| helper::open_dir_beneath | Same four flags including NO_XDEV for recursive cleanup and the retry after EACCES. Used by partial cleanup and the dropped-identity helper. Retained. |

The hostsys calls from these processes are socket credential/receive operations and a pure cgroup counter parser. The hostsys readiness probe and filesystem-effect adapter are separate entry points. Runtime manifests use the existing bwrap module. External executable behavior is outside the Rust inventory; all launched programs inherit the restriction and cannot assume openat2 is available.

## Job execution decision

J-11.2 requires inherited SUID/SGID restrictions, and J-11.4 explicitly mandates openat2 at each untrusted source open. Both requirements cannot be met in that process on this systemd version. NOFOLLOW does not block bind mounts. Device and inode equality across statx and fstat can hold on the same object exposed by a bind mount. A mount-ID check after an ordinary I/O open also occurs after the open's effects. The cgroup-specific rename guarantee does not apply to attacker-controlled source directories.

A decision is required on separating the openat2-dependent work into an independently confined component, with fixed operations and roots, the existing mount view, bounded descriptors and lifetimes, caller authentication, and equivalent SUID/SGID enforcement for the new component. That would require reviewing source ownership checks, destination creation, chown, cleanup identity and process/descriptor admission together. Alternatively, a reviewed platform policy could enforce the same SUID/SGID invariant while permitting the required constrained opens. Neither approach is implemented. The five source and cleanup call sites still return errors when the kernel denies the operation. Do not submit a job at the startup checkpoint.

The startup test installs an actual libseccomp errno rule in a subprocess, verifies raw ENOSYS, exercises the live fd and cgroup access primitives and checks extra descriptors, symlinks, proc magic links, absolute relative-path input and parent traversal. The guest check must use the existing socket and unit, accept only an ordinary hello with the expected launcher digest, and close without a request.

Sources: [systemd 255 seccomp implementation](https://github.com/systemd/systemd/blob/v255/src/shared/seccomp-util.c#L2148-L2163), [Linux 6.8 cgroup v2 rename rule](https://github.com/torvalds/linux/blob/v6.8/kernel/cgroup/cgroup.c#L268), [J-11.2 and J-11.4](../../docs/implementation/M5-INTERFACES.md).
