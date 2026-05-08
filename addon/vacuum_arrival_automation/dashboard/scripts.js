(function () {
  const list = document.querySelector("[data-room-list]");
  if (list) {
    let dragged = null;
    let touchClone = null;
    let touchStartY = 0;
    let touchOffsetY = 0;
    const placeholder = document.createElement("div");
    placeholder.className = "room-placeholder";

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await response.text());
    }

    function clearDropTargets() {
      list.querySelectorAll(".drop-target").forEach((node) => node.classList.remove("drop-target"));
    }

    function clearPlaceholder() {
      if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
    }

    function updateRanks() {
      const activeSection = list.querySelector('.room-section[data-room-section-kind="active"]');
      const dueSection = list.querySelector('.room-section[data-room-section-kind="due"]');
      const laterSection = list.querySelector('.room-section[data-room-section-kind="later"]');
      const returnWindow = parseInt(list.dataset.returnWindow, 10) || 0;

      function collectRows(startElement, endElement) {
        const rows = [];
        let current = startElement?.nextElementSibling;
        while (current && current !== endElement) {
          if (current === placeholder && dragged) rows.push(dragged);
          else if (current.classList.contains("room-row")) {
            if (current !== dragged || !placeholder.parentNode) rows.push(current);
          }
          current = current.nextElementSibling;
        }
        return rows;
      }

      const activeRows = collectRows(activeSection, dueSection);
      const dueRows = collectRows(dueSection, laterSection);
      const laterRows = collectRows(laterSection, null);

      let cumulativeDuration = 0;
      activeRows.forEach((row) => {
        cumulativeDuration += parseInt(row.dataset.duration, 10) || 0;
      });

      const allQueuedRows = [...dueRows, ...laterRows];
      allQueuedRows.forEach((row) => {
        const duration = parseInt(row.dataset.duration, 10) || 0;
        cumulativeDuration += duration;
        const fits = cumulativeDuration <= returnWindow;
        row.classList.toggle("fits", fits && !row.classList.contains("next"));
      });

      dueRows.forEach((row, index) => {
        const rank = row.querySelector(".room-inline-rank");
        if (rank) rank.textContent = `${index + 1}.`;
        row.classList.toggle("next", index === 0);
        if (index === 0) row.classList.remove("fits");
      });

      const laterStartIndex = dueRows.length;
      laterRows.forEach((row, index) => {
        const rank = row.querySelector(".room-inline-rank");
        if (rank) rank.textContent = `${laterStartIndex + index + 1}.`;
        row.classList.remove("next");
      });
    }

    function sectionKindFor(node) {
      return node?.dataset?.roomSectionKind || "";
    }

    function sectionMarkerForKind(kind) {
      return list.querySelector(`.room-section[data-room-section-kind="${kind}"]`);
    }

    async function applySectionAction(roomKey, kind) {
      if (kind === "active") {
        await postJson("/api/start_room", { room_key: roomKey });
        window.location.reload();
        return;
      }
      if (kind === "due") {
        await postJson("/api/set_room_due", { room_key: roomKey, due: true });
        window.location.reload();
        return;
      }
      if (kind === "later") {
        await postJson("/api/set_room_due", { room_key: roomKey, due: false });
        window.location.reload();
      }
    }

    function getTargetAtPoint(clientX, clientY) {
      const elements = document.elementsFromPoint(clientX, clientY);
      for (const el of elements) {
        if (el === touchClone || el === placeholder) continue;
        const row = el.closest("[data-room-key]");
        if (row && row !== dragged) return { row, section: null };
        const section = el.closest("[data-room-section-kind]");
        if (section) return { row: null, section };
      }
      return { row: null, section: null };
    }

    function handleMove(clientY) {
      if (!dragged) return;

      const { row: target, section: sectionTarget } = getTargetAtPoint(list.getBoundingClientRect().left + 50, clientY);
      const sourceKind = sectionKindFor(dragged);

      if (target) {
        const targetKind = sectionKindFor(target);

        if (targetKind && targetKind !== sourceKind) {
          clearPlaceholder();
          clearDropTargets();
          sectionMarkerForKind(targetKind)?.classList.add("drop-target");
          return;
        }

        clearDropTargets();
        const rect = target.getBoundingClientRect();
        const insertAfter = clientY > rect.top + rect.height / 2;
        const referenceNode = insertAfter ? target.nextSibling : target;

        if (referenceNode === dragged || referenceNode === dragged?.nextSibling) return;
        if (referenceNode === placeholder) return;
        list.insertBefore(placeholder, referenceNode);
        updateRanks();
        return;
      }

      if (sectionTarget) {
        const targetKind = sectionKindFor(sectionTarget);
        if (!targetKind || targetKind === sourceKind) return;
        clearPlaceholder();
        clearDropTargets();
        sectionTarget.classList.add("drop-target");
      }
    }

    async function handleDrop() {
      if (!dragged) return;

      const targetSection = list.querySelector(".drop-target[data-room-section-kind]");
      const sourceKind = sectionKindFor(dragged);
      const targetKind = targetSection ? sectionKindFor(targetSection) : null;

      if (targetKind && targetKind !== sourceKind) {
        clearDropTargets();
        clearPlaceholder();
        dragged.classList.remove("dragging");
        const roomKey = dragged.dataset.roomKey;
        dragged = null;
        try {
          await applySectionAction(roomKey, targetKind);
        } catch (error) {
          window.location.reload();
        }
        return;
      }

      if (!placeholder.parentNode) return;
      if (placeholder.previousSibling === dragged || placeholder.nextSibling === dragged) {
        clearDropTargets();
        clearPlaceholder();
        dragged.classList.remove("dragging");
        dragged = null;
        return;
      }

      list.insertBefore(dragged, placeholder);
      clearDropTargets();
      clearPlaceholder();
      updateRanks();

      const roomKey = dragged.dataset.roomKey;
      dragged.classList.remove("dragging");
      dragged = null;

      try {
        await postJson("/api/prioritize_room", { room_key: roomKey });
      } catch (error) {
        window.location.reload();
      }
    }

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-room-action]");
      if (!button) return;

      const action = button.dataset.roomAction;
      const roomKey = button.dataset.roomKey;

      try {
        if (action === "stop") {
          await postJson("/api/stop_cleaning", {});
        } else if (action === "start" && roomKey) {
          await postJson("/api/start_room", { room_key: roomKey });
        } else if (action === "due" && roomKey) {
          await postJson("/api/set_room_due", { room_key: roomKey, due: true });
        }
        window.location.reload();
      } catch (error) {
        window.location.reload();
      }
    });

    list.addEventListener("dragstart", (event) => {
      const row = event.target.closest("[data-room-key]");
      if (!row) return;
      dragged = row;
      row.classList.add("dragging");
      row.after(placeholder);
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", row.dataset.roomKey);
    });

    list.addEventListener("dragend", () => {
      list.querySelectorAll(".dragging").forEach((row) => row.classList.remove("dragging"));
      clearDropTargets();
      clearPlaceholder();
      dragged = null;
    });

    list.addEventListener("dragover", (event) => {
      if (!dragged) return;
      event.preventDefault();
      handleMove(event.clientY);
    });

    list.addEventListener("drop", (event) => {
      event.preventDefault();
      handleDrop();
    });

    list.addEventListener("touchstart", (event) => {
      const row = event.target.closest("[data-room-key]");
      if (!row) return;

      const touch = event.touches[0];
      const rect = row.getBoundingClientRect();
      touchStartY = touch.clientY;
      touchOffsetY = touch.clientY - rect.top;

      dragged = row;
      row.classList.add("dragging");
      row.after(placeholder);

      touchClone = row.cloneNode(true);
      touchClone.classList.add("touch-clone");
      touchClone.style.cssText = `
          position: fixed;
          left: ${rect.left}px;
          top: ${rect.top}px;
          width: ${rect.width}px;
          z-index: 1000;
          pointer-events: none;
          opacity: 0.9;
          transform: scale(1.02);
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
        `;
      document.body.appendChild(touchClone);
    }, { passive: true });

    list.addEventListener("touchmove", (event) => {
      if (!dragged || !touchClone) return;
      event.preventDefault();

      const touch = event.touches[0];
      touchClone.style.top = `${touch.clientY - touchOffsetY}px`;
      handleMove(touch.clientY);
    }, { passive: false });

    list.addEventListener("touchend", () => {
      if (touchClone) {
        touchClone.remove();
        touchClone = null;
      }
      handleDrop();
    });

    list.addEventListener("touchcancel", () => {
      if (touchClone) {
        touchClone.remove();
        touchClone = null;
      }
      if (dragged) {
        dragged.classList.remove("dragging");
        dragged = null;
      }
      clearDropTargets();
      clearPlaceholder();
    });
  }

  // State machine polling and apply logic adapted to pills/target
  (function () {
    const root = document.querySelector("[data-state-machine]");
    if (!root) return;

    function stateValue(state, fallback = "-") {
      if (!state || typeof state !== "object") return fallback;
      const value = state.state;
      if (value === undefined || value === null || value === "" || value === "unknown" || value === "unavailable") return fallback;
      return String(value);
    }

    function personIsHome(person) {
      return ["home", "zuhause"].includes(String(person?.state || "").toLowerCase());
    }

    function activeRoomLabel(summary) {
      const active = stateValue(summary?.states?.sensors?.active_room, "");
      return ["", "-", "keine", "keiner", "none"].includes(active.toLowerCase()) ? "" : active;
    }

    function orderedRooms(summary) {
      const active = activeRoomLabel(summary);
      const queue = Array.isArray(summary?.status?.room_queue) ? summary.status.room_queue : [];
      return queue
        .filter((room) => String(room.room || "") !== active)
        .sort((a, b) => {
          const aEnabled = a.enabled !== false;
          const bEnabled = b.enabled !== false;
          if (aEnabled !== bEnabled) return aEnabled ? -1 : 1;
          if (Boolean(a.fits_now) !== Boolean(b.fits_now)) return a.fits_now ? -1 : 1;
          return numberValue(b.priority, 0) - numberValue(a.priority, 0);
        });
    }

    function deriveContext(summary) {
      const sensors = summary?.states?.sensors || {};
      const globalStates = summary?.states?.global || {};
      const status = summary?.status || {};
      const statusKey = stateValue(sensors.status, "Warten").toLowerCase();
      const activeRoom = activeRoomLabel(summary);
      const nextRoom = orderedRooms(summary)[0] || null;
      const enabled = stateValue(globalStates.enabled, status.enabled || "on").toLowerCase();
      const people = Array.isArray(status.presence_summary) ? status.presence_summary : [];
      const homePeople = people.filter(personIsHome);

      const automation_active = enabled !== "off";
      const nobody_home = homePeople.length === 0;
      const travel_inactive = !status.travel_mode_active && !statusKey.includes("reisemodus");
      const manual_inactive = !activeRoom;

      return {
        pills: {
          automation_active,
          nobody_home,
          travel_inactive,
          manual_inactive,
        },
      };
    }

    function applyContext(context) {
      const pills = context.pills || {};
      Object.entries(pills).forEach(([key, ok]) => {
        const el = root.querySelector(`[data-pill-key="${key}"]`);
        if (!el) return;
        el.classList.toggle("ok", Boolean(ok));
      });
      const allOk = Object.values(pills).every(Boolean);
      const cleaning = root.querySelector("[data-cleaning-node]");
      if (cleaning) cleaning.classList.toggle("ok", allOk);
    }

    async function refresh() {
      try {
        const resp = await fetch("/api/summary", { headers: { Accept: "application/json" } });
        if (!resp.ok) return;
        applyContext(deriveContext(await resp.json()));
      } catch (e) {
        // ignore
      }
    }

    refresh();
    setInterval(refresh, 3000);
  })();
})();
