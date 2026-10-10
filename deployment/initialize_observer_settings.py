"""One-time migration: persistent HA configuration helpers, automation stays OFF.

Run inside the observer container with the dashboard environment variables
(VACUUM_DASHBOARD_OPTIONS, VACUUM_DASHBOARD_API, VACUUM_DASHBOARD_TOKEN_FILE). No vacuum
services are called. Existing helpers and their values are preserved.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import yaml

root = Path('/conf')
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

d = module('dashboard', root / 'web/redirect_dashboard.py')
h = module('helpers', root / 'web/.stage-dashboard-live/helper_setup.py')
h.CORE_INPUT_TEXT_HELPERS = h.CORE_INPUT_TEXT_HELPERS + [{'id':'tracking_mode','max':32,'initial':'local'}]
options = d.load_options()
assert options.get('observation_mode') is True
apps_path = root / 'apps/apps.yaml'
apps = yaml.safe_load(apps_path.read_text())
app = apps['vacuum_automation']
assert app.get('observation_mode') is True
entities = d.helper_entities(options, d.parse_rooms(options.get('rooms')))
existing = {s['entity_id'] for s in d.all_states()}
# Creating a master helper defaults to OFF. Never silently disable an existing ON helper.
master = entities['global']['enabled']
assert master not in existing or d.state_for(master)['state'] == 'off'
zone = d.state_for(options.get('home_zone', 'zone.home')) or {}
coordinates = zone.get('attributes', {})
default_options = {
    'start_hour': ('start_hour', 8), 'end_hour': ('end_hour', 22),
    'return_buffer': ('return_buffer_min', 5), 'start_margin': ('start_margin_min', 5), 'fallback_speed': ('fallback_speed_kmh', 30),
    'default_travel_time': ('default_travel_time_min', 60),
    'home_latitude': ('home_latitude', coordinates.get('latitude', 0)),
    'home_longitude': ('home_longitude', coordinates.get('longitude', 0)),
    'travel_pause_radius': ('travel_pause_radius_km', 100), 'max_distance_km': ('max_distance_km', 0),
}
boolean_options = {'enabled': ('enabled', False), 'learning_enabled': ('learning_enabled', True),
    'home_override_enabled': ('home_override_enabled', False), 'travel_mode_enabled': ('travel_mode_enabled', True),
    'start_notifications_enabled': ('start_notifications_enabled', False), 'return_summary_enabled': ('return_summary_enabled', True)}
async def run():
    h.SUPERVISOR_WS_API = os.environ['VACUUM_DASHBOARD_API'].replace('http://','ws://').replace('https://','wss://') + '/websocket'
    # AppDaemon bundles aiohttp; no additional package installation is needed.
    import aiohttp
    class Helper(h.WebSocketHelper):
        async def connect(self):
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(h.SUPERVISOR_WS_API, timeout=15)
            assert (await self.ws.receive_json())['type'] == 'auth_required'
            await self.ws.send_json({'type':'auth','access_token':self.token})
            assert (await self.ws.receive_json())['type'] == 'auth_ok'
            return True
        async def send_command(self, msg_type, **kwargs):
            self.msg_id += 1
            await self.ws.send_json({'id':self.msg_id,'type':msg_type,**kwargs})
            return await self.ws.receive_json(timeout=15)
        async def close(self):
            await self.ws.close()
            await self.session.close()
    ws = Helper()
    ws.token = yaml.safe_load(Path(os.environ['VACUUM_DASHBOARD_TOKEN_FILE']).read_text())['ha_token']
    assert await ws.connect()
    try:
        for domain, definitions in [('input_boolean',h.CORE_INPUT_BOOLEAN_HELPERS),('input_number',h.CORE_INPUT_NUMBER_HELPERS),('input_text',h.CORE_INPUT_TEXT_HELPERS)]:
            for definition in definitions:
                suffix = definition['id']
                if suffix in {'state', 'one_time_room_override'}:
                    continue
                entity = f'{domain}.{options.get("helper_prefix", "vacuum_automation")}_{suffix}'
                if entity in existing:
                    continue
                name = h.helper_display_name(options.get('helper_prefix','vacuum_automation'),suffix)
                if domain == 'input_boolean':
                    result = await ws.create_input_boolean(name, definition.get('icon','mdi:toggle-switch'))
                elif domain == 'input_number':
                    key, fallback = default_options[suffix]
                    result = await ws.create_input_number(name, definition['min'],definition['max'],definition.get('step',1),definition.get('mode','box'),definition.get('unit_of_measurement',''),options.get(key,fallback))
                else:
                    initial = definition.get('initial','')
                    if suffix == 'selected_presence_entities':
                        initial = ','.join(d.parse_presence_entities(options.get('presence_entities')))
                    result = await ws.create_input_text(name,definition.get('max',255),initial=initial)
                assert result and result.get('success'), (entity,result)
                if domain == 'input_boolean' and suffix != 'enabled':
                    key, fallback = boolean_options[suffix]
                    if options.get(key,fallback):
                        result = await ws.send_command('call_service',domain='input_boolean',service='turn_on',service_data={'entity_id':entity})
                        assert result and result.get('success'), entity
                print('Created',entity)
        raw_rooms = options.get('rooms') or []
        if isinstance(raw_rooms, str):
            raw_rooms = yaml.safe_load(raw_rooms) or []
        for room in raw_rooms:
            prefix = options.get('helper_prefix', 'vacuum_automation')
            for suffix, domain in [('enabled', 'input_boolean'), ('interval_h', 'input_number'), ('completion_percent', 'input_number'), ('cleaning_profile', 'input_text')]:
                entity = f'{domain}.{prefix}_{room["id"]}_{suffix}'
                if entity in existing:
                    continue
                name = h.helper_display_name(prefix, f'{room["id"]}_{suffix}')
                if suffix == 'enabled':
                    result = await ws.create_input_boolean(name)
                elif suffix == 'cleaning_profile':
                    result = await ws.create_input_text(name, 255, initial='{}')
                elif suffix == 'completion_percent':
                    result = await ws.create_input_number(name, 1, 100, 1, 'box', '%', 100)
                else:
                    result = await ws.create_input_number(name, 1, 8760, 1, 'box', 'h', room.get('interval_h', 72))
                assert result and result.get('success'), (entity, result)
                if suffix == 'enabled' and room.get('enabled', True):
                    result = await ws.send_command('call_service', domain='input_boolean', service='turn_on', service_data={'entity_id':entity})
                    assert result and result.get('success'), entity
                print('Created', entity)
    finally:
        await ws.close()
asyncio.run(run())
# AppDaemon reads these helpers at runtime after this configuration reload.
bindings = {'enabled':'enabled_entity','learning':'learning_enabled_entity','travel_logic':'travel_mode_enabled_entity',
    'start_push':'start_notifications_enabled_entity','return_push':'return_summary_enabled_entity','custom_home':'home_override_enabled_entity'}
for key, entity in entities['global'].items():
    if key == 'one_time_room_override':
        continue
    app[bindings.get(key,key+'_entity')] = entity
assert app['observation_mode'] is True
from datetime import datetime, timezone
backup = apps_path.with_name('apps.yaml.before-settings-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.write_bytes(apps_path.read_bytes())
temporary = apps_path.with_suffix('.yaml.new')
temporary.write_text(yaml.safe_dump(apps, sort_keys=False))
temporary.replace(apps_path)
print('Observer setting bindings saved, observation remains enabled.')
