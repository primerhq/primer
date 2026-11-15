"""Host-signed local authority. Never create a reservation, VM or trust key here."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
import platform
import pwd
import time
import re
from pathlib import Path
import stat
import subprocess
import sys
import uuid
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import MODEL, digest, source_hashes

TRUST_ROOT = Path('/etc/primer-w4-f1/authority.pub')
OWNER_ROOT = Path('/etc/primer-w4-f1/model-owner.pub')
ENDPOINT = 'http://asus-gx10.piranha-ph.ts.net:8000/v1'
SHARED_FS = {'9p', 'virtiofs', 'nfs', 'nfs4', 'cifs', 'ceph', 'fuse.sshfs'}


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def finite(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('finite numeric time/resource required')
    return value


def text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('actual authority/reference is absent')
    return value


def identifier(value) -> str:
    if str(uuid.UUID(value)) != value:
        raise ValueError('canonical VM/run/boot UUID required')
    return value


def sha(value) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('SHA256 required')
    return value


def read_packet(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate authority field')
            result[key] = value
        return result
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError('authority packet exceeds bound')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda x: finite(x))


def guest_trust_key(*, owner=False) -> bytes:
    """Fixed host-installed root, not an argument or key chosen by the guest packet."""
    root = OWNER_ROOT if owner else TRUST_ROOT
    for p in [root, *root.parents]:
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
            raise ValueError('guest trust root/ancestors must be root-owned and non-writable')
    if not stat.S_ISREG(root.lstat().st_mode):
        raise ValueError('guest trust root must be a regular file')
    key = root.read_bytes()
    if len(key) != 32:
        raise ValueError('raw Ed25519 host public key required')
    return key


def validate_vm_xml(vm: dict, xml: str) -> None:
    """Closed headless domain policy: every host-backed source is explicitly owned."""
    if len(xml.encode()) > 131072 or digest(xml.encode()) != sha(vm['xml_sha256']):
        raise ValueError('actual domain XML differs from host authority')
    root = ET.fromstring(xml)
    if root.tag != 'domain' or root.get('type') != 'kvm' or root.findtext('uuid') != identifier(vm['uuid']):
        raise ValueError('owned KVM domain identity differs')
    if root.findtext('name') != 'w4-f1-' + vm['uuid'] or root.findtext('name') != vm['name']:
        raise ValueError('domain is not the exact owned F1 guest')
    root_tags = {'name', 'uuid', 'title', 'description', 'metadata', 'memory', 'currentMemory', 'vcpu',
                 'os', 'features', 'cpu', 'clock', 'on_poweroff', 'on_reboot', 'on_crash', 'pm', 'resource', 'devices', 'seclabel'}
    if any(node.tag not in root_tags for node in root):
        raise ValueError('unreviewed domain root capability')
    metadata = root.find('metadata');inert = set(metadata.iter()) if metadata is not None else set()
    if any('}' in node.tag for node in root.iter() if node not in inert):
        raise ValueError('executable domain namespace extensions forbidden')
    if int(root.findtext('vcpu', '0')) != 2 or vm['vcpus'] != 2:
        raise ValueError('guest CPU bound differs')
    for tag in ('memory', 'currentMemory'):
        memory = root.find(tag)
        if tag == 'memory' and memory is None:
            raise ValueError('guest RAM absent')
        if memory is not None and (memory.get('unit') != 'KiB' or int(memory.text) != 6 * 1024 * 1024):
            raise ValueError('guest RAM bound differs')
    if vm['memory_mib'] != 6144 or finite(vm['disk_cap_bytes']) != 20 * 1024**3:
        raise ValueError('guest RAM/disk cap differs')
    owned = Path(vm['owned_host_root'])
    if owned != Path('/var/tmp') / vm['name'] or Path(vm['disk_path']) != owned / 'guest.qcow2' or Path(vm['image_path']) != owned / 'base.qcow2':
        raise ValueError('all VM disk/image paths must be the exact private owned files')
    sha(vm['image_sha256'])
    seed = vm['seed']
    if seed is not None and (set(seed) != {'path', 'sha256'} or Path(seed['path']) != owned / 'seed.iso'):
        raise ValueError('only the private read-only seed ISO is permitted')
    if seed is not None:sha(seed['sha256'])
    os_node = root.find('os')
    if os_node is not None and any(n.tag not in {'type', 'boot', 'bootmenu'} for n in os_node):
        raise ValueError('host kernel/loader/init/media paths forbidden')
    devices = root.find('devices')
    if len(root.findall('devices')) != 1:
        raise ValueError('exactly one closed device tree required')
    if devices is None or any(n.tag not in {'emulator', 'disk', 'controller', 'interface', 'input', 'video', 'memballoon', 'rng'} for n in devices):
        raise ValueError('unreviewed device/channel/socket/character backend')
    if len(devices.findall('emulator'))>1 or any((n.text or '').strip()!='/usr/bin/qemu-system-x86_64' or list(n) for n in devices.findall('emulator')):
        raise ValueError('only the reviewed system QEMU executable is permitted')
    for rng in devices.findall('rng'):
        backends=rng.findall('backend')
        if len(backends)!=1 or backends[0].attrib!={'model':'random'} or (backends[0].text or '').strip()!='/dev/urandom' or list(backends[0]):
            raise ValueError('only the system random device is permitted')
    paths = {};sources = set()
    disks = devices.findall('disk')
    if len(disks) != (2 if seed is not None else 1):
        raise ValueError('additional disk/block/CDROM/LUN media forbidden')
    primaries = [d for d in disks if d.get('device') == 'disk']
    if len(primaries) != 1:raise ValueError('one private primary disk required')
    for disk in disks:
        if any(n.tag not in {'driver','source','backingStore','target','address','alias','readonly','boot'} for n in disk):
            raise ValueError('unreviewed disk secret/backend capability')
        driver, source = disk.find('driver'), disk.find('source')
        primary = disk is primaries[0]
        expected = vm['disk_path'] if primary else seed['path']
        if (disk.get('type') != 'file' or source is None or (source.get('file') != expected or set(source.attrib)-{'file','index'})
                or driver is None or driver.get('type') != ('qcow2' if primary else 'raw')
                or (not primary and (disk.get('device') != 'cdrom' or disk.find('readonly') is None))):
            raise ValueError('unapproved media source/type or writable seed')
        sources.add(source);paths[(source, 'file')] = expected
        backing = disk.find('backingStore')
        if backing is not None and list(backing):
            src = backing.find('source')
            nested = backing.find('backingStore')
            if (not primary or backing.get('type') != 'file' or src is None or (src.get('file') != vm['image_path'] or set(src.attrib)-{'file','index'})
                    or (nested is not None and list(nested))):
                raise ValueError('unreviewed backing source/chain')
            sources.add(src);paths[(src, 'file')] = vm['image_path']
    interfaces = devices.findall('interface')
    if len(interfaces) != 1 or interfaces[0].get('type') != 'network':
        raise ValueError('only the reviewed private NAT attachment is permitted')
    interface = interfaces[0];source = interface.find('source')
    if (source is None or source.get('network') != 'default' or set(source.attrib)-{'network','portid','bridge'}
            or source.get('bridge','virbr0') != 'virbr0'
            or any(n.tag not in {'mac','source','model','address','alias','target','link','boot'} for n in interface)):
        raise ValueError('unreviewed network/socket backend')
    sources.add(source)
    for node in root.iter():
        if node in inert:continue
        if node.tag == 'source' and node not in sources:raise ValueError('unallowlisted host-backed source')
        for key,value in node.attrib.items():
            if value.startswith('/') and paths.get((node,key)) != value:
                raise ValueError('unallowlisted host file/device/socket path')
        value = (node.text or '').strip()
        if value.startswith('/') and not ((node.tag == 'emulator' and value == '/usr/bin/qemu-system-x86_64')
                                          or (node.tag == 'backend' and node.get('model') == 'random' and value == '/dev/urandom')
                                          or (node.tag == 'partition' and value == '/machine')):
            raise ValueError('unallowlisted host-backed text path')


def validate_authority(packet: dict, key: bytes, owner_key: bytes, *, source: str, hashes: dict, now: float) -> dict:
    """Authenticate an operator's factual hold receipt; idle/client-lock claims are not authority."""
    finite(now)
    if key == owner_key:
        raise ValueError('host execution and model-owner trust roles must be distinct')
    if set(packet) != {'payload', 'signature'}:
        raise ValueError('signed host authority envelope required')
    p = packet['payload']
    if set(p) != {'mode', 'run_id', 'source_sha', 'driver_hashes', 'lead', 'issued_at', 'starts_at', 'expires_at', 'vm', 'reservation', 'guest_network_authorization', 'preboot', 'preboot_sha256'}:
        raise ValueError('unknown/missing local authority fields')
    if set(p['lead']) != {'principal', 'authorization_reference'}:
        raise ValueError('unknown/missing lead authority')
    Ed25519PublicKey.from_public_bytes(key).verify(bytes.fromhex(packet['signature']), canonical(p))
    if p['mode'] != 'local-vm' or p['source_sha'] != source or p['driver_hashes'] != hashes:
        raise ValueError('local mode/exact reviewed source differs')
    identifier(p['run_id']); text(p['lead']['principal']); text(p['lead']['authorization_reference'])
    start, end, issued = (finite(p[k]) for k in ('starts_at', 'expires_at', 'issued_at'))
    if not start <= now <= end or not issued <= now <= issued + 900 or not now + 900 <= end <= start + 1800:
        raise ValueError('finite fresh lead window must cover the full bounded attempt')
    vm = p['vm']
    if set(vm) != {'uuid', 'name', 'xml', 'xml_sha256', 'vcpus', 'memory_mib', 'disk_cap_bytes', 'owned_host_root', 'disk_path', 'image_path', 'image_sha256', 'boot_id', 'machine_id', 'seed', 'host_process', 'booted_at', 'clock_probe'}:
        raise ValueError('unknown/missing host VM facts')
    validate_vm_xml(vm, vm['xml'])
    identifier(vm['boot_id'])
    if len(vm['machine_id']) != 32 or any(c not in '0123456789abcdef' for c in vm['machine_id']):
        raise ValueError('actual guest machine id required')
    text(p['guest_network_authorization'])
    validate_owner_reservation(p['reservation'], owner_key, source=source, run_id=p['run_id'],
                               vm_uuid=vm['uuid'], start=start, end=end)
    validate_execution_chain(p, key, owner_key)
    return p


def validate_owner_reservation(reservation_packet: dict, owner_key: bytes, *, source: str, run_id: str,
                               vm_uuid: str, start: float, end: float) -> dict:
    """Pure host pre-boot validation; this never creates or infers a reservation."""
    identifier(run_id); identifier(vm_uuid); finite(start); finite(end)
    if end <= start:
        raise ValueError('bounded owner window required')
    if set(reservation_packet) != {'payload', 'signature'}:
        raise ValueError('separately signed actual model-owner reservation required')
    r = reservation_packet['payload']
    Ed25519PublicKey.from_public_bytes(owner_key).verify(bytes.fromhex(reservation_packet['signature']), canonical(r))
    if set(r) != {'id', 'owner', 'owner_reference', 'scope', 'mechanism', 'endpoint', 'model', 'exclusive_consumer', 'source_sha', 'vm_uuid', 'starts_at', 'expires_at', 'consumer_inventory', 'acknowledgements'}:
        raise ValueError('unknown/missing model-owner reservation facts')
    if r['source_sha'] != source or r['vm_uuid'] != vm_uuid:
        raise ValueError('model-owner source/VM reservation differs')
    if (r['scope'] != 'all-consumers' or r['mechanism'] != 'owner-coordinated-holds'
            or r['endpoint'] != ENDPOINT or r['model'] != MODEL or r['exclusive_consumer'] != run_id):
        raise ValueError('actual model-wide owner-coordinated holds required')
    text(r['id']); text(r['owner']); text(r['owner_reference'])
    if not finite(r['starts_at']) <= start or finite(r['expires_at']) < end:
        raise ValueError('owner reservation does not cover the lead window')
    inventory = r['consumer_inventory']
    if not inventory or len(inventory) != len(set(inventory)) or not all(text(c) for c in inventory):
        raise ValueError('owner-reviewed complete consumer inventory required')
    acknowledgements = r['acknowledgements']
    if len(acknowledgements) != len(inventory) or {a['consumer'] for a in acknowledgements} != set(inventory):
        raise ValueError('every actual consumer needs an owner hold acknowledgement')
    for a in acknowledgements:
        if set(a) != {'consumer', 'owner', 'reference', 'evidence_name', 'evidence_sha256', 'held_from', 'held_until'}:
            raise ValueError('unknown/missing original hold acknowledgement fields')
        text(a['owner']); text(a['reference']); sha(a['evidence_sha256'])
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', a['evidence_name']) or a['evidence_name'] in {'.', '..'}:
            raise ValueError('named actual hold evidence required')
        if not finite(a['held_from']) <= start or finite(a['held_until']) < end:
            raise ValueError('consumer hold does not cover the authorized window')
    return r


PREFLIGHT_FIELDS = {'mode','source_sha','driver_hashes','run_id','vm_uuid','checked_at','starts_at','expires_at',
                    'owner_key_sha256','host_key_sha256','signed_reservation_sha256','original_host_holds',
                    'does_not_establish_or_infer_a_hold'}


def validate_execution_chain(p: dict, host_key: bytes, owner_key: bytes):
    sealed=p['preboot']
    if set(sealed) != {'payload','signature'}:raise ValueError('actual signed pre-boot phase required')
    pre=sealed['payload']
    Ed25519PublicKey.from_public_bytes(host_key).verify(bytes.fromhex(sealed['signature']),canonical(pre))
    if set(pre) != PREFLIGHT_FIELDS or pre['mode'] != 'local-vm-owner-preflight':raise ValueError('closed actual preflight schema required')
    expected={'source_sha':p['source_sha'],'driver_hashes':p['driver_hashes'],'run_id':p['run_id'],
              'vm_uuid':p['vm']['uuid'],'starts_at':p['starts_at'],'expires_at':p['expires_at'],
              'owner_key_sha256':digest(owner_key),'host_key_sha256':digest(host_key),
              'signed_reservation_sha256':digest(canonical(p['reservation'])),'does_not_establish_or_infer_a_hold':True}
    if any(pre[k]!=v for k,v in expected.items()):raise ValueError('preflight source/run/VM/owner/window differs')
    if sha(p['preboot_sha256']) != digest(canonical(sealed)+b'\n'):raise ValueError('original preflight bytes differ')
    acknowledgements=p['reservation']['payload']['acknowledgements']
    if pre['original_host_holds'] != [{'consumer':a['consumer'],'name':a['evidence_name'],'sha256':a['evidence_sha256']} for a in acknowledgements]:
        raise ValueError('original acknowledged holds are disconnected')
    vm=p['vm'];process=vm['host_process']
    if set(process) != {'pid','starttime','started_at','cmdline_sha256'} or isinstance(process['pid'],bool) or not isinstance(process['pid'],int) or process['pid']<=1 or not str(process['starttime']).isdigit():
        raise ValueError('actual host kernel VM identity required')
    sha(process['cmdline_sha256']);started=finite(process['started_at']);booted=finite(vm['booted_at'])
    if not p['starts_at'] <= finite(pre['checked_at']) <= started-2 or not started-2 <= booted <= started+180:
        raise ValueError('owner preflight must precede actual VM/kernel boot')
    clock=vm['clock_probe']
    if set(clock) != {'host_before','guest_time','host_after'}:raise ValueError('actual host/guest clock probe required')
    before,guest,after=(finite(clock[k]) for k in ('host_before','guest_time','host_after'))
    if not started <= before <= after <= p['issued_at'] or after-before>2 or abs(guest-(before+after)/2)>2 or guest<booted:
        raise ValueError('host/guest clock skew or observation chronology differs')


def observe_guest() -> dict:
    def read(path): return Path(path).read_text().strip()
    virtualization = subprocess.check_output(['systemd-detect-virt', '--vm'], text=True, timeout=5).strip()
    filesystems = sorted({line.split(' - ', 1)[1].split()[0] for line in read('/proc/self/mountinfo').splitlines()})
    try:
        machine_uuid = read('/sys/class/dmi/id/product_uuid')
    except PermissionError:
        # Guest-only sudoers may grant exactly this read, never an elevated probe/app.
        machine_uuid = subprocess.check_output(['sudo', '-n', '/usr/bin/cat', '/sys/class/dmi/id/product_uuid'], text=True, timeout=5).strip()
    os_release = platform.freedesktop_os_release()
    return {'clock_ticks':os.sysconf('SC_CLK_TCK'),'observed_at':time.time(),'booted_at':next(int(line.split()[1]) for line in read('/proc/stat').splitlines() if line.startswith('btime ')),'os_id': os_release['ID'], 'os_version': os_release['VERSION_ID'], 'uuid': machine_uuid.lower(), 'boot_id': read('/proc/sys/kernel/random/boot_id'),
            'machine_id': read('/etc/machine-id'), 'virtualization': virtualization,
            'python': list(sys.version_info[:3]), 'real_executable': os.readlink('/proc/self/exe'),
            'cpus': os.cpu_count(), 'filesystems': filesystems}


def validate_guest(p: dict, observation: dict):
    vm = p['vm']
    if set(observation) != {'clock_ticks','observed_at','booted_at','os_id', 'os_version', 'uuid', 'boot_id', 'machine_id', 'virtualization', 'python', 'real_executable', 'cpus', 'filesystems'}:
        raise ValueError('unknown/missing actual guest observations')
    if any(observation[k] != vm[k] for k in ('uuid', 'boot_id', 'machine_id','booted_at')):
        raise ValueError('actual guest identity differs from host authority')
    observed=finite(observation['observed_at'])
    if not vm['clock_probe']['guest_time']-2 <= observed <= vm['clock_probe']['guest_time']+900:
        raise ValueError('actual guest clock probe is stale/disconnected')
    if (observation['os_id'] != 'ubuntu' or observation['os_version'] != '24.04' or observation['virtualization'] != 'kvm' or observation['python'][:2] != [3, 12]
            or observation['real_executable'] != '/usr/bin/python3.12' or observation['cpus'] != 2
            or set(observation['filesystems']) & SHARED_FS):
        raise ValueError('isolated native guest execution prerequisites absent')


def source_state(repo: Path) -> str:
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=normal'], cwd=repo):
        raise ValueError('local VM source checkout must be clean')
    return head


def private_host_file(path: Path, owned: Path) -> dict:
    """Verify real canonical file ownership, not merely a signed path string."""
    allowed = {0, os.geteuid()}
    try:allowed.add(pwd.getpwnam('libvirt-qemu').pw_uid)
    except KeyError:pass
    root = owned.lstat();st = path.lstat()
    if (owned.resolve() != owned or path.resolve() != path or path.parent != owned
            or not stat.S_ISDIR(root.st_mode) or root.st_uid not in allowed or stat.S_IMODE(root.st_mode) & 0o027
            or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in allowed or stat.S_IMODE(st.st_mode) & 0o022):
        raise ValueError('owned host file/root identity or permissions unsafe')
    return {'path':str(path),'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode),'device':st.st_dev,'inode':st.st_ino}


def observe_host_process(vm: dict) -> dict:
    """Bind the real libvirt PID to its UUID/name and kernel start clock, read-only."""
    pid_path = Path('/run/libvirt/qemu') / (vm['name'] + '.pid')
    pid = int(pid_path.read_text().strip())
    if pid <= 1:raise ValueError('actual owned QEMU PID absent')
    proc = Path('/proc') / str(pid)
    fields = (proc/'stat').read_text().rsplit(')',1)[1].split()
    argv = (proc/'cmdline').read_bytes().split(b'\0')[:-1]
    try:actual_uuid=argv[argv.index(b'-uuid')+1].decode();name=argv[argv.index(b'-name')+1].decode()
    except (ValueError,IndexError,UnicodeDecodeError) as exc:raise ValueError('QEMU process identity absent') from exc
    if actual_uuid != vm['uuid'] or name not in {vm['name'],'guest='+vm['name']} and not name.startswith('guest='+vm['name']+','):
        raise ValueError('kernel PID does not belong to the actual domain')
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    ticks = fields[19]
    return {'pid':pid,'starttime':ticks,'started_at':boot+int(ticks)/os.sysconf('SC_CLK_TCK'),
            'cmdline_sha256':digest(b'\0'.join(argv))}


def host_vm_observation(vm: dict) -> dict:
    """Observe real host paths/process/XML/image without starting or stopping anything."""
    identifier(vm['uuid'])
    prefix = ['virsh', '--connect', 'qemu:///system']
    state = subprocess.check_output([*prefix, 'domstate', vm['uuid']], text=True, timeout=5).strip()
    if state != 'running':raise ValueError('owned VM is not running for final host verification')
    xml = subprocess.check_output([*prefix, 'dumpxml', vm['uuid']], text=True, timeout=5)
    validate_vm_xml(vm, xml)
    owned=Path(vm['owned_host_root'])
    paths=[Path(vm['disk_path']),Path(vm['image_path'])]
    if vm['seed'] is not None:paths.append(Path(vm['seed']['path']))
    identities=[private_host_file(path,owned) for path in paths]
    def file_hash(path):
        h=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
        return h.hexdigest()
    image=file_hash(Path(vm['image_path']))
    if image != vm['image_sha256']:raise ValueError('actual host image differs')
    seed_hash=None if vm['seed'] is None else file_hash(Path(vm['seed']['path']))
    if vm['seed'] is not None and seed_hash != vm['seed']['sha256']:raise ValueError('actual private seed digest differs')
    info=json.loads(subprocess.check_output(['qemu-img','info','--force-share','--output=json',vm['disk_path']],text=True,timeout=5))
    if info['format'] != 'qcow2' or info['virtual-size'] != vm['disk_cap_bytes'] or info.get('full-backing-filename') != vm['image_path']:
        raise ValueError('actual owned disk cap/backing differs')
    process=observe_host_process(vm)
    if process != vm['host_process']:raise ValueError('VM kernel process changed/restarted')
    return {'uuid':vm['uuid'],'xml_sha256':digest(xml.encode()),'image_sha256':image,'seed_sha256':seed_hash,
            'disk_cap_bytes':info['virtual-size'],'state':state,'host_process':process,'host_paths':identities,'observed_at':time.time()}


def verify_hold_files(reservation: dict, directory: Path) -> list[dict]:
    """Host-controlled original acknowledgements are required, not guest reconstructions."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('trusted host hold-evidence directory required')
    result = []
    for acknowledgement in reservation['acknowledgements']:
        path = directory / acknowledgement['evidence_name']
        if path.is_symlink() or path.parent.resolve() != directory.resolve() or not path.is_file():
            raise ValueError('missing/unsafe original owner hold evidence')
        data = path.read_bytes()
        if not data or len(data) > 262144 or digest(data) != acknowledgement['evidence_sha256']:
            raise ValueError('actual original hold evidence digest differs')
        result.append({'consumer': acknowledgement['consumer'], 'name': path.name, 'sha256': digest(data)})
    return result


def read_host_signing_key(path: Path) -> bytes:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid not in {0, os.geteuid()} or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError('host-only private signing key must be a private owned regular file')
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('raw host Ed25519 private key required')
    return key
