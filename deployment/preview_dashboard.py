#!/usr/bin/env python3
"""Generate an offline scenario gallery using the real policy and dashboard renderer.

Run: python3 deployment/preview_dashboard.py
Open the generated HTML or serve its folder locally. No HA connection is used.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate():
    # This harness exposes no Home Assistant runtime or service implementation.
    class NoDeviceAccess:
        def call_service(self, *args, **kwargs):
            raise RuntimeError('Gerätebefehle sind in der Vorschau ausgeschlossen')
    with patch.dict(sys.modules):
        for name in ('appdaemon', 'appdaemon.plugins', 'appdaemon.plugins.hass'):
            sys.modules[name] = types.ModuleType(name)
        api = types.ModuleType('appdaemon.plugins.hass.hassapi')
        api.Hass = NoDeviceAccess
        sys.modules[api.__name__] = api
        automation = load('preview_automation', ROOT / 'apps/vacuum_automation/vacuum_automation.py')
    dashboard = load('preview_dashboard', ROOT / 'addon/ghostvacuum/redirect_dashboard.py')
    now = datetime(2026, 10, 11, 12, tzinfo=ZoneInfo('Europe/Berlin'))
    rooms = [('Büro', 1), ('Schlafzimmer', 2), ('Bad', 3), ('Flur', 4), ('Pantry', 5), ('Kleiner Flur', 6), ('Wohnzimmer', 8), ('Küche', 9)]
    base_person = dict(entity_id='person.simon', state='not_home', presence='away', travel_time_min=7,
                       motion='observing', stay_minutes=0, stationary=False, monitored_absence=False)
    cases = [
        ('Deine Situationen', 'Zuhause · WLAN bestätigt', dict(presence='home', state='home', travel_time_min=0, presence_source='router'), {}, 'WLAN bestätigt die Anwesenheit. Kein automatischer Start.'),
        ('Deine Situationen', 'Im Puffer · 3 min bis zuhause', dict(travel_time_min=3), {}, 'Unterwegs, noch kein bestätigter Aufenthalt. Innerhalb des Puffers kein Start.'),
        ('Deine Situationen', 'Knapp außerhalb · 7 min bis zuhause', {}, {}, 'Der Heimweg ist noch kurz. Nach 10 Minuten bestätigtem Aufenthalt kann die Reinigung freigegeben werden.'),
        ('Deine Situationen', 'Weit genug entfernt · 25 min', dict(travel_time_min=25), {}, 'Genug Abstand zum Zuhause: Eine Reinigung kann ohne Wartezeit auf einen Aufenthalt starten.'),
        ('Deine Situationen', 'Aufenthalt · seit 3 min, Heimweg 7 min', dict(stay_minutes=3), {}, '3 von 10 Minuten Aufenthalt sind erkannt. Nach weiteren 7 beobachteten Minuten kann die Reinigung freigegeben werden.'),
        ('Deine Situationen', 'Aufenthalt · seit 15 min, Heimweg 7 min', dict(stay_minutes=15, stationary=True, monitored_absence=True, motion='staying'), {'cleaning': True}, 'Aufenthalt bestätigt. Reinigung ist freigegeben und wird bei Annäherung überwacht.'),
        ('Deine Situationen', 'Rückkehr · nähert sich, noch 7 min', dict(travel_time_min=7, monitored_absence=True, motion='approaching'), {'cleaning': True}, 'Die laufende Reinigung darf weiterlaufen. Der Stopp-Puffer ist noch nicht erreicht.'),
        ('Deine Situationen', 'Rückkehr · Puffer erreicht, noch 5 min', dict(travel_time_min=5, monitored_absence=True, motion='approaching'), {'returning': True, 'running_policy': True}, 'Die eigene automatische Reinigung wird beendet und der Roboter zur Station geschickt.'),
        ('Weitere Fälle', 'Unterwegs im Puffer · WLAN nicht verbunden', dict(state='not_home', presence='away', travel_time_min=1, near_home_unconfirmed=True), {}, 'Noch unterwegs: Der nahe Standort fällt in den Puffer. Zuhause gilt erst, wenn das WLAN-Gerät wieder erkannt wird.'),
        ('Weitere Fälle', 'Abwesenheit wird bestätigt', dict(state='home', presence='home', travel_time_min=0), {'native_away': True}, 'WLAN wurde verloren. Erst ein frischer Standort außerhalb von zuhause bestätigt die Abwesenheit.'),
        ('Weitere Fälle', 'Standortdaten fehlen oder sind veraltet', dict(state='unknown', presence='unknown', travel_time_min=None, motion='unknown'), {}, 'Ohne belastbare Anwesenheit und Rückkehrzeit keine Startfreigabe.'),
        ('Weitere Fälle', 'Außerhalb Reiseradius · Timer läuft', dict(travel_time_min=180, outside_radius=True, outside_since=(datetime.now(ZoneInfo('Europe/Berlin'))-timedelta(hours=2)).isoformat()), {}, 'Außerhalb des Reiseradius. Nach 24 Stunden wird der Reisemodus aktiv, bis dahin bleibt die Reinigung möglich.'),
        ('Weitere Fälle', 'Reisemodus', dict(travel_time_min=180, travel_mode_active=True), {'travel': True}, 'Reisemodus pausiert die automatische Reinigung.'),
        ('Weitere Fälle', 'Sonntag nicht freigegeben', dict(travel_time_min=25), {'weekday': True}, 'Ausreichend Zeit, aber der Wochentag ist für automatische Starts deaktiviert.'),
        ('Weitere Fälle', 'Außerhalb der Reinigungszeit', dict(travel_time_min=25), {'hours': True}, 'Ausreichend Zeit, aber automatische Starts sind erst ab 14 Uhr erlaubt.'),
        ('Weitere Fälle', 'Zweite Person zuhause', dict(travel_time_min=25), {'second_home': True}, 'Eine berücksichtigte Person zuhause verhindert den automatischen Start.'),
        ('Weitere Fälle', 'Nach Aufenthalt weiter vom Zuhause weg', dict(travel_time_min=3, monitored_absence=True, motion='observing'), {'returning': True, 'running_policy': True}, 'Die Puffergrenze sperrt auch nach einem Aufenthalt. Eine eigene laufende Reinigung wird beendet.'),
        ('Weitere Fälle', 'Im Puffer · seit 15 min am selben Ort', dict(travel_time_min=3, stay_minutes=0, stationary=False, monitored_absence=False, motion='observing'), {}, 'Auch nach 15 Minuten am selben Ort: Im Puffer wird kein Aufenthalt erfasst. Die Reinigung bleibt gesperrt.'),
        ('Weitere Fälle', 'Extern / manuell gestartete Reinigung', dict(presence='home', state='home', travel_time_min=0, presence_source='router'), {'external': True}, 'Eine externe Reinigung wird angezeigt, von der Rückkehr-Automatik aber nicht beendet.'),
    ]
    entries = []
    for group, title, changes, flags, description in cases:
        person = {**base_person, **changes}
        people = [person]
        if flags.get('second_home'):
            people.append({**base_person, 'entity_id': 'person.alex', 'state': 'home', 'presence': 'home', 'travel_time_min': 0, 'presence_source': 'router'})
        app = automation.VacuumAutomation()
        app.datetime = lambda aware=False: now
        app._presence_summary = lambda: people
        app._current_return_buffer_min = lambda: 5
        app._current_start_margin_min = lambda: 5
        app._current_start_hour = lambda: 14 if flags.get('hours') else 8
        app._current_end_hour = lambda: 22
        app._current_active_weekdays = lambda: list(range(6 if flags.get('weekday') else 7))
        app.travel_mode_active = flags.get('travel', False)
        running = bool(flags.get('cleaning') or flags.get('running_policy') or flags.get('external'))
        policy = app._return_policy(running=running)
        vacuum_state = 'returning' if flags.get('returning') else 'cleaning' if running else 'docked'
        own_cleaning = flags.get('cleaning', False)
        summary = {
            'available_presence_entities': [dict(entity_id=p['entity_id'], state='not_home' if flags.get('native_away') else p['state'], presence_source=p.get('presence_source') or 'gps') for p in people],
            'status': dict(observation_mode=False, anyone_home=any(p['presence']=='home' for p in people), everyone_away=all(p['presence']=='away' for p in people), presence_summary=people,
                           travel_mode_active=app.travel_mode_active, travel_pause_radius_km=100,travel_pause_after_hours=24,return_policy=policy, active_room='Küche' if own_cleaning else None,
                           robot_diagnostics={'job_status':'owned_job' if own_cleaning else 'no_owned_job'},
                           room_queue=[dict(room='Küche',room_key='segment_9',enabled=True,forecast_score=1.2,priority=1,interval_h=48,learned_duration_min=12)],
                           room_stats=[dict(room_key='segment_9',room='Küche',last_cleaned=(now-timedelta(days=2)).isoformat(),learned_duration_min=12)]),
            'states': {
                'global': dict(enabled={'state':'on'}, return_buffer={'state':'5'}, start_hour={'state':str(app._current_start_hour())}, end_hour={'state':'22'}, active_weekdays={'state':','.join(['mon','tue','wed','thu','fri','sat']+([] if flags.get('weekday') else ['sun']))}),
                'sensors': dict(return_window={'state':str(app._calculate_available_time_minutes(False))}, active_room={'state':'Küche' if own_cleaning else '', 'attributes':{'remaining_min':7}}),
                'vacuum': dict(state=vacuum_state, attributes=dict(friendly_name='Willi🐝',battery=90,current_segment=9,cleaning_progress=42,cleaning_mode='Sweeping and mopping')),
                'rooms': [dict(id=f'segment_{sid}',enabled_state={'state':'on' if sid==9 else 'off'},interval_h_state={'state':'48'}) for _,sid in rooms]},
            'room_segments':[dict(room_key=f'segment_{sid}',segment_id=sid,name=name) for name,sid in rooms]
        }
        schedule = dashboard.dashboard_schedule_notice
        with patch.object(dashboard, 'dashboard_schedule_notice', lambda s: schedule(s, now=now)):
            model = dashboard.build_live_dashboard_view(summary)
        model['updated_at'] = now.isoformat()
        entries.append(dict(group=group,title=title,description=description,policy=policy,model=model))
    folder = ROOT / 'addon/ghostvacuum/dashboard'
    template = (folder/'observation.html').read_text()
    script = (folder/'observation.js').read_text()
    script = script.replace("async function command(path, payload = {}) {", "async function command(path, payload = {}) { return false;")
    script = script.replace('if (!once) setTimeout(refresh,10000);', '').replace('setTimeout(refresh,10000);', '')
    script = script.replace("render(JSON.parse($('live-initial').textContent));", "window.renderScenario = render; render(JSON.parse($('live-initial').textContent));")
    template = template.replace('/*LIVE_STYLES*/',(folder/'observation.css').read_text()).replace('/*LIVE_SCRIPT*/',script).replace('LIVE_INITIAL_JSON',json.dumps(entries[0]['model'],ensure_ascii=False))
    template = template.replace('<title>GhostVacuum · Live-Dashboard</title>', '<title>GhostVacuum · Situationen</title>')
    # Keep production styles/renderer intact. All extra UI is confined to this generated file.
    extra_style = '''<style>.scenario-bar{padding:12px 20px;background:#e9f0e1;border-bottom:1px solid #cddcc1;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.scenario-bar label{font-weight:700}.scenario-bar select{font:inherit;padding:8px;border:1px solid #bccbb1;border-radius:9px;max-width:100%}.scenario-bar p{margin:0;flex-basis:100%;font-size:13px;color:#4b6044}.scenario-badge{font-size:12px;font-weight:650}.scenario-bar button{padding:8px 12px}.scenario-bar~header .settings{display:none}@media(max-width:760px){.scenario-bar{padding:10px;gap:7px}.scenario-bar label{font-size:12px}.scenario-bar select{flex:1;min-width:0;font-size:12px}.scenario-bar p{font-size:11px}}</style>'''
    toolbar = '''<aside class="scenario-bar"><label for="scenario">Vorschau</label><button id="previous" aria-label="Vorherige Situation">Zurück</button><select id="scenario"></select><button id="next" aria-label="Nächste Situation">Weiter</button><span class="scenario-badge" id="decision"></span><p id="scenario-description"></p></aside>'''
    preview_js = '''<script>
const scenarios = SCENARIOS;
const choice = document.getElementById('scenario');
let lastGroup;
for (const [index,item] of scenarios.entries()) {
  if (lastGroup?.label !== item.group) { lastGroup=document.createElement('optgroup');lastGroup.label=item.group;choice.append(lastGroup); }
  const option = document.createElement('option');option.value=index;option.textContent=`${index+1}. ${item.title}`;lastGroup.append(option);
}
function showScenario(index) {
  choice.value=index;const item=scenarios[index];window.renderScenario(item.model);
  document.getElementById('scenario-description').textContent=item.description;
  document.getElementById('decision').textContent=item.model.cleaning_permission.title;
  document.querySelector('.observation-label').textContent='SIMULATION · keine Verbindung zu Geräten';
  document.getElementById('connection').textContent='Beispiel · Sonntag, 12:00';
  document.getElementById('mode-footer').textContent='Vorschau mit Beispieldaten · keine Gerätebefehle';
  document.querySelectorAll('main button').forEach(button=>{button.disabled=true;});
  document.querySelectorAll('[draggable]').forEach(row=>row.draggable=false);
  document.getElementById('previous').disabled=index===0;
  document.getElementById('next').disabled=index===scenarios.length-1;
}
choice.addEventListener('change',()=>showScenario(Number(choice.value)));
document.getElementById('previous').addEventListener('click',()=>showScenario(Number(choice.value)-1));
document.getElementById('next').addEventListener('click',()=>showScenario(Number(choice.value)+1));
showScenario(0);
</script>'''.replace('SCENARIOS',json.dumps(entries,ensure_ascii=False).replace('<','\\u003c'))
    template = template.replace('</head>',extra_style+'</head>').replace('<body data-live-observation="true">','<body data-live-observation="true">'+toolbar).replace('</body>',preview_js+'</body>')
    output = ROOT/'outputs/dashboard-entwuerfe/situationen.html'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(template)
    return output, entries


if __name__ == '__main__':
    path, scenarios = generate()
    print(f'{len(scenarios)} Situationen: {path}')
    for item in scenarios:
        print(f"{item['title']}: {item['policy']['reason']}")
