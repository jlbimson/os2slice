// The print panel's script in jsdom: node panel.test.js <page.html> <panel.js>
//
// tests/test_panel_js.py renders the page from the server's own functions and CSS (an
// MMU printer with four tools, T3 an empty gate, and three filament profiles) and runs
// this. Onshape's selection messages and menu changes are simulated; what's checked is
// what the user would see (computed style, not just the `hidden` flag) and what the form
// would send. Prints PASS/FAIL per check; exits 1 when any fails.
"use strict";
const fs = require("fs");
const { JSDOM } = require("jsdom");

const [pagePath, scriptPath] = process.argv.slice(2);
const dom = new JSDOM(fs.readFileSync(pagePath, "utf8"), {
  runScripts: "outside-only",
  url: "https://os2slice.test/panel",
});
const w = dom.window;
w.fetch = () => new Promise(() => {}); // no network: status polls and links never answer
w.parent.postMessage = () => {};
w.setInterval = () => 0;
w.eval(fs.readFileSync(scriptPath, "utf8"));

const d = w.document;
const form = d.getElementById("printform");
const filament = form.elements.filament;
const slot = form.elements.filament_tool;
const slotLabel = d.getElementById("tool-slot");

const shown = (el) => w.getComputedStyle(el).display !== "none";
const values = (select) => [...select.options].map((o) => o.value + (o.disabled ? "(x)" : ""));
// An option's label without its colour emoji (the swatch span has no text).
const labelOf = (o) => [...o.childNodes]
  .filter((n) => !(n.classList && n.classList.contains("fc-emoji")))
  .map((n) => n.textContent).join("");
const emojiOf = (o) => (o.querySelector(".fc-emoji") || { textContent: "" }).textContent;
const valueOf = (select, text) => [...select.options].find((o) => labelOf(o) === text).value;
const sent = () => Object.fromEntries(new w.FormData(form));
const change = (el, value) => {
  el.value = value;
  el.dispatchEvent(new w.Event("change", { bubbles: true }));
};
const select = (...bodies) => w.dispatchEvent(new w.MessageEvent("message", {
  origin: "https://cad.onshape.com",
  data: {
    messageName: "SELECTION",
    selections: bodies.map((b) => ({ selectionType: "BODY", selectionId: b })),
  },
}));

let failed = 0;
function check(what, ok, got) {
  console.log(`${ok ? "PASS" : "FAIL"} ${what}${ok ? "" : `  got: ${JSON.stringify(got)}`}`);
  if (!ok) failed += 1;
}

// -- one part, the MMU printer profile ------------------------------------------------
select("A");
const asaOpt = [...filament.options].find((o) => labelOf(o) === "PM ASA");
check("a coloured filament: emoji, then an exact swatch",
  emojiOf(asaOpt) === "\u{1F7E7} "
  && asaOpt.querySelector(".fc-swatch").style.background !== ""
  && asaOpt.dataset.color === "#F2754E", asaOpt.outerHTML);
const petCfOpt = [...filament.options].find((o) => labelOf(o) === "Sirayatech PET-CF");
check("no colour: no icon", petCfOpt.children.length === 0, petCfOpt.outerHTML);
const t3Opt = [...filament.options].find((o) => o.value === "t3");
check("an empty gate: no icon", t3Opt.children.length === 0, t3Opt.outerHTML);
check("the submit button is the one toggled, not a select's own",
  form.querySelector('button[type="submit"]').disabled === false, "disabled");
check("MMU profile: tools listed, T0 preselected",
  filament.value === "t0" && values(filament).includes("t3(x)"), values(filament));
check("a tool picked: no Load into", !shown(slotLabel) && slot.disabled, shown(slotLabel));

const pmAsa = valueOf(filament, "PM ASA");
const petCf = valueOf(filament, "Sirayatech PET-CF");
const creality = valueOf(filament, "Creality PETG");
change(filament, pmAsa);
check("a profile picked: Load into shown", shown(slotLabel) && !slot.disabled, shown(slotLabel));
check("a profile already loaded: its tool preselected", slot.value === "t0", slot.value);
change(filament, petCf);
check("a profile loaded nowhere: the empty gate preselected", slot.value === "t3", slot.value);
check("Load into labels", [...slot.options].map(labelOf).join(" | ") ===
  "T0, now Black (ASA) | T1, now PolyLite™ ASA Blue (PETG) | T2, now CR-PETG Transparent"
  + " | T3, now empty", [...slot.options].map(labelOf));
check("Load into icons: the tool's filament colour, none for an empty gate",
  [...slot.options].map(emojiOf).join("|") === "\u2B1B |\u2B1B |\u{1F7E6} |",
  [...slot.options].map(emojiOf));
change(slot, "t2");
change(filament, creality);
check("a tool the user chose is kept", slot.value === "t2", slot.value);
change(filament, petCf);
change(slot, "t3");
check("the form sends the filament and its tool",
  sent().filament === petCf && sent().filament_tool === "t3", sent());
change(filament, "t1");
check("back to a tool: Load into gone, not sent",
  !shown(slotLabel) && !("filament_tool" in sent()), sent());

// -- the plain printer profile -------------------------------------------------------
change(form.elements.machine, "JoshPrint 0.5");
check("plain profile: no tools", !values(filament).some((v) => /^t\d/.test(v)), values(filament));
check("plain profile: no Load into", !shown(slotLabel) && !("filament_tool" in sent()), sent());
change(form.elements.machine, "");

// -- two parts -----------------------------------------------------------------------
select("A", "B");
const extra = d.querySelector("#extras select[data-part=B]");
const extraSlot = extra.parentElement.nextElementSibling;
check("second part: tools and profiles",
  values(extra).includes("t2") && values(extra).includes(petCf), values(extra));
const extraT2 = [...extra.options].find((o) => o.value === "t2");
check("second part: one icon, the same label",
  extraT2.querySelectorAll(".fc-emoji").length === 1
  && labelOf(extraT2) === "T2: CR-PETG Transparent → Creality PETG"
  && extraT2.dataset.color === "#00FFFF", extraT2.outerHTML);
change(extra, petCf);
check("second part, a profile: its own Load into", shown(extraSlot), shown(extraSlot));
change(extraSlot.querySelector("select"), "t1");
check("second part's value names its tool", form.elements.extra.value === `B:t1.${petCf}`,
  form.elements.extra.value);
change(extra, "t2");
check("second part on a tool: no Load into, plain id",
  !shown(extraSlot) && form.elements.extra.value === "B:t2", form.elements.extra.value);

process.exit(failed ? 1 : 0);
