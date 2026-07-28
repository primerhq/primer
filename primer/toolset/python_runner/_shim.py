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
    SCMP_ACT_ERRNO = 0x00050000 | (EPERM & 0x0000FFFF)

    denied = [
        "ptrace", "process_vm_readv", "process_vm_writev",
        "fork", "vfork", "execve", "execveat",
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
        return lib.seccomp_load(ctypes.c_void_p(ctx)) == 0
    finally:
        lib.seccomp_release(ctypes.c_void_p(ctx))


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


try:
    _out = _main()
except Exception as _exc:
    _out = {"ok": False, "error": {"type": type(_exc).__name__,
                                   "message": str(_exc),
                                   "traceback": traceback.format_exc()[-4000:]}}
sys.stdout.write(json.dumps(_out))
'''
