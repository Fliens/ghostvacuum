"""Configure observer presence only. Never connects to HA or controls devices."""
import argparse
import json
import math
import os
from pathlib import Path
import re

import yaml


def configure(root, people=None, radius=None, hours=None, speed=None):
    paths = [root / 'options.yaml', root / 'apps/apps.yaml']
    documents = [yaml.safe_load(path.read_text()) for path in paths]
    options, apps = documents
    app = apps['vacuum_automation']
    if options.get('observation_mode') is not True or app.get('observation_mode') is not True:
        raise ValueError('Beobachtungsmodus muss in beiden Konfigurationen aktiv sein')
    changes = {}
    if people is not None:
        if not people or any(not re.fullmatch(r'person\.[a-z0-9_]+', p) for p in people):
            raise ValueError('Mindestens eine gültige person.*-Entität erforderlich')
        changes['presence_entities'] = list(dict.fromkeys(people))
    for key, value, allow_zero in [('travel_pause_radius_km', radius, False),
                                    ('travel_pause_after_hours', hours, True),
                                    ('fallback_speed_kmh', speed, False)]:
        if value is not None:
            if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
                raise ValueError(f'Ungültiger Wert für {key}')
            changes[key] = value
    if changes:
        for document in (options, app):
            document.update(changes)
            document['selected_presence_entities_entity'] = None
            document['travel_mode_enabled'] = True
        # Write dashboard options first. The final app write triggers AppDaemon reload.
        for path, document in zip(paths, documents):
            backup = path.with_suffix(path.suffix + '.before-presence-settings')
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
            temporary = path.with_suffix('.pending')
            temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
            temporary.chmod(0o600)
            os.replace(temporary, path)
    return {key: app.get(key) for key in ('presence_entities', 'travel_pause_radius_km',
            'travel_pause_after_hours', 'fallback_speed_kmh', 'travel_mode_enabled', 'observation_mode')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/conf'))
    parser.add_argument('--person', action='append')
    parser.add_argument('--radius-km', type=float)
    parser.add_argument('--outside-hours', type=float)
    parser.add_argument('--estimate-speed-kmh', type=float)
    args = parser.parse_args()
    print(json.dumps(configure(args.root, args.person, args.radius_km,
                               args.outside_hours, args.estimate_speed_kmh), ensure_ascii=False))
