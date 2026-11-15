"""Synthetic local authority wiring, never VM/model/PG/kernel operations."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from scripts.e2e.w4_f1 import contracts as c, local_authority as a, local_vm as v, local_verify as gate, probe
from tests.tooling.test_w4_f1_contracts import make_trace, write_native_fixture, native_fixture_rows

HOST = Ed25519PrivateKey.from_private_bytes(b'\x01' * 32)
OWNER = Ed25519PrivateKey.from_private_bytes(b'\x02' * 32)
HOST_PUB = HOST.public_key().public_bytes_raw()
OWNER_PUB = OWNER.public_key().public_bytes_raw()
RUN = '11111111-1111-4111-8111-111111111111'
VM = '22222222-2222-4222-8222-222222222222'
BOOT = '33333333-3333-4333-8333-333333333333'
NOW = datetime(2026,11,15,tzinfo=timezone.utc).timestamp()
SOURCE = 'a' * 40
HASHES = {'synthetic-unit-fixture.py': 'b' * 64}
HOLD = b'SYNTHETIC UNIT HOLD ONLY, NOT AN ACTUAL RESERVATION'


def signed(payload, key=HOST):
    return {'payload': deepcopy(payload), 'signature': key.sign(a.canonical(payload)).hex()}


def payload():
    name = 'w4-f1-' + VM
    xml = f'<domain type="kvm"><name>{name}</name><uuid>{VM}</uuid><vcpu>2</vcpu><memory unit="KiB">6291456</memory><devices><disk type="file" device="disk"><driver type="qcow2"/><source file="/var/tmp/{name}/guest.qcow2"/></disk><interface type="network"><source network="default"/></interface></devices></domain>'
    reservation = {'id': 'synthetic-hold', 'owner': 'synthetic-owner', 'owner_reference': 'synthetic-reviewed-ack',
                   'scope': 'all-consumers', 'mechanism': 'owner-coordinated-holds', 'endpoint': a.ENDPOINT, 'model': c.MODEL,
                   'exclusive_consumer': RUN, 'source_sha': SOURCE, 'vm_uuid': VM, 'starts_at': NOW - 50, 'expires_at': NOW + 1700,
                   'consumer_inventory': ['synthetic-consumer'], 'acknowledgements': [
                       {'consumer': 'synthetic-consumer', 'owner': 'synthetic-consumer-owner', 'reference': 'synthetic-ack',
                        'held_from': NOW - 50, 'held_until': NOW + 1700, 'evidence_name': 'hold.txt', 'evidence_sha256': c.digest(HOLD)}]}
    p = {'mode': 'local-vm', 'run_id': RUN, 'source_sha': SOURCE, 'driver_hashes': HASHES,
            'lead': {'principal': 'synthetic-lead', 'authorization_reference': 'synthetic-local-only-window'},
            'issued_at': NOW - 2.5, 'starts_at': NOW - 40, 'expires_at': NOW + 1500,
            'vm': {'uuid': VM, 'name': name, 'xml': xml, 'xml_sha256': c.digest(xml.encode()),
                   'vcpus': 2, 'memory_mib': 6144, 'disk_cap_bytes': 20 * 1024**3,
                   'owned_host_root': '/var/tmp/' + name, 'disk_path': '/var/tmp/' + name + '/guest.qcow2',
                   'image_path': '/var/tmp/' + name + '/base.qcow2', 'image_sha256': c.digest(b'SYNTHETIC IMAGE'),
                   'boot_id': BOOT, 'machine_id': '4' * 32,'seed':None,
                   'host_process':{'pid':4321,'starttime':'9000','started_at':NOW-25,'cmdline_sha256':c.digest(b'SYNTHETIC QEMU')},
                   'booted_at':NOW-24,'clock_probe':{'host_before':NOW-3.1,'guest_time':NOW-3,'host_after':NOW-2.9}},
            'reservation': signed(reservation, OWNER), 'guest_network_authorization': 'synthetic-guest-network-authorization'}
    pre={'mode':'local-vm-owner-preflight','source_sha':SOURCE,'driver_hashes':HASHES,'run_id':RUN,'vm_uuid':VM,
         'checked_at':NOW-30,'starts_at':p['starts_at'],'expires_at':p['expires_at'],'host_key_sha256':c.digest(HOST_PUB),
         'owner_key_sha256':c.digest(OWNER_PUB),'signed_reservation_sha256':c.digest(a.canonical(p['reservation'])),
         'original_host_holds':[{'consumer':'synthetic-consumer','name':'hold.txt','sha256':c.digest(HOLD)}],
         'does_not_establish_or_infer_a_hold':True}
    p['preboot']=signed(pre);p['preboot_sha256']=c.digest(a.canonical(p['preboot'])+b'\n')
    return p


def observation():
    return {'observed_at':NOW+0.2,'booted_at':NOW-24,'clock_ticks':100,'os_id':'ubuntu','os_version':'24.04','uuid': VM, 'boot_id': BOOT, 'machine_id': '4' * 32, 'virtualization': 'kvm',
            'python': [3, 12, 3], 'real_executable': '/usr/bin/python3.12', 'cpus': 2, 'filesystems': ['ext4', 'proc', 'tmpfs']}


def local_trace():
    t = make_trace();p = payload();r = p['reservation']['payload']
    raw = json.dumps({'data': [{'id': c.MODEL}]}).encode()
    t.update(authority_mode='local-vm', driver_hashes=HASHES, run={'mode': 'local-vm', 'run_id': RUN, 'vm_uuid': VM},
             reservation={'id': r['id'], 'owner': r['owner'], 'checked_at': NOW, 'expires': r['expires_at'], 'endpoint': a.ENDPOINT},
             local_vm={'authority': signed(p), 'guest': observation(), 'key_sha256': c.digest(HOST_PUB),
                       'owner_key_sha256': c.digest(OWNER_PUB),'timeline':{'started_at':NOW,'finished_at':NOW+70}, 'network': {'body_hex': raw.hex(), 'sha256': c.digest(raw),
                       'endpoint': a.ENDPOINT, 'status': 200, 'checked_at': NOW + 0.3}})
    def iso(offset):return datetime.fromtimestamp(NOW+offset,timezone.utc).isoformat()
    moments={'idle1':16,'compacted':19,'before_kill':26,'dead_live':27,'dead_expired':56,'reclaimed':58.2,'idle2':65}
    for name,offset in moments.items():t['snapshots'][name]['observed_at']=iso(offset)
    for name in ('before_kill','dead_live','dead_expired'):
        t['snapshots'][name]['lease'].update(claimed_at=iso(23.5),last_heartbeat_at=iso(25),expires_at=iso(55))
    t['snapshots']['reclaimed']['lease'].update(claimed_at=iso(58),last_heartbeat_at=iso(58),expires_at=iso(88))
    for watch,offset in zip(t['watches'],[8,22],strict=True):watch['observed_at']=iso(offset)
    spans=[(1,2),(6,7),(10,11),(12,13),(14,15),(17,18),(20,21),(24,27),(58.5,60),(61,62),(63,64)]
    for packet,(start,end) in zip(t['packets'],spans,strict=True):packet.update(started_at=NOW+start,completed_at=NOW+end,elapsed_seconds=end-start)
    call_times={'call-1-0':(7,9),'call-1-1':(11,11.1),'call-1-2':(13,13.1),'call-2-0':(21,23),'call-2-1':(60,60.1),'call-2-2':(62,62.1)}
    done=0
    for row in t['disk_records']:
        kind=row['kind'];fields=row['payload']
        if kind in {'tool_call','tool_result'}:offset=call_times[fields['id'] if kind=='tool_call' else fields['call_id']][kind=='tool_result']
        elif kind=='compaction_marker':offset=18.2
        else:offset=[15.2,64.2][done];done+=1
        row['created_at']=iso(offset)
    t['disk_records']=probe.normalized_records(t['disk_records']);t['rest_records']=deepcopy(t['disk_records']);t['elapsed_seconds']=70
    for name,offset in [('old_process',5),('new_process',57)]:
        process=t[name];process['starttime']=str(int((24+offset)*100))
        next(r for r in t['runtime'] if r['pid']==process['pid'])['starttime']=process['starttime']
    t['cleanup']['native_evidence']=native_fixture_rows(t)
    return t


def actual_host_observation(vm):
    paths=[vm['disk_path'],vm['image_path']]+([] if vm['seed'] is None else [vm['seed']['path']])
    return {'uuid':VM,'xml_sha256':vm['xml_sha256'],'image_sha256':vm['image_sha256'],
            'seed_sha256':None if vm['seed'] is None else vm['seed']['sha256'],'disk_cap_bytes':20*1024**3,
            'state':'running','host_process':vm['host_process'],'observed_at':NOW+71,
            'host_paths':[{'path':p,'uid':1000,'gid':1000,'mode':0o600,'device':1,'inode':i+1} for i,p in enumerate(paths)]}


def prepare_artifact(tmp_path,monkeypatch):
    t=local_trace();write_native_fixture(tmp_path,t);raw=json.dumps(t).encode();(tmp_path/'trace.json').write_bytes(raw)
    (tmp_path/'local-accepted.json').write_text(json.dumps({'source_sha':SOURCE,'trace_sha256':c.digest(raw)}))
    (tmp_path/'owner-preboot.json').write_bytes(a.canonical(t['local_vm']['authority']['payload']['preboot'])+b'\n')
    (tmp_path/'hold.txt').write_bytes(HOLD);junit=tmp_path/'local-junit.xml'
    junit.write_text(f'<testsuite><testcase classname="scripts.e2e.w4_f1.local_acceptance" name="{c.CASE}"/></testsuite>')
    monkeypatch.setattr(gate,'source_state',lambda repo:SOURCE);monkeypatch.setattr(gate,'source_hashes',lambda repo:HASHES)
    monkeypatch.setattr(gate.time,'time',lambda:NOW+72);monkeypatch.setattr(gate,'host_vm_observation',actual_host_observation)
    return t,raw,junit


def validate(p):
    return a.validate_authority(signed(p), HOST_PUB, OWNER_PUB, source=SOURCE, hashes=HASHES, now=NOW)


def test_distinct_signed_authorities_and_connected_original_lifecycle():
    validate(payload());a.validate_guest(payload(), observation())
    v.require_local_evidence(local_trace(), SOURCE, HOST_PUB, OWNER_PUB)
    with pytest.raises(ValueError, match='CI'):
        c.require_evidence(local_trace(), SOURCE)
    with pytest.raises((KeyError, ValueError)):
        v.require_local_evidence(make_trace(), SOURCE, HOST_PUB, OWNER_PUB)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(mode='github-actions'), lambda p: p.update(source_sha='e' * 40),
    lambda p: p.update(driver_hashes={}), lambda p: p.update(unreviewed=True),
    lambda p: p.update(expires_at=NOW + 800), lambda p: p.update(issued_at=NOW - 1000),
    lambda p: p.update(starts_at=NOW + 10), lambda p: p.update(expires_at=float('inf')),
    lambda p: p.update(issued_at=float('nan')), lambda p: p['lead'].update(authorization_reference=''),
    lambda p: p['vm'].update(uuid=str(uuid.uuid4())), lambda p: p['vm'].update(vcpus=16),
    lambda p: p['vm'].update(disk_path='/shared/guest.qcow2'), lambda p: p['vm'].update(xml_sha256='0' * 64),
])
def test_wrong_mode_source_window_VM_or_unknown_fields_reject(mutation):
    p = payload();mutation(p)
    with pytest.raises((ValueError, InvalidSignature, AssertionError)):
        validate(p)


@pytest.mark.parametrize('field,value', [('scope', 'this-client'), ('mechanism', 'idle-metrics'),
    ('owner_reference', ''), ('endpoint', 'http://127.0.0.1:8000/v1'), ('model', 'other'),
    ('consumer_inventory', []), ('acknowledgements', []), ('expires_at', NOW + 100),
    ('source_sha', 'c' * 40), ('exclusive_consumer', str(uuid.uuid4())), ('extra', True)])
def test_even_validly_signed_owner_receipt_requires_actual_complete_bounded_hold(field, value):
    p = payload();r = p['reservation']['payload'];r[field] = value;p['reservation'] = signed(r, OWNER)
    with pytest.raises((ValueError, InvalidSignature, KeyError)):
        validate(p)


def test_host_cannot_manufacture_model_owner_authority():
    p = payload();p['reservation'] = signed(p['reservation']['payload'], HOST)
    with pytest.raises(InvalidSignature):validate(p)
    with pytest.raises(ValueError, match='distinct'):
        a.validate_authority(signed(payload()), OWNER_PUB, OWNER_PUB, source=SOURCE, hashes=HASHES, now=NOW)


@pytest.mark.parametrize('field,value', [('uuid', RUN), ('boot_id', RUN), ('machine_id', 'f' * 32),
    ('os_id','debian'), ('os_version','22.04'), ('virtualization', 'none'), ('python', [3, 14, 4]), ('cpus', 16), ('filesystems', ['ext4', 'virtiofs']),
    ('real_executable', '/host/python'), ('unreviewed', True)])
def test_actual_direct_host_other_guest_or_shared_mount_refuses(field, value):
    o = observation();o[field] = value
    with pytest.raises(ValueError):a.validate_guest(payload(), o)


@pytest.mark.parametrize('field,value', [('snapshots', {}), ('packets', []), ('outcome', 'running')])
def test_local_authority_cannot_replace_any_original_raw_oracle(field, value):
    t = local_trace();t[field] = value
    with pytest.raises((AssertionError, ValueError, KeyError)):
        v.require_local_evidence(t, SOURCE, HOST_PUB, OWNER_PUB)


@pytest.mark.parametrize('name', ['GITHUB_ACTIONS', 'GITHUB_SHA', 'RUNNER_NAME', 'W4_F1_CONTROL_PLANE_PROOF', 'W4_F1_EXECUTION_PROOF'])
def test_no_fabricated_CI_receipts_in_local_entry(name):
    with pytest.raises(ValueError):v.local_context({name: 'fabricated'})


async def test_invalid_local_context_refuses_before_source_network_DB_output_or_process(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(v.probe.subprocess, 'check_output', lambda *a, **kw: pytest.fail('no source/process IO before context guard'))
    monkeypatch.setattr(v.httpx, 'AsyncClient', lambda *a, **kw: pytest.fail('no network'))
    with pytest.raises(ValueError):await v.run(tmp_path / 'output', tmp_path / 'authority', 'unused')
    assert not (tmp_path / 'output').exists()


def test_host_original_hold_files_required_and_bound(tmp_path):
    r = payload()['reservation']['payload'];p = tmp_path / 'hold.txt'
    with pytest.raises(ValueError):a.verify_hold_files(r, tmp_path)
    p.write_bytes(HOLD);assert a.verify_hold_files(r, tmp_path)[0]['sha256'] == c.digest(HOLD)
    p.write_bytes(b'changed')
    with pytest.raises(ValueError):a.verify_hold_files(r, tmp_path)
    p.unlink();p.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError):a.verify_hold_files(r, tmp_path)


def test_host_final_gate_reuses_actual_native_bytes_and_distinct_named_JUnit(tmp_path, monkeypatch):
    t,raw,junit=prepare_artifact(tmp_path,monkeypatch)
    calls=[];monkeypatch.setattr(gate,'host_vm_observation',lambda vm:calls.append(vm['uuid']) or actual_host_observation(vm))
    def check():return gate.verify(tmp_path, SOURCE, junit, host_key=HOST_PUB, host_key_sha256=c.digest(HOST_PUB),
                                  owner_key=OWNER_PUB, owner_key_sha256=c.digest(OWNER_PUB), hold_evidence=tmp_path)
    assert check()['mode']=='local-vm' and calls==[VM]
    junit.write_text(f'<testsuite><testcase classname="scripts.e2e.w4_f1.local_acceptance" name="{c.CASE}"/><testcase classname="other" name="unbounded"/></testsuite>')
    with pytest.raises(ValueError,match='single'):check()
    junit.write_text(f'<testsuite><testcase classname="scripts.e2e.w4_f1.acceptance" name="{c.CASE}"/></testsuite>')
    with pytest.raises(ValueError,match='execute'):check()
    (tmp_path / 'trace.json').write_bytes(raw+b' ')
    with pytest.raises(ValueError,match='receipt'):check()


def test_host_observer_reads_actual_domain_image_and_disk_without_starting_anything(tmp_path, monkeypatch):
    vm = payload()['vm'];image = tmp_path / 'image';image.write_bytes(b'SYNTHETIC IMAGE')
    calls=[];original_open=Path.open
    monkeypatch.setattr(a.Path,'open',lambda path,*args,**kw:original_open(image if str(path)==vm['image_path'] else path,*args,**kw))
    monkeypatch.setattr(a,'private_host_file',lambda path,root:{'path':str(path),'uid':1000,'gid':1000,'mode':0o600,'device':1,'inode':1})
    monkeypatch.setattr(a,'observe_host_process',lambda value:vm['host_process'])
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0]=='virsh' and argv[3]=='domstate':return 'running\n'
        if argv[0]=='virsh' and argv[3]=='dumpxml':return vm['xml']
        if argv[:2]==['qemu-img','info']:
            return json.dumps({'format':'qcow2','virtual-size':20*1024**3,'full-backing-filename':vm['image_path']})
        pytest.fail('unexpected provisioning/process command')
    monkeypatch.setattr(a.subprocess,'check_output',run)
    assert a.host_vm_observation(vm)['uuid']==VM
    assert len(calls)==3 and all(not any(x in cmd for x in ['start','destroy','define','create']) for cmd in calls)
    image.write_bytes(b'changed')
    with pytest.raises(ValueError,match='image'):a.host_vm_observation(vm)


@pytest.mark.parametrize('mutation', ['filesystem','hostdev','qemu','disk','network'])
def test_host_XML_does_not_accept_passthrough_or_unreviewed_network(mutation):
    vm=payload()['vm'];xml=vm['xml']
    if mutation=='filesystem':xml=xml.replace('<devices>','<devices><filesystem/>')
    elif mutation=='hostdev':xml=xml.replace('<devices>','<devices><hostdev/>')
    elif mutation=='qemu':xml=xml.replace('<devices>','<devices><q:commandline xmlns:q="urn:qemu"/>')
    elif mutation=='disk':xml=xml.replace('device="disk"','device="lun"')
    else:xml=xml.replace('network="default"','network="host"')
    vm.update(xml=xml,xml_sha256=c.digest(xml.encode()))
    with pytest.raises(ValueError):a.validate_vm_xml(vm,xml)


def test_unprivileged_guest_cannot_choose_an_untrusted_public_key_path(tmp_path,monkeypatch):
    key=tmp_path/'key';key.write_bytes(HOST_PUB);key.chmod(0o666)
    monkeypatch.setattr(a,'TRUST_ROOT',key)
    with pytest.raises(ValueError):a.guest_trust_key()


def test_duplicate_or_nonfinite_authority_JSON_rejects_before_signature(tmp_path):
    p=tmp_path/'authority.json'
    for raw in ['{"payload":{},"payload":{}}','{"value":NaN}','{"value":Infinity}']:
        p.write_text(raw)
        with pytest.raises(ValueError):a.read_packet(p)


async def test_wrong_actual_VM_stops_before_any_network_or_owned_process(tmp_path,monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    monkeypatch.setattr(v,'local_context',lambda env: None)
    monkeypatch.setattr(v.os,'geteuid',lambda: 1000)
    def git(argv,**kw):return SOURCE if 'rev-parse' in argv else ''
    monkeypatch.setattr(v.probe.subprocess,'check_output',git)
    monkeypatch.setattr(v,'guest_trust_key',lambda owner=False: OWNER_PUB if owner else HOST_PUB)
    monkeypatch.setattr(v,'read_packet',lambda path: signed(payload()))
    monkeypatch.setattr(v,'source_hashes',lambda repo: HASHES)
    monkeypatch.setattr(v.time,'time',lambda:NOW)
    monkeypatch.setattr(v,'observe_guest',lambda: observation()|{'uuid':RUN})
    monkeypatch.setattr(v.httpx,'AsyncClient',lambda **kw: pytest.fail('no network'))
    monkeypatch.setattr(v.probe,'require_process_handles',lambda: pytest.fail('no process handles'))
    with pytest.raises(ValueError,match='identity'):await v.run(tmp_path/'output',tmp_path/'authority','unused')
    assert not (tmp_path/'output').exists()


def test_original_CI_workflow_and_entry_remain_CI_only():
    import inspect
    code=inspect.getsource(probe.run)
    assert 'prerequisites(dict(os.environ)' in code and 'evidence_validator=require_evidence' in code
    assert 'local_vm' not in code and 'local' not in inspect.getsource(probe.main)
    assert not Path('scripts/e2e/w4_f1/acceptance.py').read_text().count('local')


def test_owner_preboot_gate_needs_real_original_acks_without_VM_or_network(tmp_path,monkeypatch):
    from scripts.e2e.w4_f1 import local_preflight as pre
    p=payload();(tmp_path/'hold.txt').write_bytes(HOLD)
    monkeypatch.setattr(pre,'source_state',lambda repo: SOURCE);monkeypatch.setattr(pre,'source_hashes',lambda repo: HASHES)
    monkeypatch.setattr(pre.time,'time',lambda: NOW)
    result=pre.verify(p['reservation'],SOURCE,owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),
                      run_id=RUN,vm_uuid=VM,starts_at=NOW-10,expires_at=NOW+1500,hold_evidence=tmp_path)
    assert result['mode']=='local-vm-owner-preflight' and result['does_not_establish_or_infer_a_hold']
    (tmp_path/'hold.txt').unlink()
    with pytest.raises(ValueError):pre.verify(p['reservation'],SOURCE,owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),
                      run_id=RUN,vm_uuid=VM,starts_at=NOW-10,expires_at=NOW+1500,hold_evidence=tmp_path)


@pytest.mark.parametrize('left', [100,float('inf'),float('nan')])
def test_preboot_gate_rejects_short_or_nonfinite_windows_before_VM_IO(tmp_path,monkeypatch,left):
    from scripts.e2e.w4_f1 import local_preflight as pre
    monkeypatch.setattr(pre,'source_state',lambda repo: SOURCE);monkeypatch.setattr(pre,'source_hashes',lambda repo: HASHES);monkeypatch.setattr(pre.time,'time',lambda:NOW)
    with pytest.raises(ValueError):pre.verify(payload()['reservation'],SOURCE,owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),
                        run_id=RUN,vm_uuid=VM,starts_at=NOW-10,expires_at=NOW+left,hold_evidence=tmp_path)


@pytest.mark.parametrize('obstacle',['none','domain','root','unsigned','wrong-run','bad-original','forged-host','old-release','extra-release'])
def test_separate_postcleanup_receipt_requires_actual_absence_and_owner_restoration(tmp_path,monkeypatch,obstacle):
    from scripts.e2e.w4_f1 import local_cleanup as clean
    t,raw,junit=prepare_artifact(tmp_path,monkeypatch)
    g=gate.verify(tmp_path,SOURCE,junit,host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),
                  owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),hold_evidence=tmp_path)
    (tmp_path/'local-gate.json').write_text(json.dumps(signed(g,OWNER if obstacle=='forged-host' else HOST)))
    restored=b'SYNTHETIC RESTORATION ONLY';(tmp_path/'restore.txt').write_bytes(restored)
    p={'mode':'local-vm-release','run_id':RUN,'vm_uuid':VM,'source_sha':SOURCE,'reservation_id':t['reservation']['id'],
       'endpoint':a.ENDPOINT,'model':c.MODEL,'released_at':NOW+73,'restoration_reference':'synthetic-release',
       'evidence_name':'restore.txt','evidence_sha256':c.digest(restored)}
    if obstacle=='wrong-run':p['run_id']=VM
    if obstacle=='old-release':p['released_at']=NOW+71
    if obstacle=='extra-release':p['unreviewed']=True
    if obstacle=='bad-original':(tmp_path/'restore.txt').write_bytes(b'tampered')
    release=signed(p,HOST if obstacle=='unsigned' else OWNER)
    monkeypatch.setattr(clean,'source_state',lambda repo: SOURCE);monkeypatch.setattr(clean,'source_hashes',lambda repo: HASHES)
    monkeypatch.setattr(clean.time,'time',lambda:NOW+74)
    monkeypatch.setattr(clean.subprocess,'check_output',lambda *args,**kw: VM if obstacle=='domain' else '')
    original_exists=Path.exists;root=Path(t['local_vm']['authority']['payload']['vm']['owned_host_root'])
    monkeypatch.setattr(clean.Path,'exists',lambda path: True if obstacle=='root' and path==root else original_exists(path))
    def verify():return clean.verify(tmp_path,SOURCE,owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),release=release,restoration_evidence=tmp_path)
    if obstacle=='none':assert verify()['owned_domain_and_host_root_absent']
    else:
        with pytest.raises((ValueError,InvalidSignature)):verify()


def test_host_only_gate_signature_and_private_file_permissions(tmp_path):
    raw = HOST.private_bytes_raw()
    receipt = gate.seal_gate({'mode':'local-vm','source_sha':SOURCE},raw,HOST_PUB)
    HOST.public_key().verify(bytes.fromhex(receipt['signature']),a.canonical(receipt['payload']))
    with pytest.raises(ValueError):gate.seal_gate(receipt['payload'],OWNER.private_bytes_raw(),HOST_PUB)
    key=tmp_path/'host-private';key.write_bytes(raw);key.chmod(0o600)
    assert a.read_host_signing_key(key)==raw
    key.chmod(0o644)
    with pytest.raises(ValueError):a.read_host_signing_key(key)


def test_interpreter_observation_is_connected_and_optimized_oracles_refuse(monkeypatch):
    t=local_trace();t['interpreter']['real_executable']='/disconnected'
    with pytest.raises(ValueError,match='interpreter'):v.require_local_evidence(t,SOURCE,HOST_PUB,OWNER_PUB)
    from types import SimpleNamespace
    monkeypatch.setattr(v.sys,'flags',SimpleNamespace(optimize=1))
    with pytest.raises(ValueError,match='assertions'):v.require_local_evidence(local_trace(),SOURCE,HOST_PUB,OWNER_PUB)


def expanded_domain():
    """Representative expanded libvirt headless XML, including its private seed."""
    vm=payload()['vm'];root=vm['owned_host_root'];vm['seed']={'path':root+'/seed.iso','sha256':c.digest(b'SYNTHETIC SEED')}
    xml=f'''<domain type="kvm" id="1"><name>{vm['name']}</name><uuid>{VM}</uuid>
<metadata><libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0"><libosinfo:os id="http://ubuntu.com/ubuntu/24.04"/></libosinfo:libosinfo></metadata>
<memory unit="KiB">6291456</memory><currentMemory unit="KiB">6291456</currentMemory><vcpu placement="static">2</vcpu>
<resource><partition>/machine</partition></resource><os><type arch="x86_64" machine="pc-q35-8.2">hvm</type><boot dev="hd"/></os>
<features><acpi/><apic/></features><cpu mode="host-passthrough"/><clock offset="utc"/>
<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="{root}/guest.qcow2" index="1"/>
<backingStore type="file" index="2"><format type="qcow2"/><source file="{root}/base.qcow2" index="2"/><backingStore/></backingStore><target dev="vda" bus="virtio"/><alias name="virtio-disk0"/></disk>
<disk type="file" device="cdrom"><driver name="qemu" type="raw"/><source file="{root}/seed.iso" index="3"/><target dev="sda" bus="sata"/><readonly/></disk>
<controller type="pci" index="0" model="pcie-root"/><interface type="network"><mac address="52:54:00:00:00:01"/><source network="default" bridge="virbr0"/><target dev="vnet1"/><model type="virtio"/></interface>
<video><model type="none"/></video><memballoon model="virtio"/><rng model="virtio"><backend model="random">/dev/urandom</backend></rng></devices></domain>'''
    vm.update(xml=xml,xml_sha256=c.digest(xml.encode()));return vm


def test_expanded_headless_XML_accepts_only_exact_private_seed_and_backing():
    vm=expanded_domain();a.validate_vm_xml(vm,vm['xml'])


@pytest.mark.parametrize('mutation',[
    'block-cdrom','file-cdrom','lun','docker-channel','serial','relative-emulator','relative-rng',
    'loader','writable-seed','foreign-seed','foreign-backing','second-device-tree','socket-network',
])
def test_validly_resigned_XML_rejects_every_unowned_host_backend(mutation):
    vm=expanded_domain();xml=vm['xml']
    additions={
        'block-cdrom':'<disk type="block" device="cdrom"><source dev="/dev/sda"/></disk>',
        'file-cdrom':'<disk type="file" device="cdrom"><source file="/etc/shadow"/></disk>',
        'lun':'<disk type="block" device="lun"><source dev="/dev/sdb"/></disk>',
        'docker-channel':'<channel type="unix"><source mode="connect" path="/run/docker.sock"/></channel>',
        'serial':'<serial type="file"><source path="/tmp/shared"/></serial>',
    }
    if mutation in additions:xml=xml.replace('<devices>','<devices>'+additions[mutation])
    elif mutation=='relative-emulator':xml=xml.replace('/usr/bin/qemu-system-x86_64','../../bin/foreign')
    elif mutation=='relative-rng':xml=xml.replace('/dev/urandom','../../run/docker.sock')
    elif mutation=='loader':xml=xml.replace('<os>','<os><loader>/etc/host-loader</loader>')
    elif mutation=='writable-seed':xml=xml.replace('<readonly/>','')
    elif mutation=='foreign-seed':xml=xml.replace(vm['seed']['path'],'/tmp/shared.iso')
    elif mutation=='foreign-backing':xml=xml.replace(vm['image_path'],'/shared/base.qcow2')
    elif mutation=='second-device-tree':xml=xml.replace('</domain>','<devices><console type="pty"/></devices></domain>')
    else:xml=xml.replace('<model type="virtio"/></interface>','<backend type="passt" path="/run/foreign.sock"/><model type="virtio"/></interface>')
    vm.update(xml=xml,xml_sha256=c.digest(xml.encode()))
    with pytest.raises(ValueError):a.validate_vm_xml(vm,xml)


@pytest.mark.parametrize('obstacle',['none','root-writable','file-writable','symlink','hardlink','outside'])
def test_real_private_host_file_identity_and_permissions(tmp_path,obstacle):
    owned=tmp_path/'owned';owned.mkdir(mode=0o700);path=owned/'disk';path.write_bytes(b'unit');path.chmod(0o600)
    if obstacle=='root-writable':owned.chmod(0o770)
    elif obstacle=='file-writable':path.chmod(0o666)
    elif obstacle=='symlink':path.unlink();path.symlink_to(tmp_path/'outside')
    elif obstacle=='hardlink':(owned/'alias').hardlink_to(path)
    elif obstacle=='outside':path=tmp_path/'outside';path.write_bytes(b'foreign')
    if obstacle=='none':
        actual=a.private_host_file(path,owned);assert actual['inode']==path.stat().st_ino and actual['path']==str(path)
    else:
        with pytest.raises(ValueError):a.private_host_file(path,owned)


def test_actual_VM_kernel_start_clock_joins_UUID_and_name_without_operations(monkeypatch):
    vm=payload()['vm'];argv=[b'/usr/bin/qemu-system-x86_64',b'-name',('guest='+vm['name']+',debug-threads=on').encode(),b'-uuid',VM.encode()]
    texts={f"/run/libvirt/qemu/{vm['name']}.pid":'4321', '/proc/4321/stat':'4321 (qemu name) '+' '.join(['S']+['0']*18+['9000']),'/proc/stat':'btime 1000\n'}
    monkeypatch.setattr(a.Path,'read_text',lambda path,*args,**kw:texts[str(path)])
    monkeypatch.setattr(a.Path,'read_bytes',lambda path:b'\0'.join(argv)+b'\0')
    monkeypatch.setattr(a.os,'sysconf',lambda value:100)
    assert a.observe_host_process(vm)=={'pid':4321,'starttime':'9000','started_at':1090,'cmdline_sha256':c.digest(b'\0'.join(argv))}
    argv[-1]=RUN.encode()
    with pytest.raises(ValueError,match='domain'):a.observe_host_process(vm)


@pytest.mark.parametrize('mutation',[
    'all-30-days','native-30-days','requests-30-days','elapsed-negative','elapsed-nan','elapsed-inf',
    'duration-disconnected','preparation-before-authority','late-finish','request-duration-nan','runtime-before-boot',
])
def test_actual_raw_clocks_and_total_timer_are_bound_to_signed_window(mutation):
    from scripts.e2e.w4_f1.local_timing import validate_timing
    t=local_trace();p=t['local_vm']['authority']['payload'];validate_timing(t,p)
    def shifted(value):return datetime.fromtimestamp(c.utc_time(value).timestamp()+30*86400,timezone.utc).isoformat()
    if mutation in {'all-30-days','native-30-days'}:
        for row in t['disk_records']:row['created_at']=shifted(row['created_at'])
    if mutation=='all-30-days':
        for row in [*t['snapshots'].values(),*t['watches']]:
            row['observed_at']=shifted(row['observed_at'])
            if row.get('lease'):
                for field in ('claimed_at','last_heartbeat_at','expires_at'):row['lease'][field]=shifted(row['lease'][field])
    if mutation in {'all-30-days','requests-30-days'}:
        for row in t['packets']:
            for field in ('started_at','completed_at'):row[field]+=30*86400
    if mutation.startswith('elapsed-'):t['elapsed_seconds']={'elapsed-negative':-1,'elapsed-nan':float('nan'),'elapsed-inf':float('inf')}[mutation]
    elif mutation=='duration-disconnected':t['elapsed_seconds']=1
    elif mutation=='preparation-before-authority':t['local_vm']['timeline']['started_at']=NOW-100
    elif mutation=='late-finish':t['local_vm']['timeline']['finished_at']=p['expires_at']+1
    elif mutation=='request-duration-nan':t['packets'][0]['elapsed_seconds']=float('nan')
    elif mutation=='runtime-before-boot':t['old_process']['starttime']='1'
    with pytest.raises(ValueError):validate_timing(t,p)


@pytest.mark.parametrize('mutation',['missing','wrong-run','wrong-VM','wrong-source','wrong-driver','wrong-owner','unknown','post-boot','digest','clock-skew','changed-boot'])
def test_actual_signed_preboot_receipt_is_required_before_measured_boot(mutation):
    p=payload();validate(p)
    if mutation=='missing':del p['preboot']
    elif mutation=='digest':p['preboot_sha256']='0'*64
    elif mutation=='clock-skew':p['vm']['clock_probe']['guest_time']+=100
    elif mutation=='changed-boot':p['vm']['booted_at']=NOW-100
    else:
        pre=p['preboot']['payload'];field,value={
            'wrong-run':('run_id',VM),'wrong-VM':('vm_uuid',RUN),'wrong-source':('source_sha','e'*40),
            'wrong-driver':('driver_hashes',{}),'wrong-owner':('owner_key_sha256','f'*64),
            'unknown':('unknown',True),'post-boot':('checked_at',NOW-10),
        }[mutation]
        pre[field]=value;p['preboot']=signed(pre);p['preboot_sha256']=c.digest(a.canonical(p['preboot'])+b'\n')
    with pytest.raises((ValueError,KeyError)):validate(p)


async def test_exhausted_preparation_timer_refuses_before_output_or_process(tmp_path,monkeypatch):
    monkeypatch.setattr(probe.time,'monotonic',lambda:580)
    with pytest.raises(ValueError,match='preparation'):
        await probe.execute_probe(tmp_path/'never-created',head=SOURCE,reservation={},run_context={},temp_root=tmp_path,dsn='unused',evidence_validator=lambda *a:None,accepted_name='local-accepted.json',timer_started=0)
    assert not (tmp_path/'never-created').exists()


async def test_local_entry_carries_preparation_timer_into_shared_execution(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from scripts.e2e.w4_f1.local_timing import LocalRelay
    monkeypatch.setattr(v,'local_context',lambda env:None);monkeypatch.setattr(v.os,'geteuid',lambda:1000)
    monkeypatch.setattr(v.probe.subprocess,'check_output',lambda argv,**kw:SOURCE if 'rev-parse' in argv else '')
    monkeypatch.setattr(v,'guest_trust_key',lambda owner=False:OWNER_PUB if owner else HOST_PUB)
    monkeypatch.setattr(v,'read_packet',lambda path:signed(payload()));monkeypatch.setattr(v,'source_hashes',lambda repo:HASHES)
    monkeypatch.setattr(v.time,'monotonic',lambda:123);clock=iter([NOW,NOW,NOW+.3]);monkeypatch.setattr(v.time,'time',lambda:next(clock))
    monkeypatch.setattr(v,'observe_guest',observation);monkeypatch.setattr(v,'postgres_config',lambda dsn:None)
    monkeypatch.setattr(v.probe,'require_process_handles',lambda:None)
    class Client:
        def __init__(self,**kwargs):assert kwargs=={'timeout':5,'trust_env':False}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url):
            assert url==a.ENDPOINT+'/models'
            return SimpleNamespace(content=json.dumps({'data':[{'id':c.MODEL}]}).encode(),raise_for_status=lambda:None,json=lambda:{'data':[{'id':c.MODEL}]})
    monkeypatch.setattr(v.httpx,'AsyncClient',Client);seen=[]
    async def execute(output,**kwargs):seen.append(kwargs)
    monkeypatch.setattr(v.probe,'execute_probe',execute)
    await v.run(Path('/var/tmp/w4-f1-local-artifacts')/RUN,tmp_path/'synthetic-authority','unused')
    assert seen[0]['timer_started']==123 and seen[0]['relay_type'] is LocalRelay
    assert seen[0]['local_vm']['timeline']=={'started_at':NOW}


@pytest.mark.parametrize('mutation',['partial','unknown','wrong-run','wrong-VM','wrong-reservation','wrong-host-clock','missing-host-path','host-unknown','wrong-preboot','JUnit-bytes','native-bytes','preboot-bytes'])
def test_postcleanup_revalidates_closed_complete_gate_and_retained_original_bytes(tmp_path,monkeypatch,mutation):
    from scripts.e2e.w4_f1 import local_cleanup as clean
    t,raw,junit=prepare_artifact(tmp_path,monkeypatch)
    g=gate.verify(tmp_path,SOURCE,junit,host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),hold_evidence=tmp_path)
    if mutation=='partial':g={k:g[k] for k in ('mode','source_sha','trace_sha256','owner_key_sha256','host_key_sha256','verified_at')}
    elif mutation=='unknown':g['unknown']=True
    elif mutation=='wrong-run':g['run_id']=VM
    elif mutation=='wrong-VM':g['vm_uuid']=RUN
    elif mutation=='wrong-reservation':g['reservation_id']='foreign'
    elif mutation=='wrong-host-clock':g['actual_host_vm']['observed_at']-=86400
    elif mutation=='missing-host-path':g['actual_host_vm']['host_paths'].pop()
    elif mutation=='host-unknown':g['actual_host_vm']['unknown']=True
    elif mutation=='wrong-preboot':g['preboot_sha256']='0'*64
    elif mutation=='JUnit-bytes':junit.write_bytes(junit.read_bytes()+b' ')
    elif mutation=='native-bytes':
        native=next(row for row in g['native_evidence'] if row['path'].endswith('.jsonl'));path=tmp_path/native['path'];path.write_bytes(path.read_bytes()+b' ')
    else:(tmp_path/'owner-preboot.json').write_bytes((tmp_path/'owner-preboot.json').read_bytes()+b' ')
    (tmp_path/'local-gate.json').write_text(json.dumps(signed(g)))
    monkeypatch.setattr(clean,'source_state',lambda repo:SOURCE);monkeypatch.setattr(clean,'source_hashes',lambda repo:HASHES)
    monkeypatch.setattr(clean.subprocess,'check_output',lambda *args,**kw:pytest.fail('invalid gate must refuse before absence/resource checks'))
    with pytest.raises(ValueError):
        clean.verify(tmp_path,SOURCE,owner_key=OWNER_PUB,owner_key_sha256=c.digest(OWNER_PUB),host_key=HOST_PUB,host_key_sha256=c.digest(HOST_PUB),release={},restoration_evidence=tmp_path)


def test_actual_preflight_CLI_writes_exclusive_authenticated_original_bytes(tmp_path,monkeypatch):
    from scripts.e2e.w4_f1 import local_preflight as pre
    monkeypatch.setattr(pre,'source_state',lambda repo:SOURCE);monkeypatch.setattr(pre,'source_hashes',lambda repo:HASHES)
    monkeypatch.setattr(pre.time,'time',lambda:NOW-30)
    for name,data in [('owner.pub',OWNER_PUB),('host.pub',HOST_PUB),('host.key',HOST.private_bytes_raw()),('hold.txt',HOLD)]:
        path=tmp_path/name;path.write_bytes(data);path.chmod(0o600)
    reservation=tmp_path/'reservation.json';reservation.write_text(json.dumps(payload()['reservation']))
    output=tmp_path/'owner-preboot.json'
    monkeypatch.setattr(pre.sys if hasattr(pre,'sys') else __import__('sys'),'argv',[
        'preflight','--reservation',str(reservation),'--source',SOURCE,'--run-id',RUN,'--vm-uuid',VM,
        '--starts-at',str(NOW-40),'--expires-at',str(NOW+1500),'--owner-public-key',str(tmp_path/'owner.pub'),
        '--owner-key-sha256',c.digest(OWNER_PUB),'--host-public-key',str(tmp_path/'host.pub'),
        '--host-key-sha256',c.digest(HOST_PUB),'--host-signing-key',str(tmp_path/'host.key'),
        '--hold-evidence',str(tmp_path),'--receipt',str(output)])
    pre.main();sealed=a.read_packet(output)
    assert output.read_bytes()==a.canonical(sealed)+b'\n'
    HOST.public_key().verify(bytes.fromhex(sealed['signature']),a.canonical(sealed['payload']))
    p=payload();p['preboot']=sealed;p['preboot_sha256']=c.digest(output.read_bytes());validate(p)
    with pytest.raises(FileExistsError):pre.main()


@pytest.mark.parametrize('fails',[False,True])
async def test_local_relay_records_absolute_anchors_around_actual_parent_exchange(monkeypatch,fails):
    from scripts.e2e.w4_f1 import local_timing as timing
    clock=iter([NOW+1,NOW+2]);monkeypatch.setattr(timing.time,'time',lambda:next(clock))
    body={'synthetic-only':True};sentinel=object()
    async def parent(self,request):
        assert request is body;self.packets.append({'request':request})
        if fails:raise ValueError('synthetic transport refusal')
        return sentinel
    monkeypatch.setattr(timing.Relay,'exchange',parent)
    relay=timing.LocalRelay('unused')
    if fails:
        with pytest.raises(ValueError,match='transport'):await relay.exchange(body)
    else:assert await relay.exchange(body) is sentinel
    assert relay.packets==[{'request':body,'started_at':NOW+1,'completed_at':NOW+2}]
