"""Local-only absolute anchors around the unchanged relay and native lifecycle."""
import time

from .contracts import parse_response, utc_time
from .local_authority import finite
from .relay import Relay

SKEW = 2


class LocalRelay(Relay):
    async def exchange(self, body):
        started=time.time();before=len(self.packets)
        try:return await super().exchange(body)
        finally:
            matches=[row for row in self.packets[before:] if row['request'] is body]
            if len(matches)==1:matches[0].update(started_at=started,completed_at=time.time())


def validate_timing(t: dict, authority: dict):
    clock=t['local_vm']['timeline']
    if set(clock) != {'started_at','finished_at'}:raise ValueError('complete local outer clock required')
    start,end=(finite(clock[k]) for k in ('started_at','finished_at'));elapsed=finite(t['elapsed_seconds'])
    if not 0<=elapsed<=600 or not authority['starts_at']<=start<=end<=authority['expires_at'] or abs(end-start-elapsed)>SKEW:
        raise ValueError('actual local phase clock exceeds/disconnects from signed window or overall timer')
    if not authority['issued_at']-SKEW<=start<=t['reservation']['checked_at']<=t['local_vm']['guest']['observed_at']<=t['local_vm']['network']['checked_at']<=end:
        raise ValueError('preflight/guest/network phase clocks are disconnected')
    def event(value):
        stamp=utc_time(value).timestamp()
        if not start-SKEW<=stamp<=end+SKEW:raise ValueError('native event/observation outside authorized execution')
        return stamp
    watches=[event(row['observed_at']) for row in t['watches']]
    for row in t['snapshots'].values():
        event(row['observed_at'])
        for key in ('created_at','updated_at'):
            if row['session'].get(key) is not None:event(row['session'][key])
        if row['lease'] is not None:
            for key in ('claimed_at','last_heartbeat_at'):event(row['lease'][key])
            expiry=utc_time(row['lease']['expires_at']).timestamp()
            if not authority['starts_at']<=expiry<=authority['expires_at']:raise ValueError('lease expiry outside owner window')
    previous=t['local_vm']['network']['checked_at'];forwarded=[]
    for packet in t['packets']:
        a,b=finite(packet['started_at']),finite(packet['completed_at'])
        duration=finite(packet['elapsed_seconds'])
        if duration<0 or not start<=a<=b<=end or a<previous-SKEW or abs(b-a-duration)>SKEW:
            raise ValueError('actual request interval missing/outside signed execution clock')
        previous=b
        if packet['stage'] not in {'capability','compact'} and packet['disposition']=='forwarded':
            forwarded.extend((packet,c) for c in parse_response(bytes.fromhex(packet['response_hex']))['calls'])
    rows=t['disk_records'];native_times=[event(r['created_at']) for r in rows]
    if native_times!=sorted(native_times):raise ValueError('native commit clock moved backwards')
    calls=[r for r in rows if r['kind']=='tool_call'];watch_index=0
    for native,(packet,wire) in zip(calls,forwarded,strict=True):
        stamp=event(native['created_at'])
        if not packet['completed_at']-SKEW<=stamp<=packet['completed_at']+SKEW:
            raise ValueError('native call is disconnected from actual request interval')
        result=next(r for r in rows if r['kind']=='tool_result' and r['payload']['call_id']==native['payload']['id'])
        if event(result['created_at'])<stamp-SKEW:raise ValueError('tool result predates actual call')
        if wire['name']=='workspace_ext__watch_files':
            if not stamp-SKEW<=watches[watch_index]<=event(result['created_at'])+SKEW:raise ValueError('actual parked watch clock is disconnected')
            watch_index+=1
    guest=t['local_vm']['guest'];boot=guest['booted_at'];ticks=guest['clock_ticks']
    if isinstance(ticks,bool) or not isinstance(ticks,int) or not 1<=ticks<=10000:raise ValueError('native clock tick rate absent')
    app_starts=[]
    for name in ('old_process','new_process'):
        stamp=boot+int(t[name]['starttime'])/ticks
        if not start-SKEW<=stamp<=end+SKEW:raise ValueError('actual runtime process started outside execution window')
        app_starts.append(stamp)
    if app_starts[0]>watches[0]+SKEW or not event(t['snapshots']['dead_expired']['observed_at'])-SKEW<=app_starts[1]<=event(t['snapshots']['reclaimed']['observed_at'])+SKEW:
        raise ValueError('runtime restart clock does not join expired/reclaimed lease')
