// os2slice print panel: follows Onshape's selection (docs/ONSHAPE_API.md).
// Only reads messages from the configured Onshape origin; writes text via textContent.
// Several selected parts = one multi-material object, each part with its own filament.
"use strict";
(() => {
  const root = document.getElementById("panel");
  const ONSHAPE = root.dataset.onshape;
  const ids = JSON.parse(root.dataset.ids);
  const parts = JSON.parse(root.dataset.parts);
  const form = document.getElementById("printform");
  const orient = form.elements.orient;
  const faceOpt = orient.querySelector('option[value="face"]');
  const button = form.querySelector("button");
  const shown = document.getElementById("selection");
  const extrasBox = document.getElementById("extras");
  const printer = form.elements.printer;
  const studio = document.getElementById("studio-link");
  const webStudio = document.getElementById("web-studio-link"); // absent if not set up
  const webState = document.getElementById("web-studio-state");
  let webStatus = "unknown";
  let sending = false; // a part is on its way to the web session
  let selectionOk = false;
  const mac = /Mac/.test(navigator.userAgent);

  let bodies = [];
  let faces = [];
  const chosen = {}; // extra part id -> tray id, kept across re-renders

  const post = (name) => window.parent.postMessage({ ...ids, messageName: name }, ONSHAPE);
  post("applicationInit");
  setInterval(() => post("keepAlive"), 60000);

  const printerName = () => printer.value.split("|")[0];
  const nameOf = (id) => parts[id] || id;

  // The loaded slots of the chosen printer, from the main menu's options.
  function slotOptions() {
    const prefix = `${printerName()}|`;
    return [...printer.options].filter((o) => o.value.startsWith(prefix) && !o.disabled);
  }

  function renderExtras() {
    extrasBox.replaceChildren();
    const extra = bodies.slice(1);
    const options = slotOptions();
    for (const id of extra) {
      const label = document.createElement("label");
      label.textContent = `Filament for ${nameOf(id)} `;
      const select = document.createElement("select");
      select.dataset.part = id;
      for (const o of options) {
        const opt = document.createElement("option");
        opt.value = o.value.split("|")[1];
        opt.textContent = o.textContent.replace(`${printerName()} · `, "");
        opt.dataset.color = o.dataset.color || "";
        select.append(opt);
      }
      if (chosen[id] && [...select.options].some((o) => o.value === chosen[id])) {
        select.value = chosen[id];
      }
      chosen[id] = select.value;
      select.addEventListener("change", () => { chosen[id] = select.value; sync(); });
      label.append(select);
      extrasBox.append(label);
    }
  }

  function extraValue() {
    return bodies.slice(1).filter((id) => chosen[id]).map((id) => `${id}:${chosen[id]}`).join(",");
  }

  function sync() {
    const part = bodies[0] || "";
    const face = faces.length === 1 ? faces[0] : "";
    const multi = bodies.length > 1;
    form.elements.p.value = part;
    form.elements.face.value = face;
    form.elements.extra.value = multi ? extraValue() : "";
    faceOpt.disabled = !face;
    if (face && orient.value !== "face" && !sync.userPicked) orient.value = "face";
    if (!face && orient.value === "face") orient.value = "as-modeled";

    let text;
    let ok = Boolean(part || face) && faces.length <= 1;
    if (faces.length > 1) text = "Select at most one face (the one the print stands on).";
    else if (multi) {
      text = `${bodies.length} parts as one print: ${bodies.map(nameOf).join(", ")}`;
      if (face) text += `, face ${face} down`;
      const everyPartHasSlot = printer.value.includes("|")
        && bodies.slice(1).every((id) => chosen[id]);
      if (!everyPartHasSlot) {
        text += ". Pick a loaded filament (not the preset) for every part.";
        ok = false;
      }
    } else if (part && face) text = `${nameOf(part)}, face ${face} down`;
    else if (part) text = nameOf(part);
    else if (face) text = `The part with face ${face}, that face down`;
    else text = "Select a part (or several, for multi-material), or the face it stands on.";
    shown.textContent = text;
    button.disabled = !ok;
    document.dispatchEvent(new CustomEvent("os2slice:selection"));
    selectionOk = Boolean(part || face) && faces.length <= 1;
    scheduleStudioLink(selectionOk);
    updateWebStudio();
  }

  // The shared Bambu Studio in the browser (one session for everyone, D-21).
  function updateWebStudio() {
    if (!webStudio) return;
    const label = { free: "(free)", busy: "(in use)", unknown: "(unavailable)" }[webStatus];
    if (!sending) webState.textContent = label || "";
    const usable = selectionOk && webStatus !== "unknown";
    webStudio.classList.toggle("off", !usable);
  }

  async function pollWebStudio() {
    if (!webStudio) return;
    try {
      const res = await fetch("/panel/web-studio/status", { credentials: "same-origin" });
      webStatus = res.ok ? (await res.json()).state : "unknown";
    } catch {
      webStatus = "unknown";
    }
    updateWebStudio();
  }

  if (webStudio) {
    webStudio.addEventListener("click", (ev) => {
      if (webStudio.classList.contains("off")) { ev.preventDefault(); return; }
      if (webStatus === "busy" && !window.confirm(
        "Someone has the web Bambu Studio open (maybe you). Send this part there anyway?\n\n"
        + "Cancel, then use \"on this computer\" to open it in your own Bambu Studio.")) {
        ev.preventDefault();
        return;
      }
      // The link opens the session in a new tab; this hands it the part. The server
      // slices it first (for the printer and filament settings), so it takes a while.
      sending = true;
      webState.textContent = "(preparing… slicing for the settings)";
      fetch("/panel/web-studio", {
        method: "POST", credentials: "same-origin",
        body: new URLSearchParams(new FormData(form)),
      }).then((res) => {
        sending = false;
        webState.textContent = res.ok ? "(sent: it opens in a few seconds)" : "(failed: see the log)";
      }).catch(() => {
        sending = false;
        webState.textContent = "(failed)";
      });
    });
    pollWebStudio();
    setInterval(pollWebStudio, 10000);
  }

  // "Open in Bambu Studio": a short-lived download URL for this selection as a 3MF,
  // handed to Bambu Studio's URL handler (Windows/Linux vs macOS forms).
  let linkTimer = 0;
  let linkFetch = null;
  function scheduleStudioLink(enabled) {
    clearTimeout(linkTimer);
    studio.classList.add("off");
    studio.href = "#";
    if (!enabled) return;
    linkTimer = setTimeout(async () => {
      if (linkFetch) linkFetch.abort();
      linkFetch = new AbortController();
      try {
        const res = await fetch("/panel/model-link", {
          method: "POST", signal: linkFetch.signal, credentials: "same-origin",
          body: new URLSearchParams(new FormData(form)),
        });
        if (!res.ok) return;
        const { url } = await res.json();
        const encoded = encodeURIComponent(url);
        studio.href = mac ? `bambustudioopen://${encoded}` : `bambustudio://open?file=${encoded}`;
        studio.classList.remove("off");
      } catch (err) {
        if (err.name !== "AbortError") studio.title = String(err);
      }
    }, 400);
  }

  // The preview takes each part's filament colour.
  const syncColor = () => {
    const opt = printer.options[printer.selectedIndex];
    root.dataset.color = (opt && opt.dataset.color) || "";
    document.dispatchEvent(new CustomEvent("os2slice:color"));
  };
  printer.addEventListener("change", () => { renderExtras(); syncColor(); sync(); });
  orient.addEventListener("change", () => { sync.userPicked = true; sync(); });
  // The Bambu Studio link carries the settings (copies, brim, ...): rebuild it on edits.
  const SETTINGS = ["walls", "infill", "supports", "build_plate_only", "top_layers",
    "bottom_layers", "brim", "copies", "plate"];
  form.addEventListener("change", (ev) => {
    if (SETTINGS.includes(ev.target.name)) scheduleStudioLink(selectionOk);
  });
  syncColor();

  window.addEventListener("message", (ev) => {
    if (ev.origin !== ONSHAPE || !ev.data || ev.data.messageName !== "SELECTION") return;
    const sel = Array.isArray(ev.data.selections) ? ev.data.selections : [];
    bodies = sel.filter((s) => s && s.selectionType === "BODY").map((s) => String(s.selectionId));
    faces = sel
      .filter((s) => s && s.selectionType === "ENTITY" && s.entityType === "FACE")
      .map((s) => String(s.selectionId));
    sync.userPicked = false;
    renderExtras();
    sync();
  });
  // For the preview: part id -> colour of the filament it prints with.
  window.os2sliceColours = () => {
    const out = {};
    if (bodies[0]) out[bodies[0]] = root.dataset.color || "";
    for (const sel of extrasBox.querySelectorAll("select")) {
      const o = sel.options[sel.selectedIndex];
      out[sel.dataset.part] = (o && o.dataset.color) || "";
    }
    return out;
  };
  renderExtras();
  sync();
})();
