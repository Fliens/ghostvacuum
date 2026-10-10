"""Run on the existing GhostVacuum host. Uses HA's supported APIs, no robot services.
Requires VACUUM_DASHBOARD_OPTIONS, VACUUM_DASHBOARD_API and TOKEN_FILE.
Creates Proximity only if absent, links the dashboard without replacing others.
"""
import asyncio,importlib.util,json,os,math,shutil,datetime
from pathlib import Path
import aiohttp,yaml
root=Path('/conf')
spec=importlib.util.spec_from_file_location('dashboard',root/'web/redirect_dashboard.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
options=d.load_options();app_path=root/'apps/apps.yaml';apps=yaml.safe_load(app_path.read_text());app=apps['vacuum_automation']
master=app['enabled_entity'];assert d.state_for(master)['state']=='off','Master must remain OFF'
person=options.get('travel_person_entity','person.simon');zone=options.get('home_zone','zone.home')
entries=d.api_request('/config/config_entries/entry')
proximity=[e for e in entries if e.get('domain')=='proximity']
# Reuse the existing integration for this zone; ambiguous entries fail closed.
zone_name=d.state_for(zone).get('attributes',{}).get('friendly_name')
matches=[e for e in proximity if e.get('title')==zone_name]
assert len(matches)<=1,'Multiple proximity integrations for this zone: select explicitly'
entry_id=matches[0]['entry_id'] if matches else None
if entry_id is None:
 flow=d.api_request('/config/config_entries/flow','POST',{'handler':'proximity','show_advanced_options':False})
 assert flow['type']=='form',flow.get('type')
 result=d.api_request('/config/config_entries/flow/'+flow['flow_id'],'POST',{'zone':zone,'tracked_entities':[person],'ignored_zones':[],'tolerance':50})
 assert result['type']=='create_entry',result.get('errors')
 entry_id=result['result']['entry_id']
 print('Proximity integration created for',person,zone)

async def configure():
 token=yaml.safe_load(Path(os.environ['VACUUM_DASHBOARD_TOKEN_FILE']).read_text())['ha_token']
 url=os.environ['VACUUM_DASHBOARD_API'].replace('http://','ws://').replace('https://','wss://')+'/websocket'
 async with aiohttp.ClientSession() as session:
  async with session.ws_connect(url) as ws:
   assert (await ws.receive_json())['type']=='auth_required'
   await ws.send_json({'type':'auth','access_token':token});assert (await ws.receive_json())['type']=='auth_ok'
   counter=0
   async def call(kind,**payload):
    nonlocal counter
    counter+=1;await ws.send_json({'id':counter,'type':kind,**payload})
    while True:
     reply=await ws.receive_json(timeout=20)
     if reply.get('id')==counter:break
    assert reply.get('success'),reply.get('error')
    return reply.get('result')
   registry=await call('config/entity_registry/list')
   sensors=[e for e in registry if e.get('config_entry_id')==entry_id and e.get('entity_id','').startswith('sensor.') and e.get('translation_key')=='dist_to_zone']
   if not sensors:
    sensors=[e for e in registry if e.get('config_entry_id')==entry_id and e.get('entity_id','').endswith('_distance') and 'nearest' not in e.get('entity_id','')]
   assert len(sensors)==1,[(e.get('entity_id'),e.get('translation_key')) for e in registry if e.get('config_entry_id')==entry_id]
   sensor=sensors[0]['entity_id']
   if sensors[0].get('disabled_by'):
    await call('config/entity_registry/update',entity_id=sensor,disabled_by=None)
   dashboards=await call('lovelace/dashboards/list')
   path='dashboard-ghostvacuum'
   existing=next((x for x in dashboards if x.get('url_path')==path),None)
   if existing is None:
    await call('lovelace/dashboards/create',url_path=path,title='GhostVacuum',icon='mdi:robot-vacuum',show_in_sidebar=True,require_admin=False)
    await call('lovelace/config/save',url_path=path,config={'title':'GhostVacuum','views':[{'title':'GhostVacuum','path':'0','type':'panel','cards':[{'type':'iframe','url':'http://172.27.30.30/ghostvacuum/','aspect_ratio':'100%'}]}]})
    print('HA sidebar dashboard created:',path)
   else:
    print('Existing HA dashboard preserved:',path)
   return sensor
sensor=asyncio.run(configure())
state=d.state_for(sensor)
assert state and state.get('attributes',{}).get('unit_of_measurement') in {'m','km'},'Distance sensor not ready'
value=float(state['state']);assert math.isfinite(value) and value>=0
backup=root/'web'/('backup-ha-distance-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir()
for f in ['options.yaml','apps/apps.yaml']:
 target=backup/f;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(root/f,target)
options['distance_entity']=sensor;app['distance_entity']=sensor
options['dashboard_path']='/dashboard-ghostvacuum/0';app['dashboard_path']='/dashboard-ghostvacuum/0'
assert d.state_for(master)['state']=='off'
for path,data in [(root/'options.yaml',options),(app_path,apps)]:
 tmp=Path(str(path)+'.new');tmp.write_text(yaml.safe_dump(data,sort_keys=False,allow_unicode=True));tmp.replace(path)
entity=d.helper_entities(options,[])['global']['tracking_mode']
d.service_call('input_text','set_value',entity,{'value':'distance'})
assert d.state_for(master)['state']=='off'
print('Configured distance sensor:',sensor,state['state'],state['attributes']['unit_of_measurement'])
print('Master OFF, no vacuum commands, HA not restarted')
