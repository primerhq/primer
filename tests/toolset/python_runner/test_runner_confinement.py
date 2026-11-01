"""Part 3 Task 106 (GS-2, ruling D17): a python tool runs under Landlock where the kernel has it, and says `rlimit-only` where it does not.

Characterization tests run at every isolation level and pass before and after. The Landlock asserts skip cleanly on a kernel without it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import sysconfig

import pytest

from primer.toolset.python_runner import runners as runners_mod
from primer.toolset.python_runner.protocol import build_request
from primer.toolset.python_runner.runners import IsolationLevel, LocalHardenedRunner

def _kernel_landlock_abi() -> int:
    """Asked straight of the kernel (syscall 444 with the VERSION flag), independent of the code under test."""
    if sys.platform != "linux":
        return 0
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return max(int(libc.syscall(ctypes.c_long(444), None, ctypes.c_size_t(0), ctypes.c_uint32(1))), 0)


LANDLOCK = pytest.mark.skipif(_kernel_landlock_abi() < 1, reason="this kernel has no Landlock")


def _req(module: str, args: dict | None = None) -> dict:
    req = build_request(module=module, fn="run", phase="call", args=args or {}, ctx={}, cpu_seconds=10,
                        address_space_bytes=512 * 1024 * 1024)
    req["allow_network"] = False
    return req


async def _run(module: str, args: dict | None = None, runner: LocalHardenedRunner | None = None):
    resp = await (runner or LocalHardenedRunner(env={})).run(_req(module, args), timeout_seconds=30)
    assert resp.ok, resp.error
    return resp.value


# A representative tool: the stdlib surface existing toolsets use, TLS and DNS files, and its scratch dir.
REPRESENTATIVE = '''
import json, os, re, ssl, datetime, urllib.request, zoneinfo

def run():
    out = {}
    out["json"] = json.loads(json.dumps({"a": 1}))
    out["re"] = re.sub(r"a+", "b", "caat")
    out["zone"] = datetime.datetime(2026, 1, 1, tzinfo=zoneinfo.ZoneInfo("Europe/Paris")).utcoffset().total_seconds()
    ctx = ssl.create_default_context()
    out["ctx"] = ctx.verify_mode == ssl.CERT_REQUIRED
    paths = ssl.get_default_verify_paths()
    bundle = next((p for p in (paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
                   if p and os.path.isfile(p)), None)
    out["bundle_bytes"] = len(open(bundle, "rb").read()) if bundle else -1
    try:
        import certifi
        out["certifi_bytes"] = len(open(certifi.where(), "rb").read())
    except ImportError:
        out["certifi_bytes"] = None            # -I -S keeps site-packages off sys.path
    for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        out[name] = len(open(name, "rb").read()) if os.path.exists(name) else -1
    out["urandom"] = len(open("/dev/urandom", "rb").read(8))
    out["urlopen"] = urllib.request.Request("https://example.invalid/").full_url
    return out
'''


async def test_representative_tool_runs():
    value = await _run(REPRESENTATIVE)
    assert value["json"] == {"a": 1} and value["re"] == "cbt"
    assert value["zone"] == 3600.0 and value["ctx"] is True
    assert value["bundle_bytes"] != 0
    assert value["urandom"] == 8


async def test_a_tool_writes_and_reads_a_file_in_its_scratch_dir():
    module = (
        "import os\n"
        "def run():\n"
        "    path = os.path.join(os.getcwd(), 'note.txt')\n"
        "    open(path, 'w').write('hello')\n"
        "    return {'text': open(path).read(), 'listing': os.listdir(os.getcwd())}\n"
    )
    assert await _run(module) == {"text": "hello", "listing": ["note.txt"]}


async def test_each_call_gets_its_own_scratch_dir_that_is_removed_after():
    module = "import os\ndef run():\n    return {'cwd': os.getcwd(), 'tmp': os.environ.get('TMPDIR')}\n"
    first = await _run(module)
    second = await _run(module)
    assert first["cwd"] != second["cwd"] and first["tmp"] == first["cwd"]
    assert not os.path.exists(first["cwd"])


@LANDLOCK
def test_the_runner_reports_landlock_where_the_kernel_has_it():
    assert LocalHardenedRunner(env={}).isolation_level is IsolationLevel.LANDLOCK


@LANDLOCK
async def test_a_tool_cannot_read_the_api_processs_environment():
    module = "import os\ndef run():\n    return len(open('/proc/%d/environ' % os.getppid(), 'rb').read())\n"
    resp = await LocalHardenedRunner(env={}).run(_req(module), timeout_seconds=30)
    assert resp.ok is False and resp.error["type"] == "PermissionError"


@LANDLOCK
async def test_a_tool_cannot_write_the_api_venv_or_read_outside_its_scratch_dir():
    site_packages = sysconfig.get_paths()["purelib"]
    module = (
        "import os\n"
        "def run(sp, probe):\n"
        "    out = {}\n"
        "    for key, fn in (('write_pth', lambda: open(os.path.join(sp, 'evil.pth'), 'w')),\n"
        "                    ('read_hostname', lambda: open('/etc/hostname')),\n"
        "                    ('read_home', lambda: os.listdir('/home')),\n"
        "                    ('read_probe', lambda: open(probe))):\n"
        "        try:\n"
        "            fn()\n"
        "            out[key] = 'allowed'\n"
        "        except PermissionError:\n"
        "            out[key] = 'denied'\n"
        "    return out\n"
    )
    probe = os.path.abspath(__file__)
    value = await _run(module, {"sp": site_packages, "probe": probe})
    assert value == {"write_pth": "denied", "read_hostname": "denied", "read_home": "denied", "read_probe": "denied"}
    assert not os.path.exists(os.path.join(site_packages, "evil.pth"))


def test_the_api_process_can_mark_itself_non_dumpable():
    from primer.common.hardening import make_non_dumpable

    code = (
        "import os, subprocess, sys\n"
        "from primer.common.hardening import make_non_dumpable\n"
        "assert make_non_dumpable() is True\n"
        "child = subprocess.run([sys.executable, '-c', "
        "\"import os; open('/proc/%d/environ' % os.getppid(), 'rb').read()\"], capture_output=True, text=True)\n"
        "print('PermissionError' in child.stderr)\n"
    )
    if sys.platform != "linux":
        assert make_non_dumpable() is False
        return
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True", out.stderr


async def test_without_landlock_the_tool_still_runs_and_the_badge_is_not_landlock(monkeypatch, caplog):
    monkeypatch.setattr(runners_mod, "detect_landlock_abi", lambda: 0)
    runner = LocalHardenedRunner(env={})
    assert runner.isolation_level in (IsolationLevel.SECCOMP, IsolationLevel.RLIMIT_ONLY, IsolationLevel.SANDBOX_EXEC)
    value = await _run(REPRESENTATIVE, runner=runner)
    assert value["urandom"] == 8
    with caplog.at_level(logging.WARNING, logger=runners_mod.logger.name):
        runners_mod.warn_if_unconfined(runner.isolation_level)
    assert "Landlock" in caplog.text


def test_no_warning_where_landlock_confines(caplog):
    with caplog.at_level(logging.WARNING, logger=runners_mod.logger.name):
        runners_mod.warn_if_unconfined(IsolationLevel.LANDLOCK)
    assert caplog.text == ""


@pytest.mark.parametrize("landlock", [True, False])
async def test_a_third_party_import_fails_the_same_way_with_or_without_landlock(monkeypatch, landlock):
    """`-I -S` keeps site-packages off sys.path, which is why Landlock grants no read there (and an editable install's .pth would expose the repo)."""
    if not landlock:
        monkeypatch.setattr(runners_mod, "detect_landlock_abi", lambda: 0)
    module = "def run():\n    import pydantic\n    return 1\n"
    resp = await LocalHardenedRunner(env={}).run(_req(module), timeout_seconds=30)
    assert resp.ok is False and resp.error["type"] == "ModuleNotFoundError"
