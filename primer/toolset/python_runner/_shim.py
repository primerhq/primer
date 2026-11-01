"""The in-sandbox runner, shipped as source so a bare interpreter can run it.

Executed as ``python -I -S -c SHIM_SOURCE``. It sets its own resource limits as
its first real work, before the user module is compiled or run: an unprivileged
process can lower a limit but never raise the hard limit again, so user code
cannot undo them. Every limit is set with soft equal to hard, because a
soft-only limit is decorative - user code can raise it straight back to the
hard value.

RLIMIT_NPROC is deliberately absent. It caps processes per real UID rather than
per process tree, so a value low enough to stop forking would also be tripped
by unrelated primer processes running under the same UID. Forking is denied by
the seccomp filter and by the container backend's process limits instead.
"""

from __future__ import annotations

SHIM_SOURCE = r'''
import json, sys, traceback


def _apply_limits(limits):
    try:
        import resource
    except ImportError:
        return
    cpu = int(limits.get("cpu_seconds", 30))
    mem = int(limits.get("address_space_bytes", 512 * 1024 * 1024))
    wanted = [
        (resource.RLIMIT_CPU, cpu),
        (resource.RLIMIT_AS, mem),
        (resource.RLIMIT_FSIZE, 8 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CORE, 0),
    ]
    for res, val in wanted:
        try:
            # soft == hard: a soft-only limit can be raised back by the tool.
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):
            pass


def _apply_seccomp(allow_network):
    """Install a syscall filter via libseccomp, if this host has it.

    Driven through ctypes rather than a Python seccomp package on purpose:
    the shim runs under -I -S, so NO site-package is importable here. A
    dependency would simply never load. libseccomp is a system library, so
    ctypes reaches it while sys.path stays empty.

    Default-allow with a deny list, because a default-deny filter has to
    enumerate everything CPython touches to start up and one omission is a
    crash rather than a contained tool.

    Returns True when a filter was loaded.
    """
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("seccomp")
    if not name:
        return False
    try:
        lib = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return False

    SCMP_ACT_ALLOW = 0x7FFF0000
    EPERM = 1
    ENOSYS = 38
    SCMP_ACT_ERRNO = 0x00050000 | (EPERM & 0x0000FFFF)
    SCMP_ACT_ENOSYS = 0x00050000 | ENOSYS
    SCMP_CMP_MASKED_EQ = 7
    CLONE_THREAD = 0x00010000

    class _ArgCmp(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    # glibc's fork() and posix_spawn() are clone(2) calls, so denying the fork syscall alone stops nothing. setsid and
    # setpgid are what would let a child leave the process group the runner kills on a timeout.
    denied = [
        "ptrace", "process_vm_readv", "process_vm_writev",
        "fork", "vfork", "execve", "execveat", "setsid", "setpgid",
        "mount", "umount2", "pivot_root", "chroot",
        "init_module", "finit_module", "delete_module",
        "kexec_load", "reboot", "swapon", "swapoff",
    ]
    if not allow_network:
        # socket() and socketpair() are deliberately NOT denied: they create
        # an fd and move no data, and asyncio builds its event loop self-pipe
        # from one. Denying them blocks every async tool while adding no
        # safety. What actually reaches the network is denied instead.
        denied += [
            "connect", "bind", "listen", "accept", "accept4",
            "sendto", "sendmsg", "recvfrom", "recvmsg",
        ]

    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(_ArgCmp),
    ]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    ctx = lib.seccomp_init(ctypes.c_uint32(SCMP_ACT_ALLOW))
    if not ctx:
        return False
    try:
        for sysname in denied:
            nr = lib.seccomp_syscall_resolve_name(sysname.encode())
            if nr < 0:
                # Not present on this architecture; nothing to deny.
                continue
            lib.seccomp_rule_add(
                ctypes.c_void_p(ctx), ctypes.c_uint32(SCMP_ACT_ERRNO),
                ctypes.c_int(nr), ctypes.c_uint(0),
            )
        # clone without CLONE_THREAD is a new process; with it, a thread, which stays allowed. Flags are argument 0 on
        # x86_64 and arm64 (s390 swaps the first two arguments).
        nr = lib.seccomp_syscall_resolve_name(b"clone")
        if nr >= 0:
            no_thread = _ArgCmp(0, SCMP_CMP_MASKED_EQ, CLONE_THREAD, 0)
            lib.seccomp_rule_add_array(
                ctypes.c_void_p(ctx), ctypes.c_uint32(SCMP_ACT_ERRNO),
                ctypes.c_int(nr), ctypes.c_uint(1), ctypes.byref(no_thread),
            )
        # clone3 passes its flags behind a pointer the filter cannot read. ENOSYS makes glibc fall back to clone, which
        # the rule above judges.
        nr = lib.seccomp_syscall_resolve_name(b"clone3")
        if nr >= 0:
            lib.seccomp_rule_add(
                ctypes.c_void_p(ctx), ctypes.c_uint32(SCMP_ACT_ENOSYS),
                ctypes.c_int(nr), ctypes.c_uint(0),
            )
        return lib.seccomp_load(ctypes.c_void_p(ctx)) == 0
    finally:
        lib.seccomp_release(ctypes.c_void_p(ctx))


def _apply_landlock(spec):
    """Confine file access with Landlock (ctypes, like seccomp: no site-package is importable here). Read-only on spec["read"], read and write on
    spec["read_write"], everything on spec["scratch"] except execute, nothing else. Returns True when the ruleset is in force.

    Applied AFTER seccomp: loading libseccomp runs ctypes.util.find_library, which starts ldconfig, and the ruleset would refuse that.
    """
    import ctypes
    import os
    import platform

    if platform.system() != "Linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)

    def sys_(nr, *args):
        return libc.syscall(ctypes.c_long(nr), *args)

    abi = sys_(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        return False
    EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
    handled = (1 << 13) - 1                # ABI 1: execute, read, write, remove and make-* rights
    if abi >= 2:
        handled |= 1 << 13                 # REFER
    if abi >= 3:
        handled |= 1 << 14                 # TRUNCATE
    if abi >= 5:
        handled |= 1 << 15                 # IOCTL_DEV
    FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | (1 << 14) | (1 << 15)

    class _Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class _PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    ruleset = sys_(444, ctypes.byref(_Ruleset(handled)), ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        return False
    try:
        def allow(path, rights):
            try:
                fd = os.open(path, os.O_PATH)
            except OSError:
                return
            try:
                if not os.path.isdir(path):
                    rights &= FILE_RIGHTS
                rule = _PathBeneath(rights & handled, fd)
                sys_(445, ctypes.c_int(ruleset), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint32(0))
            finally:
                os.close(fd)

        for path in spec.get("read") or ():
            allow(path, READ_FILE | READ_DIR)
        for path in spec.get("read_write") or ():
            allow(path, READ_FILE | READ_DIR | WRITE_FILE | (1 << 14))
        if spec.get("scratch"):
            allow(spec["scratch"], handled & ~EXECUTE)
        if libc.prctl(38, 1, 0, 0, 0) != 0:            # PR_SET_NO_NEW_PRIVS, required to restrict oneself
            return False
        return sys_(446, ctypes.c_int(ruleset), ctypes.c_uint32(0)) == 0
    finally:
        os.close(ruleset)


class _Yield(Exception):
    def __init__(self, kind, params, meta):
        self.kind = kind
        self.params = params
        self.meta = meta


def _build_namespace():
    def ask_user(question, meta=None):
        raise _Yield("ask_user", {"question": question}, meta or {})

    def sleep_for(seconds, meta=None):
        raise _Yield("timer", {"seconds": seconds}, meta or {})

    def watch_files(paths, meta=None):
        raise _Yield("watch", {"paths": list(paths)}, meta or {})

    def primer_tool(*a, **k):
        if len(a) == 1 and not k and callable(a[0]):
            return a[0]

        def deco(fn):
            return fn

        return deco

    def resumes(_target):
        def deco(fn):
            return fn

        return deco

    return {
        "__name__": "primer_python_tool",
        "ask_user": ask_user,
        "sleep_for": sleep_for,
        "watch_files": watch_files,
        "primer_tool": primer_tool,
        "resumes": resumes,
    }


def _err(kind, message, tb=""):
    return {"ok": False, "error": {"type": kind, "message": message,
                                   "traceback": tb}}


def _main():
    req = json.loads(sys.stdin.read())
    _apply_limits(req.get("limits") or {})
    _apply_seccomp(bool(req.get("allow_network")))
    if req.get("landlock") is not None and not _apply_landlock(req["landlock"]):
        # The runner reported Landlock for this host: running unconfined under that claim would be a lie.
        return _err("RunnerError", "the Landlock confinement could not be applied")

    ns = _build_namespace()
    try:
        exec(compile(req["module"], "<tool>", "exec"), ns)
    except _Yield:
        return _err("RuntimeError",
                    "a yield helper was called at module scope")
    except Exception as exc:
        return _err(type(exc).__name__,
                    "the toolset module failed to load: %s" % exc,
                    traceback.format_exc()[-4000:])

    fn = ns.get(req["fn"])
    if fn is None:
        return _err("NameError",
                    "no function named %r in this toolset" % req["fn"])

    if req.get("phase") == "resume":
        kwargs = {"payload": req.get("payload"), "meta": req.get("meta")}
    else:
        kwargs = dict(req.get("args") or {})
        try:
            import inspect
            if "ctx" in inspect.signature(fn).parameters:
                kwargs["ctx"] = req.get("ctx")
        except (TypeError, ValueError):
            pass

    try:
        result = fn(**kwargs)
        if hasattr(result, "__await__"):
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(result)
            finally:
                loop.close()
    except _Yield as y:
        return {"ok": True, "yield": {"kind": y.kind, "params": y.params,
                                      "meta": y.meta}}
    except Exception as exc:
        return _err(type(exc).__name__, str(exc),
                    traceback.format_exc()[-4000:])

    try:
        json.dumps(result)
    except (TypeError, ValueError):
        return _err("TypeError",
                    "the tool returned a value that is not JSON "
                    "serialisable: %s" % type(result).__name__)
    return {"ok": True, "value": result}


# stdout carries the one response object: a print() in the tool goes to stderr instead of corrupting it.
_stdout, sys.stdout = sys.stdout, sys.stderr
try:
    _out = _main()
except Exception as _exc:
    _out = {"ok": False, "error": {"type": type(_exc).__name__,
                                   "message": str(_exc),
                                   "traceback": traceback.format_exc()[-4000:]}}
sys.stdout = _stdout
sys.stdout.write(json.dumps(_out))
'''
