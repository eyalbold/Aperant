#!/usr/bin/env python3
"""
landlock-exec: run a command under a Linux Landlock filesystem allowlist.

Usage:
    landlock-exec [--ro PATH]... [--rw PATH]... [--preset standard]
                  [--best-effort] [--allow-unsupported] [-v] -- CMD [ARGS...]

The wrapper builds a Landlock ruleset, calls prctl(PR_SET_NO_NEW_PRIVS),
landlock_restrict_self(), then execs CMD. The kernel enforces the allowlist
on the exec'd process and all descendants. Anything outside listed paths
returns EACCES at open()/openat() time.

Requires Linux >= 5.13 with landlock syscalls available. No privileges needed.

GOTCHA — allowlist semantics, no carve-outs:
    Landlock has no deny rules. If you grant access to PARENT, then every
    child under PARENT is accessible — a more specific allow rule cannot
    SUBTRACT. To "blacklist" PARENT/forbidden while keeping PARENT/allowed
    accessible, do NOT grant PARENT itself; grant only PARENT/allowed (and
    enumerate other siblings you actually need). Rule of thumb: if you want
    a path blocked, make sure no rule (including --preset) covers it or
    any of its ancestors.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import Structure, byref, c_int, c_uint64

SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446
PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_RULE_PATH_BENEATH = 1

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13
TRUNCATE = 1 << 14

ALL_FS = (1 << 15) - 1
RO_MASK = READ_FILE | READ_DIR | EXECUTE
RW_MASK = ALL_FS  # caller wants full access; we'll & with kernel-supported bits

# /usr, /lib*, /bin, /sbin, /etc, /opt — RW so installers (pip/apt/npm) work.
# /var, /tmp, /run, /dev — RW for runtime state.
# $HOME — RW for ~/.cache, ~/.config, ~/.npm, etc.
# /proc, /sys — RW (most subdirs aren't writable anyway by uid).
PRESET_STANDARD_RW = [
    "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt",
    "/var", "/tmp", "/run", "/dev",
    "/proc", "/sys",
]


class _RulesetAttr(Structure):
    _fields_ = [
        ("handled_access_fs", c_uint64),
        ("handled_access_net", c_uint64),
        ("scoped", c_uint64),
    ]


class _PathBeneathAttr(Structure):
    _fields_ = [
        ("allowed_access", c_uint64),
        ("parent_fd", c_int),
    ]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _create_ruleset(handled: int) -> tuple[int, int]:
    attr = _RulesetAttr(handled, 0, 0)
    fd = _libc.syscall(SYS_landlock_create_ruleset, byref(attr), ctypes.sizeof(attr), 0)
    return fd, ctypes.get_errno()


def _probe_supported_bits() -> int:
    """Find the largest handled_access_fs the kernel accepts."""
    handled = ALL_FS
    while handled:
        fd, err = _create_ruleset(handled)
        if fd >= 0:
            os.close(fd)
            return handled
        if err != 22:  # not EINVAL → some other failure
            return 0
        handled >>= 1
    return 0


def _add_path(ruleset_fd: int, path: str, access: int) -> None:
    parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        pb = _PathBeneathAttr(access, parent)
        rc = _libc.syscall(
            SYS_landlock_add_rule, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, byref(pb), 0
        )
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_add_rule({path}): {os.strerror(err)}")
    finally:
        os.close(parent)


def _restrict(ruleset_fd: int) -> None:
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")
    rc = _libc.syscall(SYS_landlock_restrict_self, ruleset_fd, 0)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock_restrict_self: {os.strerror(err)}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        print("landlock-exec: missing '--' before command", file=sys.stderr)
        return 2
    sep = argv.index("--")
    own_args, cmd = argv[:sep], argv[sep + 1 :]
    if not cmd:
        print("landlock-exec: empty command after '--'", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(prog="landlock-exec", add_help=True)
    p.add_argument("--ro", action="append", default=[], metavar="PATH",
                   help="allow read+exec on PATH (repeatable)")
    p.add_argument("--rw", action="append", default=[], metavar="PATH",
                   help="allow read+write+exec on PATH (repeatable)")
    p.add_argument("--preset", choices=["standard"], default=None,
                   help="prepend a preset of system paths (standard = /usr /etc /lib* /var /tmp /home etc., all RW)")
    p.add_argument("--best-effort", action="store_true",
                   help="skip --ro/--rw paths that don't exist instead of failing")
    p.add_argument("--allow-unsupported", action="store_true",
                   help="if Landlock is unavailable, exec without restriction (fail-open)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(own_args)

    rules: list[tuple[str, int]] = []
    if args.preset == "standard":
        for path in PRESET_STANDARD_RW:
            rules.append((path, RW_MASK))
        home = os.environ.get("HOME")
        if home:
            rules.append((home, RW_MASK))
    rules.extend((path, RO_MASK) for path in args.ro)
    rules.extend((path, RW_MASK) for path in args.rw)

    handled = _probe_supported_bits()
    if not handled:
        msg = "landlock-exec: Landlock not available on this kernel"
        if args.allow_unsupported:
            print(f"{msg}; running unrestricted (--allow-unsupported)", file=sys.stderr)
            os.execvp(cmd[0], cmd)
        print(f"{msg} (use --allow-unsupported to bypass)", file=sys.stderr)
        return 3

    fd, err = _create_ruleset(handled)
    if fd < 0:
        print(f"landlock-exec: create_ruleset failed: {os.strerror(err)}", file=sys.stderr)
        return 4

    try:
        for path, mask in rules:
            real = os.path.realpath(path)
            if not os.path.exists(real):
                if args.best_effort:
                    if args.verbose:
                        print(f"landlock-exec: skip missing {path}", file=sys.stderr)
                    continue
                print(f"landlock-exec: path does not exist: {path}", file=sys.stderr)
                return 5
            try:
                _add_path(fd, real, mask & handled)
            except OSError as e:
                print(f"landlock-exec: {e}", file=sys.stderr)
                return 6
            if args.verbose:
                kind = "rw" if mask == RW_MASK else "ro"
                print(f"landlock-exec: + {kind} {real}", file=sys.stderr)

        try:
            _restrict(fd)
        except OSError as e:
            print(f"landlock-exec: {e}", file=sys.stderr)
            return 7
    finally:
        os.close(fd)

    if args.verbose:
        print(f"landlock-exec: exec {cmd[0]} (handled=0x{handled:x})", file=sys.stderr)
    os.execvp(cmd[0], cmd)
    return 127  # unreachable


if __name__ == "__main__":
    sys.exit(main())
