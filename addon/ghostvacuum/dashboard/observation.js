(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = n => typeof n === 'number' && Number.isFinite(n);
  const minute = n => numeric(n) ? `${Math.round(n)} min` : 'Dauer unbekannt';
  const paths = {
    home:'<path d="m3 10 9-7 9 7M5 9v12h14V9M9 21v-7h6v7"/>',
    away:'<circle cx="13" cy="4" r="2"/><path d="m10 10 3-3 4 4h4M13 7l-2 8-5 6M11 15l6 6M10 10l-5 3"/>',
    travel:'<path d="M12 2v19M12 7 3 13v3l9-3 9 3v-3l-9-6M12 17l-4 4v1l4-1 4 1v-1l-4-4"/>',
    unknown:'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5M12 17h.01"/>'
  };
  const sourcePaths = {wifi:'<path d="M2 8.5a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a5.5 5.5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/>',mobile:'<path d="M4 20v-3M9 20v-7M14 20V9M19 20V4"/>'};
  const sourceLabels = {wifi:'Anwesenheit über WLAN',mobile:'Standort vom Handy (GPS)'};
  const labels = {home:'Zuhause',away:'Unterwegs',travel:'Auf Reisen',unknown:'Status unbekannt'};
  const dateLabel = value => {
    if (!value) return 'Nicht verfügbar';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Nicht verfügbar' : date.toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Berlin'});
  };
  function journeyMarkup(p) {
    const j = p.journey;
    if (!j) return '';
    const progress = j.progress || j.travel_progress;
    const active = j.steps[j.station];
    const dwellPercent = progress && progress.required > 0 ? Math.max(0, Math.min(100, progress.minutes / progress.required * 100)) : null;
    const dwellConfirmed = j.progress && progress.minutes >= progress.required;
    const detail = j.travel_progress ? j.note : dwellConfirmed || (progress && progress.minutes<=0) ? '' : progress ? `${progress.minutes} / ${progress.required} min` : j.note;
    return `<div class="presence-scale ${esc(j.tone)}" aria-label="Aufenthaltsstatus von ${esc(p.name)}"><ol class="scale-stations">${j.steps.map((step,index)=>`<li class="scale-segment ${j.station!==null&&index<=j.station?'filled':''} ${j.station===index?'active':''} ${index===2&&j.station===2&&(!j.progress||j.progress.minutes<=0)?'awaiting-dwell':''} ${j.station===index&&dwellPercent!==null?'dwell-segment':''}" ${j.station===index?'aria-current="step"':''} ${j.station===index&&dwellPercent!==null?`style="--dwell:${dwellPercent}%"`:''} title="${esc(step.label)}${step.hint?' · '+esc(step.hint):''}"><span class="sr-only">${esc(step.label)}</span>${j.station===index&&dwellPercent!==null?`<span class="segment-progress" role="progressbar" aria-label="${j.travel_progress?'Zeit bis Reisemodus':'Aufenthalt'} von ${esc(p.name)}" aria-valuemin="0" aria-valuemax="${esc(progress.required)}" aria-valuenow="${esc(progress.minutes)}"></span>`:''}</li>`).join('')}</ol><div class="scale-detail ${progress?'has-dwell':''}"><span class="scale-current">${esc(j.approaching && (j.station===2 || j.station===3) ? 'Auf dem Heimweg' : dwellConfirmed ? 'Aufenthalt bestätigt' : active?.label || 'Standort unbekannt')}</span>${detail?`<span class="scale-note">${esc(detail)}</span>`:''}</div></div>`;
  }
  let currentModel = null;
  let draggedRoom = null;
  function render(model) {
    currentModel = model;
    if (draggedRoom) return;
    const robot = model.robot;
    const locked = model.commands_locked;
    const enabled = model.automation_enabled;
    document.querySelector('.observation-label').textContent = locked ? 'Beobachtung · Automatik AUS · Gerätebefehle gesperrt' : `Automatik ${enabled ? 'AN' : 'AUS'} · Live-Betrieb`;
    $('plan-caption').textContent = locked ? 'Beobachtung · kein automatischer Reinigungsplan' : model.manual_order ? 'Manuelle Reihenfolge' : 'Automatisch nach Gewichtung & Fälligkeit';
    $('schedule-notice').textContent = model.schedule_notice || '';
    $('schedule-notice').hidden = !model.schedule_notice;
    $('schedule-notice').title = 'Automatische Reinigungsstarts sind aktuell gesperrt.';
    $('reset-order').hidden = !model.manual_order || locked;
    $('board-hint').textContent = locked ? 'Gerätebefehle und Änderungen gesperrt.' : enabled ? 'Raum starten oder laufende Reinigung abbrechen.' : 'Automatik in den Einstellungen einschalten.';
    $('mode-footer').textContent = locked ? 'Beobachtungsbetrieb · Live-Daten · keine Gerätebefehle' : 'Live-Betrieb · Gerätebefehle freigegeben';
    $('pause-cleaning').disabled = locked || !robot.cleaning;
    $('stop-cleaning').disabled = locked || !robot.cleaning;

    $('robot-name').textContent = `Jetzt · ${robot.name}`;
    $('robot-logo').textContent = robot.name;
    $('status').textContent = robot.title;
    $('status-sub').textContent = robot.subtitle;
    $('job-origin').textContent = robot.origin_label || `Automatik ${enabled ? 'AN' : 'AUS'}`;
    $('job-origin').classList.toggle('external-job', Boolean(robot.origin_label));
    $('job-origin').title = robot.origin_label ? 'Nicht von GhostVacuum gestartet. Die genaue Quelle ist unbekannt.' : locked ? 'Beobachtungsbetrieb' : 'Live-Betrieb';
    $('progress').style.width = numeric(robot.progress) ? `${robot.progress}%` : '0%';
    $('progress-label').textContent = robot.cleaning ? `${robot.active_name || 'Raum nicht gemeldet'} · ${numeric(robot.progress) ? robot.progress+' %' : 'Fortschritt unbekannt'}` : 'Kein aktiver Auftrag';
    $('remaining').textContent = robot.cleaning ? numeric(robot.remaining) ? minute(robot.remaining)+' übrig' : 'Restzeit unbekannt' : '';
    const metric = label => robot.metrics.find(m => m.label === label);
    const base = metric('Basis');
    const dust = metric('Staubkasten') || metric('Staubbeutel');
    const mop = metric('Mop');
    const water = metric('Festwasseranschluss');
    const stationInfo = [water ? 'Festwasser' : null, mop ? 'Mop '+mop.value : null].filter(Boolean).join(' · ') || 'Status nicht verfügbar';
    $('device').innerHTML = `<div><b>${esc(robot.battery)}</b><span>Akku</span></div><div><b>Station ${esc(base?.value || 'unbekannt')}</b><span>${esc(stationInfo)}</span></div><div><b>${esc(dust?.label || 'Staubbehälter')}</b><span>${esc(dust?.value || 'Füllstand unbekannt')}</span></div>`;
    const permission = model.cleaning_permission;
    $('permission-title').textContent = permission.title;
    $('permission-reason').textContent = permission.reason;
    $('permission-indicator').textContent = permission.ready ? '✓' : 'Ⅱ';
    document.querySelector('.cleaning-permission').classList.toggle('blocked', !permission.ready);
    $('presence-summary').textContent = model.presence_summary;
    $('people').innerHTML = model.people.map(p => `<div class="person-panel"><div class="person"><span class="person-icon ${esc(p.kind)}" role="img" aria-label="${esc(labels[p.kind])}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[p.kind] || paths.unknown}</svg></span><b>${esc(p.name)}</b>${sourcePaths[p.presence_source] ? `<span class="presence-source" role="img" aria-label="${esc(sourceLabels[p.presence_source])}" title="${esc(sourceLabels[p.presence_source])}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">${sourcePaths[p.presence_source]}</svg></span>` : ''}<div class="person-return"><span>${esc(p.journey?.eta || '')}</span></div></div>${journeyMarkup(p)}</div>`).join('') || '<span class="locked-note">Keine verlässlichen Personendaten</span>';
    const groups = [['active','Jetzt in Reinigung',locked ? 'Gerätestatus · Befehle gesperrt' : ''],['planned','Heute geplant',locked ? 'Im Beobachtungsbetrieb kein Plan' : 'Gewichtung & Fälligkeit'],['rest','Aktuell nicht geplant','Letzte Reinigung & nächste Fälligkeit']];
    $('room-board').innerHTML = groups.map(([group,title,hint]) => {
      const rooms = model.rooms.filter(r => r.group === group);
      let rank = 0;
      const empty = group === 'active' ? robot.cleaning ? 'Reinigung gemeldet · aktueller Raum unbekannt' : 'Kein Raum in Reinigung' : group === 'planned' ? 'Keine Räume geplant' : 'Keine weiteren Räume';
      const rows = rooms.map(r => {
        const detail = group === 'rest' ? `<span class="room-date"><small>Zuletzt gereinigt</small><span>${esc(dateLabel(r.last))}</span></span><span class="room-date"><small>Wieder fällig</small><span>${esc(dateLabel(r.next_due))}</span></span>` : group === 'active' ? esc(robot.cleaning ? 'Geräteauftrag' : '') : `<span class="planned-details"><span class="due-pill ${esc(r.due_badge?.level || 'neutral')}">${esc(r.due_badge?.label || 'Fällig')}</span><span class="room-interval">${numeric(r.interval_h) && r.interval_h > 0 ? (r.interval_h === 24 ? 'Täglich' : r.interval_h < 24 ? 'Alle '+r.interval_h.toLocaleString('de-DE')+' Std.' : 'Alle '+(r.interval_h/24).toLocaleString('de-DE')+' Tage') : 'Intervall unbekannt'}</span></span>`;
        return `<div class="board-row ${group === 'active' ? 'active' : ''} ${r.enabled === false ? 'inactive' : ''}" data-room="${esc(r.id)}" draggable="false" role="listitem"><span class="handle ${locked || !enabled || r.enabled !== true ? 'locked-handle' : 'drag-handle'}" draggable="false" aria-label="${esc(r.name)} verschieben" title="Zum Verschieben ziehen">⠿</span><span class="rank">${group==='active'?'◉':group==='planned'?String(++rank).padStart(2,'0'):'·'}</span><strong>${esc(r.name)}${r.enabled === false ? '<small class="inactive-label">Deaktiviert</small>' : ''}</strong><span class="row-detail ${group==='rest'?'room-dates':''}">${detail}</span><span class="row-badge"><small>${group==='active' && numeric(robot.remaining) ? 'Restzeit' : 'Reinigungsdauer'}</small><span>${esc(group==='active' && numeric(robot.remaining) ? minute(robot.remaining) : numeric(r.duration) && r.duration > 0 ? 'ca. '+minute(r.duration) : 'Noch unbekannt')}</span></span><span></span><button ${locked || (group !== 'active' && (!enabled || r.enabled !== true)) ? 'disabled' : ''} data-room-action="${group==='active'?'stop':'start'}" aria-label="${esc(r.name)} ${group==='active'?'abbrechen':'starten'}" title="${locked ? 'Im Beobachtungsbetrieb gesperrt' : group !== 'active' && !enabled ? 'Automatik zuerst einschalten' : r.enabled === false ? 'Raum deaktiviert' : ''}">${group==='active'?'■':'▶'}</button></div>`;
      }).join('');
      return `<section class="board-group" data-group="${group}" aria-label="${title}"><div class="group-heading"><h3>${title}<span>${group==='active'?rooms.length+'/1':rooms.length}</span></h3><small>${hint}</small></div><div class="drop-list" role="list">${rows || `<div class="empty-slot">${empty}</div>`}</div></section>`;
    }).join('');
    $('room-count').textContent = `${model.rooms.length} Räume · Einstellungen separat`;
    $('last-action').textContent = model.recent_runs.length ? 'Letzter Lauf: '+dateLabel(model.recent_runs[0].finished_at || model.recent_runs[0].ended_at) : 'Keine Raumhistorie verfügbar';
    $('connection').textContent = 'Live · '+new Date(model.updated_at).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Europe/Berlin'});
    $('connection').classList.remove('connection-error');
  }
  let commandPending = false;
  async function command(path, payload = {}) {
    if (commandPending) return false;
    commandPending = true;
    try {
      const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Befehl fehlgeschlagen');
      $('connection').textContent = 'Anforderung gesendet · wartet auf Bestätigung';
      return true;
    } catch (error) { $('connection').textContent = error.message; return false; }
    finally {commandPending = false;}
  }
  $('pause-cleaning').addEventListener('click',() => command('api/pause_cleaning'));
  $('stop-cleaning').addEventListener('click',() => command('api/stop_cleaning'));
  $('room-board').addEventListener('click',event => {
    const button = event.target.closest('[data-room-action]');
    if (!button || button.disabled) return;
    command(button.dataset.roomAction === 'stop' ? 'api/stop_cleaning' : 'api/start_room',{room_key:button.closest('[data-room]').dataset.room});
  });
  const board = $('room-board');
  let dropTarget = null;
  function clearDrop() {
    board.querySelectorAll('.drop-over,.insert-before,.insert-after').forEach(e => e.classList.remove('drop-over','insert-before','insert-after'));
    dropTarget = null;
  }
  function locateDrop(element, y) {
    clearDrop();
    const group = element?.closest('[data-group]');
    if (!group || !['active','planned'].includes(group.dataset.group)) return;
    const rows = [...group.querySelectorAll('[data-room]')].filter(row => row.dataset.room !== draggedRoom);
    const before = rows.find(row => y < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2);
    dropTarget = {room_key:draggedRoom, target:group.dataset.group, before:before?.dataset.room || null};
    group.querySelector('.drop-list').classList.add('drop-over');
    if (group.dataset.group === 'planned' && before) before.classList.add('insert-before');
  }
  async function finishDrop() {
    const target = dropTarget;
    draggedRoom = null;
    clearDrop();
    if (target && await command('api/move_room', target)) {
      await refresh(true);
    } else if (currentModel) render(currentModel);
  }
  board.addEventListener('dragstart', event => {
    const handle = event.target.closest('.drag-handle');
    if (!handle || commandPending) {event.preventDefault();return;}
    draggedRoom = handle.closest('[data-room]').dataset.room;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', draggedRoom);
    event.dataTransfer.setDragImage(handle.closest('[data-room]'), 15, 15);
  });
  board.addEventListener('dragover', event => {
    if (!draggedRoom) return;
    event.preventDefault();
    locateDrop(event.target, event.clientY);
    event.dataTransfer.dropEffect = dropTarget ? 'move' : 'none';
  });
  board.addEventListener('drop', event => {
    if (!draggedRoom) return;
    event.preventDefault();
    locateDrop(event.target,event.clientY);
    finishDrop();
  });
  board.addEventListener('dragend', () => {
    draggedRoom = null;clearDrop();
    if (currentModel && !commandPending) render(currentModel);
  });
  let touchPointer = null;
  let pointerStart = null;
  let pointerMoved = false;
  board.addEventListener('pointerdown', event => {
    const handle = event.target.closest('.drag-handle');
    if (!handle || commandPending) return;
    event.preventDefault();
    touchPointer = event.pointerId;
    pointerStart = [event.clientX,event.clientY];
    pointerMoved = false;
    draggedRoom = handle.closest('[data-room]').dataset.room;
    handle.setPointerCapture(event.pointerId);
  });
  board.addEventListener('pointermove', event => {
    if (event.pointerId !== touchPointer) return;
    event.preventDefault();
    if (!pointerMoved && Math.hypot(event.clientX-pointerStart[0],event.clientY-pointerStart[1]) < 6) return;
    pointerMoved = true;
    locateDrop(document.elementFromPoint(event.clientX,event.clientY),event.clientY);
  });
  board.addEventListener('pointerup', event => {
    if (event.pointerId !== touchPointer) return;
    touchPointer = null;
    if (pointerMoved) locateDrop(document.elementFromPoint(event.clientX,event.clientY),event.clientY);
    else clearDrop();
    finishDrop();
  });
  board.addEventListener('pointercancel', () => {touchPointer=null;draggedRoom=null;clearDrop();});
  $('reset-order').addEventListener('click', async () => {
    if (await command('api/reset_room_order')) await refresh(true);
  });
  render(JSON.parse($('live-initial').textContent));
  async function refresh(once = false) {
    try {
      const response = await fetch('api/dashboard',{cache:'no-store',signal:AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error('Verbindung fehlgeschlagen');
      render(await response.json());
    } catch {
      $('connection').textContent = 'Verbindung unterbrochen · letzte Daten angezeigt';
      $('connection').classList.add('connection-error');
    } finally {
      if (!once) setTimeout(refresh,10000);
    }
  }
  setTimeout(refresh,10000);
})();
