"""Add only missing runtime helpers. Run inside dashboard; master must be OFF."""
import asyncio
import datetime
import json
from pathlib import Path
import shutil
import sys
import yaml
import aiohttp
sys.path.insert(0,'/conf/web')
import redirect_dashboard as d
root=Path('/conf');path=root/'apps/apps.yaml';apps=yaml.safe_load(path.read_text());app=apps['vacuum_automation']
assert d.state_for(app['enabled_entity'])['state']=='off'
rooms=app['rooms'];prefix=d.load_options().get('helper_prefix','vacuum_automation')
state_helper=app.get('state_helper') or f'input_text.{prefix}_state'
assert state_helper.startswith('input_text.')
existing={s['entity_id'] for s in d.all_states()}
definitions=[]
for key,room in rooms.items():
 for suffix,lo,hi,step,unit,initial in [('weight',0.1,10,0.1,'',room.get('weight',1)),('duration_min',1,120,1,'min',room.get('duration_min',15))]:
  definitions.append(('input_number',f'{prefix}_{key}_{suffix}',{'min':lo,'max':hi,'step':step,'mode':'box','unit_of_measurement':unit,'initial':initial}))
definitions.append(('input_text',state_helper.split('.',1)[1],{'max':255,'initial':'{}'}))
async def repair():
 token=yaml.safe_load((root/'secrets.yaml').read_text())['ha_token']
 async with aiohttp.ClientSession() as session:
  async with session.ws_connect('ws://172.27.30.30:8123/api/websocket') as ws:
   await ws.receive_json();await ws.send_json({'type':'auth','access_token':token});assert (await ws.receive_json())['type']=='auth_ok'
   counter=0
   async def call(kind,**payload):
    nonlocal counter
    counter+=1;await ws.send_json({'id':counter,'type':kind,**payload})
    while True:
     reply=await ws.receive_json(timeout=20)
     if reply.get('id')==counter:break
    assert reply.get('success'),reply.get('error')
    return reply.get('result')
   for domain,obj,fields in definitions:
    entity=f'{domain}.{obj}'
    if entity in existing:continue
    result=await call(domain+'/create',name=obj.replace('_',' '),**fields)
    assert result.get('id')==obj,(obj,result.get('id'))
    print('Created',entity)
asyncio.run(repair())
# Repair only the null binding, keeping every other setting unchanged.
if not app.get('state_helper'):
 backup=path.with_name('apps.yaml.before-state-helper-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
 shutil.copy2(path,backup)
 app['state_helper']=state_helper;path.write_text(yaml.safe_dump(apps,sort_keys=False))
assert d.state_for(app['enabled_entity'])['state']=='off'
print('Automatik AUS. Keine Gerätebefehle.')
