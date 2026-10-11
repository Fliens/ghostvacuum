(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = n => typeof n === 'number' && Number.isFinite(n);
  const minute = n => numeric(n) ? `${Math.round(n)} min` : 'Dauer unbekannt';
  const personIcons = new Set(['home','away','travel','unknown']);
  const sourceIcons = new Set(['wifi','mobile']);
  const sourceLabels = {wifi:'Anwesenheit über WLAN',mobile:'Standort vom Handy (GPS)'};
  const labels = {home:'Zuhause',away:'Unterwegs',travel:'Auf Reisen',unknown:'Status unbekannt'};
  const dateLabel = (value, dateOnly = false) => {
    if (!value) return 'Nicht verfügbar';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Nicht verfügbar';
    const label = date.toLocaleString('de-DE',{day:'2-digit',month:'2-digit',...(dateOnly ? {} : {hour:'2-digit',minute:'2-digit'}),timeZone:'Europe/Berlin'});
    const weekday = date.toLocaleDateString('de-DE',{weekday:'short',timeZone:'Europe/Berlin'}).replace(/\.$/,'');
    return dateOnly ? `${weekday}, ${label}` : label;
  };
  function resetPlanPreview(model) {
    const rooms = model.rooms.map(r => r.group === 'active' ? {...r} : {...r,
      group:r.automatic_due?'planned':'rest', plan_override:false, manual_queued:false,
      suspended_today:false, order_changed:false});
    const groupRank = {active:0,planned:1,rest:2};
    rooms.sort((a,b) => groupRank[a.group]-groupRank[b.group] ||
      (a.group==='planned' ? (b.priority || 0)-(a.priority || 0) :
       a.group==='rest' ? Number(b.enabled===true)-Number(a.enabled===true) : 0) ||
      (a.default_rank ?? 0)-(b.default_rank ?? 0));
    return {...model,rooms,manual_order:false,rest_manual_order:false};
  }
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
    $('schedule-notice').textContent = model.schedule_notice || '';
    $('schedule-notice').hidden = !model.schedule_notice;
    $('schedule-notice').title = 'Automatische Reinigungsstarts sind aktuell gesperrt.';
    $('reset-order').hidden = !model.manual_order || locked;
    $('pause-cleaning').disabled = locked || !robot.cleaning;
    $('stop-cleaning').disabled = locked || !robot.cleaning;

    $('robot-name').textContent = robot.name;
    $('robot-logo').textContent = robot.name;
    $('status').textContent = robot.title;
    $('status-sub').textContent = robot.subtitle;
    $('job-origin').textContent = robot.origin_label || `Automatik ${enabled ? 'AN' : 'AUS'}`;
    $('job-origin').classList.toggle('external-job', Boolean(robot.origin_label));
    $('job-origin').title = robot.origin_label === 'Manuell gestartet' ? 'Manuell gestartet. Wird von der Automatik nicht beendet.' : robot.origin_label ? 'Nicht von GhostVacuum gestartet. Die genaue Quelle ist unbekannt.' : locked ? 'Beobachtungsbetrieb' : 'Live-Betrieb';
    $('progress').style.width = numeric(robot.progress) ? `${robot.progress}%` : '0%';
    $('progress-label').textContent = robot.cleaning ? `${robot.progress_scope === 'special' ? robot.progress_label : robot.active_name || 'Raum nicht gemeldet'} · ${numeric(robot.progress) ? robot.progress+' %' : 'Fortschritt unbekannt'}` : 'Kein aktiver Auftrag';
    $('remaining').textContent = robot.cleaning && robot.progress_scope === 'special' ? (numeric(robot.room_progress) ? 'Raumfortschritt '+robot.room_progress+' %' : 'Raumfortschritt unbekannt') : robot.cleaning ? numeric(robot.remaining) ? minute(robot.remaining)+' übrig' : 'Restzeit unbekannt' : '';
    const metric = label => robot.metrics.find(m => m.label === label);
    const base = metric('Basis');
    const dust = metric('Staubkasten') || metric('Staubbeutel');
    const mop = metric('Mop');
    const water = metric('Festwasseranschluss');
    const stationInfo = [water ? 'Festwasser' : null, mop ? 'Mop '+mop.value : null].filter(Boolean).join(' · ') || 'Status nicht verfügbar';
    $('device').innerHTML = `<div><b>${esc(robot.battery)}</b><span>Akku</span></div><div><b>Station ${esc(base?.value || 'unbekannt')}</b><span>${esc(stationInfo)}</span></div><div><b>${esc(dust?.label || 'Staubbehälter')}</b><span>${esc(dust?.value || 'Füllstand unbekannt')}</span></div>`;
    const dryingIcons = {
      mops: '<path fill="currentColor" stroke="none" fill-rule="evenodd" d="M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM14 12a2 2 0 1 0-4 0 2 2 0 0 0 4 0Z"/>',
      bag: '<path transform="translate(0 -1)" d="M9 5h6l-1 3c4 2 6 5 6 9 0 3-3 4-8 4s-8-1-8-4c0-4 2-7 6-9zM9 8h6"/>'
    };
    $('drying-status').innerHTML = '<span class="drying-heading">Trocknung</span>' + (robot.drying || []).map(d => `<div class="drying-item ${d.active?'is-active':''}" aria-label="${esc(d.label)} trocknen: ${esc(d.state)}${numeric(d.minutes)?', noch '+d.minutes+' Minuten':''}"><svg viewBox="-3 -3 30 30" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${dryingIcons[d.kind] || dryingIcons.bag}<circle class="drying-spinner" cx="12" cy="12" r="13"/></svg><b>${d.active ? numeric(d.minutes) ? (d.minutes <= 60 ? d.minutes+' min' : Math.floor(d.minutes/60)+' h '+(d.minutes%60)+' min') : 'Restzeit unbekannt' : esc(d.state)}</b></div>`).join('');
    const permission = model.cleaning_permission;
    $('permission-title').textContent = permission.title;
    $('permission-reason').textContent = permission.reason;
    document.querySelector('.cleaning-permission').classList.toggle('blocked', !permission.ready);
    $('presence-summary').textContent = model.presence_summary;
    $('people').innerHTML = model.people.map(p => `<div class="person-panel"><div class="person"><span class="person-icon ${esc(p.kind)}" role="img" aria-label="${esc(labels[p.kind])}"><span class="im-icon im-${personIcons.has(p.kind)?p.kind:'unknown'}" aria-hidden="true"></span></span><b>${esc(p.name)}</b>${sourceIcons.has(p.presence_source) ? `<span class="presence-source" role="img" aria-label="${esc(sourceLabels[p.presence_source])}" title="${esc(sourceLabels[p.presence_source])}"><span class="im-icon im-${p.presence_source}" aria-hidden="true"></span></span>` : ''}<div class="person-return"><span>${esc(p.journey?.eta || '')}</span></div></div>${journeyMarkup(p)}</div>`).join('') || '<span class="locked-note">Keine verlässlichen Personendaten</span>';
    const groups = [['active','Jetzt in Reinigung',locked ? 'Gerätestatus · Befehle gesperrt' : ''],['planned','Heute geplant',locked ? 'Im Beobachtungsbetrieb kein Plan' : 'Gewichtung & Fälligkeit'],['rest','Aktuell nicht geplant','Letzte Reinigung & nächste Fälligkeit']];
    $('room-board').innerHTML = groups.map(([group,title,hint]) => {
      const rooms = model.rooms.filter(r => r.group === group);
      let rank = 0;
      const empty = group === 'active' ? robot.cleaning ? 'Reinigung gemeldet · aktueller Raum unbekannt' : 'Kein Raum in Reinigung' : group === 'planned' ? 'Keine Räume geplant' : 'Keine weiteren Räume';
      const rowByRoom = new Map();
      const rows = rooms.map(r => {
        const detail = group === 'rest' ? `<span class="room-date"><small>Zuletzt gereinigt</small><span>${esc(dateLabel(r.last))}</span></span><span class="room-date"><small>Wieder fällig</small><span>${esc(dateLabel(r.next_due, true))}</span></span>` : group === 'active' ? esc(robot.cleaning ? [robot.cleaning_mode, numeric(robot.progress) ? robot.progress+' %' : null].filter(Boolean).join(' · ') : '') : `<span class="planned-details"><span class="planned-meta"><span class="room-interval">${numeric(r.interval_h) && r.interval_h > 0 ? (r.interval_h === 24 ? 'Täglich' : r.interval_h < 24 ? 'Alle '+r.interval_h.toLocaleString('de-DE')+' Std.' : 'Alle '+(r.interval_h/24).toLocaleString('de-DE')+' Tage') : 'Intervall unbekannt'}</span></span><span class="room-date planned-last"><small>Zuletzt gereinigt</small><span>${esc(dateLabel(r.last))}</span></span></span>`;
        const queueAction = (mode, label) => {
          const selected = group === 'planned' && (mode === 'manual' ? r.manual_queued : !r.manual_queued);
          return `<button class="queue-action ${selected?'remove-plan-action':''}" data-queue-mode="${mode}" data-room-action="${selected?'remove':mode}" ${locked || r.enabled!==true?'disabled':''} aria-label="${esc(r.name)} ${selected?'aus Planung entfernen':mode==='manual'?'manuell vormerken':'automatisch einplanen'}" title="${selected?'Aus Planung entfernen · für heute aussetzen':label+' einplanen'}"><span class="${selected?'remove-plan-icon':'im-icon im-queue'}" aria-hidden="true">${selected?'×':''}</span><small>${selected?'Entfernen':label}</small></button>`;
        };
        const changes = [group==='planned' && r.plan_override ? (r.automatic_due?'Planung manuell angepasst':'Manuell vorgemerkt') : null, r.order_changed?'Reihenfolge geändert':null, r.suspended_today?'Heute ausgesetzt':null].filter(Boolean).join(' · ');
        const duePill = group === 'planned' ? `<span class="due-pill ${esc(r.due_badge?.level || 'neutral')}">${esc(r.due_badge?.label || 'Fällig')}</span>` : '';
        const duration = r.enabled === true && numeric(r.duration) && r.duration > 0 ? `<span class="room-duration" title="Reinigungsdauer" aria-label="Reinigungsdauer ca. ${esc(minute(r.duration))}">(ca. ${esc(minute(r.duration))})</span>` : '';
        const row = `<div class="board-row ${group === 'active' ? 'active' : ''} ${r.enabled === false ? 'inactive' : ''}" data-room="${esc(r.id)}" draggable="false" role="listitem"><span class="handle ${locked || r.enabled !== true ? 'locked-handle' : 'drag-handle'}" draggable="false" aria-label="${esc(r.name)} verschieben" title="Zum Verschieben ziehen">⠿</span><span class="rank">${group==='active'?'◉':group==='planned'?String(++rank).padStart(2,'0'):'·'}</span><strong><span class="room-name-line"><span>${esc(r.name)}${changes?`<span class="room-change-dot ${r.suspended_today?'skipped':''}" role="img" aria-label="${esc(changes)}" title="${esc(changes)}"></span>`:''}</span>${duePill}</span>${duration}${r.enabled === false ? '<small class="inactive-label">Deaktiviert</small>' : ''}</strong><span class="row-detail ${group==='rest'?'room-dates':''}">${detail}</span>${group==='active' ? `<span class="row-badge"><small>Restzeit</small><span>${esc(numeric(robot.remaining) ? minute(robot.remaining) : 'Unbekannt')}</span></span>` : ''}<div class="room-actions" role="group" aria-label="Aktionen für ${esc(r.name)}">${group==='active' ? `<button ${locked?'disabled':''} data-room-action="stop" aria-label="${esc(r.name)} abbrechen" title="Reinigung abbrechen"><span class="im-icon im-stop" aria-hidden="true"></span><small>Stoppen</small></button>` : `${queueAction('manual','Manuell')}${queueAction('auto','Automatisch')}<button ${locked || r.enabled!==true?'disabled':''} data-room-action="start" aria-label="${esc(r.name)} jetzt starten" title="Jetzt starten"><span class="im-icon im-start" aria-hidden="true"></span><small>Starten</small></button>`}</div></div>`;
        rowByRoom.set(r.id,row);
        return row;
      }).join('');
      const plannedSections = group==='planned' ? [['manual','Manuell',true],['auto','Automatisch',false]].map(([mode,label,manual]) => {
        const items = rooms.filter(r=>Boolean(r.manual_queued)===manual);
        return `<section class="planned-subgroup" data-drop-target="planned_${mode}" aria-label="${label} geplant"><h4>${label}</h4><div class="drop-list" role="list">${items.map(r=>rowByRoom.get(r.id)).join('') || '<div class="plan-empty">Keine Räume</div>'}</div></section>`;
      }).join('') : '';
      return `<section class="board-group" data-group="${group}" aria-label="${title}"><div class="group-heading"><h3>${title}${group==='active'?'':group==='rest'?`<span class="room-count-enabled">${rooms.filter(r=>r.enabled===true).length} aktiv</span><span class="room-count-disabled">${rooms.filter(r=>r.enabled===false).length} deaktiviert</span>${rooms.some(r=>r.enabled==null)?`<span>${rooms.filter(r=>r.enabled==null).length} unbekannt</span>`:''}`:`<span>${rooms.length}</span>`}</h3><small class="${group==='rest' && model.rest_manual_order?'manual-order-hint':''}">${group==='rest' && model.rest_manual_order ? 'Reihenfolge manuell geändert' : hint}</small></div><div class="drop-list main-drop-list" role="list">${plannedSections || rows || `<div class="empty-slot">${empty}</div>`}</div></section>`;
    }).join('');
    showConnection();
  }
  function showConnection(message = '', error = false) {
    const status = $('connection');
    status.textContent = message;
    status.classList.toggle('connection-error', error);
    status.parentElement.hidden = !message;
  }
  let commandPending = false;
  let stateRevision = 0;
  async function command(path, payload = {}) {
    if (commandPending) return false;
    commandPending = true;
    stateRevision++;
    try {
      const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Befehl fehlgeschlagen');
      if (result.dashboard) render(result.dashboard);
      if (!result.dashboard) showConnection('Anforderung gesendet · wartet auf Bestätigung');
      return true;
    } catch (error) { showConnection(error.message, true); return false; }
    finally {commandPending = false;}
  }
  $('pause-cleaning').addEventListener('click',() => command('api/pause_cleaning'));
  $('stop-cleaning').addEventListener('click',() => command('api/stop_cleaning'));
  $('room-board').addEventListener('click',event => {
    const clear = event.target.closest('[data-clear-manual]');
    if (clear && !clear.disabled) {
      command('api/move_room',{room_key:clear.closest('[data-room]').dataset.room,target:'planned_auto',before:(()=>{const rooms=currentModel.rooms.filter(r=>r.group==='planned');return rooms[rooms.findIndex(r=>r.id===clear.closest('[data-room]').dataset.room)+1]?.id || null;})()}).then(ok=>{if(ok) refresh(true);});
      return;
    }
    const button = event.target.closest('[data-room-action]');
    if (!button || button.disabled) return;
    const action = button.dataset.roomAction;
    const room_key = button.closest('[data-room]').dataset.room;
    const target = {start:'active',manual:'planned_manual',auto:'planned_auto',remove:'rest'}[action];
    const planned = currentModel.rooms.filter(r=>r.group==='planned');
    const index = planned.findIndex(r=>r.id===room_key);
    const payload = {room_key,target};
    if (action !== 'start' && index >= 0) payload.before = planned[index+1]?.id || null;
    command(action === 'stop' ? 'api/stop_cleaning' : 'api/move_room', action === 'stop' ? {} : payload).then(ok=>{if(ok) refresh(true);});
  });
  const board = $('room-board');
  let dropTarget = null;
  function clearDrop() {
    board.querySelectorAll('.drop-over,.insert-before,.insert-after').forEach(e => e.classList.remove('drop-over','insert-before','insert-after'));
    dropTarget = null;
  }
  function appendPlanDrop(source, target) {
    if (!['planned','planned_manual','planned_auto'].includes(target)) return false;
    return source?.group !== 'planned' ||
      (target !== 'planned' && Boolean(source.manual_queued) !== (target === 'planned_manual'));
  }
  function locateDrop(element, y) {
    clearDrop();
    const group = element?.closest('[data-drop-target]') || element?.closest('[data-group]');
    const target = group?.dataset.dropTarget || group?.dataset.group;
    if (!group || !['active','planned_manual','planned_auto','rest'].includes(target)) return;
    const allRows = [...group.querySelectorAll('[data-room]')];
    if (target === 'rest') {
      const inactive = allRows.find(row => currentModel.rooms.find(r=>r.id===row.dataset.room)?.enabled !== true);
      if (inactive && y >= inactive.getBoundingClientRect().top) return;
    }
    const rows = allRows.filter(row => row.dataset.room !== draggedRoom &&
      (target !== 'rest' || currentModel.rooms.find(r=>r.id===row.dataset.room)?.enabled === true));
    const source = currentModel.rooms.find(r=>r.id===draggedRoom);
    const append = appendPlanDrop(source, target) || (target === 'rest' && source?.group !== 'rest');
    const before = append ? null : rows.find(row => y < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2);
    dropTarget = {room_key:draggedRoom, target, before:before?.dataset.room || null};
    group.querySelector('.drop-list').classList.add('drop-over');
    if (target !== 'active' && before) before.classList.add('insert-before');
  }
  async function finishDrop() {
    const target = dropTarget;
    draggedRoom = null;
    board.classList.remove('choosing-plan');
    clearDrop();
    if (target && await command('api/move_room', target)) {
      await refresh(true);
    } else if (currentModel) render(currentModel);
  }
  board.addEventListener('dragstart', event => {
    const handle = event.target.closest('.drag-handle');
    if (!handle || commandPending) {event.preventDefault();return;}
    draggedRoom = handle.closest('[data-room]').dataset.room;
    board.classList.add('choosing-plan');
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
    draggedRoom = null;board.classList.remove('choosing-plan');clearDrop();
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
    board.classList.add('choosing-plan');
    locateDrop(document.elementFromPoint(event.clientX,event.clientY),event.clientY);
  });
  board.addEventListener('pointerup', event => {
    if (event.pointerId !== touchPointer) return;
    touchPointer = null;
    if (pointerMoved) locateDrop(document.elementFromPoint(event.clientX,event.clientY),event.clientY);
    else clearDrop();
    finishDrop();
  });
  board.addEventListener('pointercancel', () => {touchPointer=null;draggedRoom=null;board.classList.remove('choosing-plan');clearDrop();});
  $('reset-order').addEventListener('click', async () => {
    if (commandPending || !currentModel) return;
    const previous = currentModel;
    render(resetPlanPreview(previous));
    if (!await command('api/reset_room_order')) {
      const error = $('connection').textContent;
      render(previous);
      showConnection(error, true);
    }
  });
  render(JSON.parse($('live-initial').textContent));
  async function refresh(once = false) {
    const revision = stateRevision;
    try {
      const response = await fetch('api/dashboard',{cache:'no-store',signal:AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error('Verbindung fehlgeschlagen');
      const model = await response.json();
      if (revision === stateRevision && !commandPending && !draggedRoom) render(model);
    } catch {
      showConnection('Verbindung unterbrochen · letzte Daten angezeigt', true);
    } finally {
      if (!once) setTimeout(refresh,10000);
    }
  }
  setTimeout(refresh,10000);
})();
