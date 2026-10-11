# GhostVacuum

Smart vacuum automation for Home Assistant - cleans while you're away and finishes before you return.

Based on the [`Tasshack/dreame-vacuum`](https://github.com/Tasshack/dreame-vacuum) custom integration.

## Observation mode

For initial commissioning, keep `observation_mode: true` in the AppDaemon
configuration or add-on options. The supplied configurations enable it by default.
It publishes status and presence diagnostics (`anyone_home`, `everyone_away`),
but blocks cleaning starts and all Home Assistant service calls. Dashboard device commands, automation activation and helper management are rejected.
Known configuration helpers can be edited and saved through the settings page.
Unlike simulation mode, it does not schedule simulated cleaning jobs.

The Home Assistant integration must already provide a real `vacuum.*` entity.
A router's `device_tracker.*` for the robot only reports its network presence
and cannot supply battery, cleaning or room data.

On Home Assistant Container, run AppDaemon separately; Supervisor add-ons cannot
be installed there. Configuration samples are templates and must be adjusted.

## Installed observation deployment (10 October 2026)

- Open `http://127.0.0.1:8099` on Simon's Mac. If the SSH tunnel is no longer
  running, double-click `Beobachtung-oeffnen.command` in this repository.
- Backend: `/home/simon/ghostvacuum-observer` on `172.27.30.30`.
- Containers: `ghostvacuum-observer` and `ghostvacuum-dashboard`, with automatic
  restart. The reproducible container definition is `deployment/observer.compose.yaml`.
- Real sources: `vacuum.willi` (MOVA V70, `mova.vacuum.r59795`) and `person.simon`
  (GPS from `device_tracker.schoimphone`). Eight room segments are detected.
- Hardware supplied by the user: bagless station with a dust container and a
  fixed water connection. `dust_collection_type: container` and
  `water_connection: true` adapt the display. These options describe installed
  hardware and do not send settings to the robot. Container fill level is unknown.
- `observation_mode: true`, simulation off, all rooms disabled. No cleaning queue,
  learning or notifications. Missing control helpers are unnecessary in this mode.
- The named HA access token is stored only on the server in `secrets.yaml`, mode
  `0600`, inside a `0700` directory. It expires after one year and can be revoked
  under Simon's Home Assistant security profile as **GhostVacuum Beobachtung**.
- 38 offline tests pass, including robot job correlation and start prerequisites,
  presence timers, multiple residents, GPS
  failures and an idle-connection regression test. The
  dashboard handles connections concurrently with a 10-second request timeout.
  Earlier live POST probes returned HTTP 403. Actual departure and
  arrival with the phone have not yet been tested. Actual named HA zones count
  as away. Unknown or arbitrary states block a future cleaning window.
- Dreame integration notifications: only warnings and errors selected. The robot's
  existing schedules and internal settings are not managed by this observer.

### Live dashboard (10 October 2026)

The approved dashboard is installed at `http://127.0.0.1:8099` with live data.
It refreshes every 10 seconds through the read-only `/api/dashboard` projection.
`observation.html`, `observation.css` and `observation.js` implement the layout.
The legacy dashboard and settings remain available in the source; observation
mode selects the new dashboard without replacing the settings page.

- Dashboard cleaning controls and drag-and-drop remain locked. Settings writes
  are restricted to known configuration helpers. The master switch, one-time
  room override, device commands and helper management remain blocked.
- Each detected room appears once. A remembered `current_segment` while docked
  does not imply that room is being cleaned. Plans and dates are not invented:
  the observer publishes no room queue or room stats, so these stay empty or
  unavailable. Real cleaning reported by the robot can still be displayed.
- Individual home/away/travel state, estimated return and travel countdown come
  from the observer. A home resident means zero cleaning time; unknown presence
  or return time does not create a cleaning window.
- Old robot state reports are labelled with their age. A failed dashboard refresh
  explicitly marks the displayed data as old.
- 52 offline tests passed. Six locked live actions return HTTP 403. The
  layout was checked without scrolling at 1280×720, 1024×768 and 594×760.
- Only `ghostvacuum-dashboard` was restarted. Observer configuration was verified
  unchanged. Previous backend backup on the server:
  `/home/simon/ghostvacuum-observer/web/backup-dashboard-20261010T160304Z/`.

### Editable settings in observation mode

`deployment/initialize_observer_settings.py` created 18 persistent Home Assistant
helpers from the existing options and home zone, preserving existing helpers.
The master helper was created OFF and cannot be enabled through this dashboard.
Runtime helper bindings were added to the observer app configuration, preserving
`observation_mode: true`. AppDaemon reloads the app configuration automatically.
The observer's service-call lock remains unchanged. No HA restart was required.
Saving the return buffer through the browser was verified, followed by a reload.
Backend backup: `/home/simon/ghostvacuum-observer/web/redirect_dashboard.py.before-settings-20261010T180041Z`.

Room settings now use compact responsive cards for activation and intervals.
16 additional room helpers were initialized from the existing configuration
(all eight rooms remained disabled, interval 72 hours). Local room-name overrides
never call the MOVA rename service. The dashboard marks disabled rooms explicitly.
Its usable cleaning time subtracts the configured return buffer once from the
observer's raw return window, and displays that calculation when time is available.
The buffer is rendered and saved as whole minutes. Room activation, deactivation
and interval save were verified in the browser with the original values restored.
The dashboard still fits 594×760 without scrolling.
Backup: `/home/simon/ghostvacuum-observer/web/backup-room-settings-20261010T180953Z/`.

The Persons tab now always lists Luftlinie, Distanz-Sensor and Waze with
availability and setup requirements. A select saves `tracking_mode` in a persistent
HA input_text helper. The observer reads the selected method at runtime; invalid
or missing sensor values yield no ETA instead of silently using another method.
Global distance/Waze sensors currently belong only to `travel_person_entity`.
The UI therefore enables those methods only for that single selected person.
Multi-person sensor mappings are not implemented. The default remains Luftlinie.
Snapshot backup: `/home/simon/ghostvacuum-observer/web/backup-tracking-options-20261010T182532Z/`.

### Pending real presence test

Use the phone normally. Do not overwrite person or tracker states. Check
`/api/summary` after leaving and after returning (allow for GPS update delay):

1. At home: `person.simon: home`, `anyone_home: on`, `everyone_away: off`.
2. Away: `person.simon: not_home`, `anyone_home: off`, `everyone_away: on`,
   and `away_since` set. Actual named HA zones also count as away.
3. Back home: home indicators restored and `away_since` cleared.
4. Throughout: `observation_mode: on`, `enabled: off`, `room_queue: []`,
   no cleaning starts or device commands. Record actual times and observed values
   before marking this test complete.

## What It Does

- Cleans only while everybody in the home is away
- Plans one room at a time based on return ETA and distance
- Stops automatic cleaning if someone gets back too early
- Supports multiple residents through `presence_entities`
- Supports any city or region through `travel_pause_zone`
- Learns real room durations from successful runs
- Keeps a persistent run history database on disk
- Publishes weekly stats and room stats as Home Assistant sensors
- Lets you edit room weight, interval, duration, and enable state from the UI
- Provides an add-on sidebar dashboard for status and controls
- Ships a Supervisor add-on with option-based config rendering

## Main Concepts

### Occupancy

- `presence_entities`: everyone who lives there
- Cleaning starts only when all of them are away
- Cleaning stops when one of them comes back

### Travel Model

- Each selected person gets a separate distance and return estimate.
- The current deployment uses local straight-line distance divided by configurable
  `fallback_speed_kmh`. This is explicitly labelled an estimate, not an actual
  public transport route. No positions are sent to external routing services.
- The earliest estimated return determines the common window. Anyone home means
  zero minutes. Missing estimates mean no future cleaning window, never a default
  fictitious commute. Stale positions (over 60 minutes by default), unavailable
  trackers, invalid coordinates and poor GPS accuracy are rejected.
- The old single-person Waze/distance source does not drive this local model.

### Travel Mode

- `travel_pause_radius_km`: radius around `zone.home`.
- `travel_pause_after_hours`: required continuous time outside for each person.
- Travel mode activates only after **all selected people** individually satisfy
  that duration. Any person's re-entry immediately disables it, even before home.
- Adding a person starts a new timer for that person. Radius or duration changes
  reset timers. Unknown positions and observation gaps over three minutes also
  reset timers conservatively. Short restarts preserve timers in the separate
  `presence_timers.json`, which contains no GPS coordinates or cleaning history.
- The observation deployment currently retains code defaults: **100 km, 24 h,
  30 km/h estimate speed**. These are not confirmed personal preferences.
- Travel observation is enabled; cleaning automation remains disabled.

### Configure people and presence parameters on the server

The old dashboard settings and draggable rooms remain in the repository and match
the server files. Observation mode replaces the cleaning queue and locks controls.
The UI redesign and editable presence controls are a later development step.

Until then, use the restricted configuration script on the server. It changes only
the two local observer configurations, checks observation mode in both, validates
values, makes backups and triggers an AppDaemon app reload. It sends no HA services.

```bash
# Show current configuration without changing it:
sudo docker exec ghostvacuum-observer python /conf/configure_presence.py

# Example only: replace people and numbers with the desired values.
# Every person must already exist in HA and have a suitable location tracker.
sudo docker exec ghostvacuum-observer python /conf/configure_presence.py \
  --person person.simon --person person.other \
  --radius-km 50 --outside-hours 6 --estimate-speed-kmh 30
```

`--person` replaces the complete selected list. Omitted parameters are preserved.
The script source is `scripts/configure_presence.py`. Device commands and legacy
helper management remain blocked; known settings helpers are editable.

### Robot state and job completion

The observer distinguishes cleaning, returning, returning to wash, washing,
drying, drainage, pause, error, other busy states, unavailable data and stale data.
It never adopts a manually started robot task as its own or learns from it.

For a future start, all technical prerequisites must hold: docked robot, finished
task, no running job, idle station, sufficient known battery, confirmed location
and saved map, target room on the selected map, readable quiet hours, and no
unresolved previous job. Defaults are `minimum_start_battery: 30`,
`robot_state_max_age_sec: 300`, `start_confirmation_timeout_sec: 120`.
These are conservative defaults, not validated operating limits.

A future requested job remains unconfirmed until a room-cleaning task on the
same map explicitly reports exactly the requested active segment. No acceptance
within the timeout, an error, lost/faded data, a changed map or another room/task
requires review and blocks retries. After an AppDaemon restart, a restored active
job also requires review. There is deliberately no automatic clearing of this latch.
An optional start notification is sent only after acceptance, never for an
unconfirmed request. Notifications remain disabled in observation mode.

Success additionally requires a new task-completed transition, a stationary dock
state (final drying is allowed), no running/started job, and a new matching-time
robot history entry with `completed: true`. Docking for intermediate washing,
pause, stale completion values and `completed: false` do not count as success.
An own abort request always prevents a successful outcome. A terminal incomplete
run blocks automatic retries. GhostVacuum updates last-cleaned/learning only after
verified success, and never in observation mode.

Known limits before enabling any devices:

- The integration may leave unchanged state timestamps old while the robot rests.
  The five-minute freshness gate therefore can block an otherwise docked robot.
  This is shown as stale data, not proof that the robot is offline. A reliable
  read-only freshness source must be validated before enabling cleaning; the gate
  must not simply be removed to obtain a start.
- The actual acceptance/active-segment and history update order still needs an
  expressly authorized hardware test. The current proof consists of offline
  event sequences and live read-only checks, not an executed robot command.
- Enabled quiet-hour windows are conservatively applied every day. Per-weekday
  bitmask semantics are not used to grant permission.
- Cleaning profiles/CleanGenius policy, battery budget for a specific room,
  unknown dust-container fill level and station overhead remain later decisions.

Offline regression cases are in `tests/test_robot_state.py`. The observation
dashboard shows the interpreted phase and technical blockers under
**Staubsaugerprüfung**. This does not constitute a cleaning release.

### Learning

- Successful runs are stored in a persistent history file
- Room durations are learned from the last few successful runs
- Learned durations become the planning duration when learning is enabled

## Add-on Dashboard

The Supervisor add-on provides a built-in sidebar dashboard for:

- Live status
- Occupancy summary
- Travel logic status
- Automation toggles
- Notification toggles

## Local Dashboard Development

You can run the add-on dashboard locally with mocked Home Assistant data:

```bash
./scripts/dev_dashboard.sh
```

Then open:

```text
http://127.0.0.1:8099
```

Running the command again restarts the previously tracked local dashboard, so you
do not need to manually `pkill` old Python processes while iterating.

- The server reads the dashboard from modular files in `addon/ghostvacuum/dashboard/`:
- `index.html` — landing dashboard markup
- `styles.css` — all CSS styles
- `scripts.js` — client-side JavaScript (drag/drop, polling, interactions)

The server assembles these files on every request, allowing you to edit individual
components without restarting. See [dashboard/README.md](addon/ghostvacuum/dashboard/README.md)
for a complete guide to the structure and data attributes.

Useful commands:

```bash
./scripts/dev_dashboard.sh status
./scripts/dev_dashboard.sh stop
./scripts/dev_dashboard.sh restart
```

To use another port:

```bash
SIDEBAR_REDIRECT_PORT=8100 ./scripts/dev_dashboard.sh
```

## Sensors Exposed by the App

- `sensor.vacuum_automation_status`
- `sensor.vacuum_automation_active_room`
- `sensor.vacuum_automation_next_room`
- `sensor.vacuum_automation_travel_time`
- `sensor.vacuum_automation_return_window`
- `sensor.vacuum_automation_distance_to_home`
- `sensor.vacuum_automation_weekly_runs`
- `sensor.vacuum_automation_weekly_minutes`
- `sensor.vacuum_automation_history`

## UI Helpers

Global helpers:

- `input_boolean.vacuum_automation_enabled`
- `input_boolean.vacuum_automation_learning_enabled`
- `input_number.vacuum_automation_start_hour`
- `input_number.vacuum_automation_end_hour`
- `input_number.vacuum_automation_return_buffer`
- `input_number.vacuum_automation_fallback_speed`
- `input_number.vacuum_automation_default_travel_time`

Per-room helpers follow this pattern:

- `input_boolean.vacuum_automation_<room>_enabled`
- `input_number.vacuum_automation_<room>_weight`
- `input_number.vacuum_automation_<room>_interval_h`
- `input_number.vacuum_automation_<room>_duration_min`

Example rooms included in the sample setup:

- `bad`
- `kueche`
- `wohnzimmer`
- `schlafzimmer`

## Repo Layout

- `apps/vacuum_automation/`: AppDaemon app and sample config
- `setup/`: helper and zone examples
- `addon/ghostvacuum/`: Supervisor add-on with generated config, helpers, and an ingress dashboard
- `INSTALLATION.md`: setup guide

### Rückkehrgrenzen und Analyse (Beobachtungsbetrieb)

Die automatische Planung startet fällige aktive Räume, sobald die kürzeste bekannte Heimwegzeit mindestens Puffer + Startabstand beträgt. Die Raumdauer muss nicht vollständig hineinpassen. Ein eigener laufender Auftrag darf bis zur Puffergrenze weiterlaufen; dann fährt der Sauger zur Station. Anwesenheit, unbekannte/veraltete Heimwegdaten, Reisemodus und das Ende des Tagesfensters verhindern weitere Reinigung. Externe Aufträge werden nicht übernommen oder beendet. Der Beobachtungsmodus sperrt weiterhin sämtliche Gerätebefehle.

Die Analyse im Tab **System** zeigt die Entscheidung ohne Geräteaktion, die beiden Grenzen sowie Puffervergleiche anhand echter Messpunkte. `storage/return_analysis.json` speichert höchstens einen Messpunkt pro Minute für 14 Tage, ohne Koordinaten. Lücken werden nicht hochgerechnet, die Vergleichszahlen sind keine abgeschlossenen Reinigungen. Die Vergleiche verwenden den aktuellen Startabstand und die zum Messzeitpunkt erlaubten Anwesenheits-/Zeitfenster. Bestehende Automatik-Historie bleibt getrennt.

Pro Raum bedeutet `completion_percent=100`: nur bestätigter vollständiger Abschluss. Kleinere Werte konfigurieren einen Mindestfortschritt. Bei einem bestätigten eigenen Einzelraum-Auftrag wird der Gesamtfortschritt als Raumfortschritt zugeordnet. Dafür müssen Karte, Auftragsart und genau ein aktives Segment weiterhin passen. Nur aktuelle Prozentmeldungen nach Annahme des Auftrags zählen; Rückfahrt-/Abschlusswerte werden nicht übernommen. Der höchste bestätigte Wert bleibt für die Auswertung eines bestätigten Abbruchs erhalten. Die aktuell installierte MOVA/Dreame-Integration meldet ihren Fortschrittssensor im angedockten Zustand als nicht verfügbar. Ohne gültige Werte zählt weiterhin nur ein bestätigter vollständiger Abschluss. Externe oder Mehrraum-Aufträge und Laufzeit-Schätzungen zählen nicht als prozentualer Raumabschluss.

Die additive Migration `deployment/initialize_observer_settings.py` erstellt fehlende `start_margin`- und Raum-`completion_percent`-Helper, erhält bestehende Werte und bricht ab, wenn Beobachtungsmodus oder deaktivierter Hauptschalter nicht bestätigt werden können.

### Reinigungseinstellungen

Im Tab **Räume** legt **Standard-Reinigung** das gemeinsame Profil fest. Jeder Raum erbt es, bis unter **Reinigung → Eigene Einstellungen** ein vollständiges eigenes Profil gespeichert wird. Zurück zu **Standard verwenden** entfernt diese Ausnahme. Profile werden in `input_text.<prefix>_cleaning_defaults` bzw. `input_text.<prefix>_<room>_cleaning_profile` gespeichert. Die additive Migration erstellt fehlende Profile, ohne bestehende Konfigurationen zu überschreiben.

CleanGenius bietet Routine/Gründlich und gemeinsame/sequenzielle Reinigung. Manuelle Modi bieten Saugen, Wischen, beides gemeinsam oder nacheinander, passende Saugkraft/Feuchtigkeit, Route und 1–3 Durchgänge. Wassertemperatur wird mangels bestätigter Geräteoption nicht angeboten. Bei aktiver Automatik werden Geräteeinstellungen vor einem Einzelraum-Start schrittweise gesetzt und durch Rückmeldung bestätigt. Fehlende/ungültige Profile oder nicht verfügbare Optionen verhindern den Start. Im Beobachtungsmodus bleibt auch das Setzen dieser Geräteeinstellungen gesperrt. Die tatsächliche Geräteausführung ist unter dieser Sperre nicht getestet.

### Lokale Runtipi-Installation (10.10.2026)

GhostVacuum läuft als benutzerdefinierte Runtipi-App `ghostvacuum:_user` mit zwei verwalteten Diensten. Öffnen: http://172.27.30.30/ghostvacuum/ oder in Home Assistant über **GhostVacuum** in der Seitenleiste: http://172.27.30.30:8123/dashboard-ghostvacuum/0. Die Route nutzt Runtipis vorhandenen lokalen HTTP-Proxy, ohne Internetfreigabe. Die Compose-Vorlage liegt unter `deployment/runtipi/docker-compose.yml`.

Die persistenten Daten und Zugangsdaten bleiben unter `/home/simon/ghostvacuum-observer` auf dem Server. Runtipi-Backups müssen diesen externen Datenpfad zusätzlich sichern. Die früheren Container `ghostvacuum-observer` und `ghostvacuum-dashboard` sind gestoppt und haben `restart=no`. Für einen Rückfall zuerst die Runtipi-App vollständig stoppen, dann die alten Container starten. Nie beide Automatik-Dienste gleichzeitig betreiben.

Home Assistants Proximity-Integration liefert `sensor.home_entfernung_von_simon` für Simon und die Zuhause-Zone. GhostVacuum verwendet diesen Sensor im Trackingmodus **Distanz-Sensor**. Das ist eine Entfernung zur Zuhause-Zone, keine berechnete Straßenroute. Die Rückkehrzeit bleibt eine Schätzung aus Entfernung und konfigurierter Geschwindigkeit. `deployment/configure_ha_dashboard_distance.py` dokumentiert die Einrichtung über HA-APIs ohne HA-Neustart.

Nach der Übernahme: Beobachtungsmodus aus, Hauptschalter weiterhin aus, nur Küche aktiv. Es wurde kein Gerätebefehl und keine Reinigung gestartet.

### GhostVacuum aktualisieren

Auf diesem Mac im Projektordner **GhostVacuum aktualisieren.command** doppelklicken, oder aus dem Projektverzeichnis:

```sh
python3 deployment/update.py
```

Zuerst in den Einstellungen die Automatik ausschalten und warten, bis der Sauger angedockt ist. Der Prozess bricht bei aktivem Hauptschalter, nicht angedocktem oder nicht verfügbarem Sauger und fehlender HA-Verbindung ab. Er startet oder stoppt keine Reinigung und aktiviert die Automatik nicht.

Der Befehl prüft alle lokalen Tests, überträgt ein Paket mit SHA-256-Prüfsummen per SSH mit geprüftem Hostschlüssel und sperrt parallele Updates auf dem Server. Nach Anhalten des Automatik-Dienstes wird der Zustand erneut geprüft. Anschließend werden beide GhostVacuum-Dienste angehalten und der vollständige Datenordner konsistent gesichert. Die Sicherung wird vor dem Dateiaustausch vollständig gelesen. Nur Anwendungscode und Dashboard-Dateien werden ersetzt. Zugangsdaten, Optionen, Raumprofile, HA-Helper, Historie und Runtipi-Konfiguration bleiben erhalten.

Nach dem Start müssen beide Container laufen, die App ihre Initialisierung bestätigen, das Live-Dashboard eine gültige Antwort liefern und der Hauptschalter weiterhin aus sein. Bei Fehlern wird der vorherige Code automatisch wiederhergestellt und erneut geprüft. Scheitert auch diese Prüfung, bleibt der Automatik-Dienst gestoppt. Eine Fehlermeldung und ein Exit-Code ungleich null melden auch einen erfolgreich zurückgerollten Updateversuch.

Sicherungen liegen auf dem Server unter `/home/simon/ghostvacuum-backups/<Versionskennung>/`: `data.tar.gz` enthält den vollständigen vorherigen Datenstand, `code/` die automatisch wiederherstellbaren Dateien, `manifest.json` das Update und `result.json` dessen Ergebnis. Der Sicherungsordner ist nur für root zugänglich, das Archiv hat Modus 0600 und enthält auch Zugangsdaten. Sicherungen werden nicht automatisch gelöscht. Beim automatischen Rollback wird ausschließlich Code zurückgesetzt, keine laufenden Einstellungen oder Historien.

`python3 deployment/update.py --check` führt nur lokale Tests und Paketbau aus. SSH-Ziel, Schlüssel und known_hosts können mit `--host`, `--key` und `--known-hosts` angegeben werden. Der Ablauf aktualisiert den aktuellen lokalen Arbeitsstand, einschließlich uncommitteter Änderungen. Container-Image, Runtipi-Compose und Konfigurationsmigrationen sind separate Wartungsschritte. Home Assistant, Zigbee2MQTT und Runtipi selbst werden nicht neu gestartet.

### MOVA: bestätigte Geräteabfragen im Ruhezustand

Die installierte Dreame/MOVA-Integration v2.0.1 nutzt vorwiegend Cloud-Push. Ein unveränderter Ruhezustand kann deshalb lange keinen neuen HA-Zeitstempel erhalten. Nach vorausgegangenen DNS-/Verbindungsfehlern wurde am 10.10.2026 ausschließlich die Integration neu geladen. DNS und der MOVA-MQTT-Port wurden aus dem HA-Container geprüft.

`deployment/mova_polling_patch.py` ergänzt für genau diese Version im bestehenden Geräte-Timer mindestens alle 60 Sekunden eine erzwungene **Leseabfrage** der Eigenschaften. Erst nach erfolgreichem `update(True)` wird `vacuum.willi.attributes.last_updated_time.ghostvacuum_confirmed_poll` aktualisiert und HA benachrichtigt. Damit erhält auch AppDaemon einen echten, veränderten Zustandsbericht. Bei Abfragefehlern entsteht kein neuer Nachweis und die bestehende Fehlerbehandlung bleibt aktiv. Es werden keine Reinigungs-, Stations- oder Einstellungsbefehle gesendet.

Der Patch wird separat im HA-Container ausgeführt und benötigt danach einen kontrollierten HA-Neustart. Ein Neuladen des Integrationseintrags übernimmt Python-Quelländerungen wegen des Modul-Caches nicht. Vorherige Dateien liegen unter `/config/.ghostvacuum-integration-backups/`. Der ursprüngliche unveränderte Stand liegt im Backup `20261010T205800Z/device.py`. Zum Rücksetzen diese Datei nach `/config/custom_components/dreame_vacuum/dreame/device.py` kopieren und HA nach erneuter Prüfung auf laufende Geräteupdates kontrolliert neu starten. Zigbee2MQTT und MQTT müssen nicht neu gestartet werden.

Ein HACS-Update der Dreame-Integration kann den Patch überschreiben. Für v2.0.1 kann er erneut angewendet werden; bei anderen Versionsnummern oder unbekanntem Quelltext bricht er ab. Dann muss die neue Integration zuerst geprüft werden. Der GhostVacuum-Code-Updater verändert diese Fremdintegration nicht.


### Aufenthalt nahe zuhause

Die Rückkehrlogik kann einen Aufenthalt nach mindestens 10 Minuten innerhalb eines
75-m-Bereichs freigeben. Dazu müssen unterschiedliche GPS-Meldungen mit höchstens
50 m Ungenauigkeit vorliegen, ohne Lücke über 10 Minuten. Wiederholte Abfragen
desselben Standorts zählen nicht als Aufenthalt. Eine Annäherung um mindestens
75 m aktiviert die Rückkehrprüfung. Erst innerhalb des Rückkehrpuffers wird ein
eigener Auftrag beendet. Bewegung weg von zuhause oder seitlich hebt einen zuvor
bestätigten Aufenthalt für einen laufenden Auftrag nicht auf. Eine erkennbare
Umkehr weg von zuhause um mindestens 75 m löst die Annäherungsmarkierung wieder.
Neue Aufträge benötigen weiterhin den Startabstand oder einen bestätigten Aufenthalt. Ein Neustart braucht den normalen Startabstand oder einen neu bestätigten
10-minütigen Aufenthalt. Zuhause, fehlende Daten, Reise- und Zeitregeln haben Vorrang.
Nach einem Neustart wird ein Aufenthalt erneut erkannt. Externe Aufträge werden
weiterhin nicht automatisch beendet.

Die System-Analyse zeichnet Aufenthaltsfreigaben mit auf. Die historische
Standortanalyse kann auf dem Server erneut gelesen werden mit:

```sh
sudo docker exec ghostvacuum__user-observer-1 python /conf/web/backfill_presence_motion.py
```

Das liest die letzten 14 Tage des HA-Recorders und schreibt ausschließlich
zusammengefasste Kennzahlen nach `storage/return_motion_history.json`. Es werden
keine Gerätebefehle gesendet und keine genauen Positionen in der Auswertung
abgelegt. Der Vergleich verwendet heutige Personen-, WLAN-, Geschwindigkeits-
und Puffer-Einstellungen und weist fehlende Anwesenheitsdaten aus. Er zählt
Standortmeldungen, keine Minuten und keine tatsächlichen Reinigungsstarts.
Verspätete iOS-Standortmeldungen können eine rechtzeitige Rückfahrt verhindern.

### WLAN-Verlust und bestätigte Abwesenheit

Bei Personen mit zugewiesenem WLAN-Tracker reicht `not_home` oder ein ausgefallener Tracker nicht für eine Reinigungsfreigabe. Ein zuvor bestätigtes Zuhause bleibt erhalten, bis ein GPS-Tracker der Person nach dem WLAN-Zustandswechsel eine neue Standortaktualisierung außerhalb der Zuhause-Zone liefert. Nach einem Observer-Neustart ist ebenfalls eine neue Aktualisierung erforderlich. Ohne vorheriges Zuhause bleibt der Zustand bis dahin unbekannt und blockiert die Reinigung.

Die Standortbestätigung muss höchstens zehn Minuten alt und auf höchstens 50 Meter genau sein. Sie muss einschließlich Genauigkeitsabstand außerhalb des Zuhause-Radius liegen. Wiederholungen eines alten HA-Zustands (`last_reported` ohne neues `last_updated`) bestätigen keine Abwesenheit. Jede erneute WLAN-Zustandsänderung setzt die Freigabe zurück. GPS-Daten werden hierfür nicht auf Platte gespeichert. Personen ohne zugewiesenen WLAN-Tracker verwenden weiterhin die vorhandene Standortprüfung. Ein Offline-Sauger kann keine zuverlässigen Stoppbefehle empfangen.
