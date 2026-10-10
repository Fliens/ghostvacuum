"""Read HA recorder history and persist coordinate-free motion analysis. No services."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import math
import sys
import urllib.parse
import urllib.request


def replay_motion(app, histories, people, home, buffer_min, margin_min, speed_kmh, since, until):
    latest = {}; previous_motion = {}; previously_allowed = False
    stats = {'gps_reports':0,'usable_reports':0,'dwell_episodes':0,'extra_release_reports':0,'approach_stops':0,'gaps':0,'home_reports':0,'unknown_presence_reports':0,'unusable_gps_reports':0}
    dates=[]; last_gps={}
    events=[]
    for stream in histories:
        for state in stream:
            try:
                t=datetime.fromisoformat(state.get('last_updated') or state['last_changed']).timestamp()
                if since.timestamp() <= t <= until.timestamp(): events.append((t,state))
            except (ValueError, TypeError, KeyError): continue
    gps_ids={p['gps'] for p in people}
    for timestamp, state in sorted(events,key=lambda item:item[0]):
        entity=state.get('entity_id'); latest[entity]=state
        if entity not in gps_ids: continue
        stats['gps_reports']+=1;dates.append(timestamp)
        if entity in last_gps and timestamp-last_gps[entity]>600:stats['gaps']+=1
        last_gps[entity]=timestamp
        now=datetime.fromtimestamp(timestamp,timezone.utc)
        app.datetime=lambda:now
        results=[]
        for person in people:
            gps=latest.get(person['gps'],{});attrs=gps.get('attributes',{})
            routers=[latest.get(e) for e in person['routers']]
            presence='home' if gps.get('state')=='home' or any(r and r.get('state')=='home' for r in routers) else 'away' if gps.get('state') not in {None,'unknown','unavailable',''} and all(r and r.get('state') not in {'unknown','unavailable',''} for r in routers) else 'unknown'
            distance=None
            try:
                coords=[float(attrs['latitude']),float(attrs['longitude'])]
                if all(math.isfinite(v) for v in coords):distance=app._haversine_km(*coords,*home)
            except (ValueError,TypeError,KeyError): pass
            gps=dict(gps);gps['last_reported']=gps.get('last_updated') or gps.get('last_changed')
            app.get_state=lambda *args,**kw:gps
            motion=app._person_motion(person['id'],presence,distance)
            eta=math.ceil(distance/speed_kmh*60) if distance is not None else None
            valid=motion['motion']!='unknown' and presence=='away' and eta is not None
            if previous_motion.get(person['id'])!='staying' and motion['motion']=='staying':stats['dwell_episodes']+=1
            previous_motion[person['id']]=motion['motion']
            results.append({**motion,'valid':valid,'eta':eta,'presence':presence})
        usable=bool(results) and all(r['valid'] for r in results)
        stats['usable_reports']+=int(usable)
        if not usable:
            if any(r['presence']=='home' for r in results):stats['home_reports']+=1
            elif any(r['presence']=='unknown' for r in results):stats['unknown_presence_reports']+=1
            else:stats['unusable_gps_reports']+=1
        new_allowed=usable and all(r['stationary'] or r['eta']>=buffer_min+margin_min for r in results)
        old_allowed=usable and all(r['eta']>=buffer_min+margin_min for r in results)
        if new_allowed and not old_allowed:stats['extra_release_reports']+=1
        if previously_allowed and usable and any(r['motion']=='approaching' and r['eta']<=buffer_min for r in results):stats['approach_stops']+=1
        # Running jobs keep permission down to the stop boundary.
        previously_allowed=usable and all(r['stationary'] or r['eta']>buffer_min for r in results)
    return {**stats,'requested_since':since.isoformat(),'until':until.isoformat(),
            'first_report':datetime.fromtimestamp(min(dates),timezone.utc).isoformat() if dates else None,
            'last_report':datetime.fromtimestamp(max(dates),timezone.utc).isoformat() if dates else None,
            'buffer_min':buffer_min,'margin_min':margin_min,'speed_kmh':speed_kmh,
            'note':'Standortlogik mit heutigen Einstellungen. Meldungen sind keine Minuten oder tatsächlichen Reinigungsstarts. WLAN-Anwesenheit wird berücksichtigt, soweit aufgezeichnet. Reise- und Reinigungszeiten sowie Raumfälligkeit sind nicht Teil dieses Vergleichs.'}


def main():
    import yaml
    sys.path.insert(0,'/conf/apps')
    from vacuum_automation import VacuumAutomation
    options=yaml.safe_load(Path('/conf/options.yaml').read_text())
    token=yaml.safe_load(Path('/conf/secrets.yaml').read_text())['ha_token']
    base='http://172.27.30.30:8123/api'
    def read(path):
        req=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=120) as response:return json.load(response)
    states={s['entity_id']:s for s in read('/states')}
    prefix=options.get('helper_prefix','vacuum_automation')
    def value(suffix,default):return states.get('input_text.'+prefix+'_'+suffix,{}).get('state',default)
    selected=value('selected_presence_entities','')
    if selected in {'','unknown','unavailable'}:selected=options.get('presence_entities',options.get('person_entity',''))
    if isinstance(selected,str):selected=[s.strip() for s in selected.split(',') if s.strip()]
    people=[]
    for entity in selected:
        trackers=states.get(entity,{}).get('attributes',{}).get('device_trackers',[])
        gps=[e for e in trackers if states.get(e,{}).get('attributes',{}).get('source_type')=='gps']
        if len(gps)!=1:raise ValueError('Historie benötigt einen eindeutigen GPS-Tracker je berücksichtigter Person')
        people.append({'id':entity,'gps':gps[0],'routers':[e for e in trackers if states.get(e,{}).get('attributes',{}).get('source_type')=='router']})
    if not people:raise ValueError('Keine Personen ausgewählt')
    ids={p['gps'] for p in people}|{e for p in people for e in p['routers']}
    until=datetime.now(timezone.utc);since=until-timedelta(days=14)
    query=urllib.parse.urlencode({'filter_entity_id':','.join(sorted(ids)),'end_time':until.isoformat(),'significant_changes_only':'0','skip_initial_state':'1'})
    histories=read('/history/period/'+urllib.parse.quote(since.isoformat(),safe='')+'?'+query)
    attrs=states.get(options.get('home_zone','zone.home'),{}).get('attributes',{})
    home=(float(attrs['latitude']),float(attrs['longitude']))
    def number(suffix,default):return float(states.get('input_number.'+prefix+'_'+suffix,{}).get('state',default))
    app=object.__new__(VacuumAutomation)
    report=replay_motion(app,histories,people,home,number('return_buffer',5),number('start_margin',5),number('fallback_speed',30),since,until)
    config=yaml.safe_load(Path('/conf/apps/apps.yaml').read_text())['vacuum_automation']
    path=Path(config['storage_dir'])/'return_motion_history.json';path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False));tmp.replace(path)
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
