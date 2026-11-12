// The file's text is only ever put into a text box, never into the page as HTML.
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const job = params.get("job") || "", name = params.get("file") || "";
const valid = /^[0-9a-f]{32}$/.test(job) && /^(post|slides)\.(ko|en|ja)\.md$/.test(name);
const fileUrl = `/api/jobs/${job}/files/${name}`;
// The preview is shown in the language of the page: the server picks its words.
const previewUrl = `${fileUrl}?lang=${currentUiLang}`;
const language = name.split(".")[1];
let savedText = "", savedTitle = "", hasTitle = false, hasDeck = false;

const dirty = () => $("text").value !== savedText || (hasTitle && $("title").value !== savedTitle);
// tone: "bad" when something did not happen, "warn" when it did but needs a look.
function say(text, tone) {
  $("result").textContent = text;
  $("result").classList.toggle("bad", tone === true || tone === "bad");
  $("result").classList.toggle("warn", tone === "warn");
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.ok) return response;
  let data = null;
  try { data = await response.json(); } catch (e) {}
  throw new Error(refusalText(data, response.statusText));
}

async function load() {
  $("name").textContent = name || t("editor.noFile");
  document.title = t("editor.pageTitle", {name});
  if (!valid) { say(t("editor.badAddress"), true); return; }
  $("view").href = previewUrl;
  try {
    const state = await (await api(`/api/jobs/${job}`)).json();
    if (!state.overview.editable.includes(name)) {
      say(name.startsWith("post.") ? t("editor.postLocked") : t("editor.fileLocked"), true);
      return;
    }
    const row = state.overview.rows.find((r) => `post.${r.language}.md` === name);
    hasTitle = Boolean(row);
    $("titleField").classList.toggle("hidden", !hasTitle);
    $("title").value = savedTitle = row ? row.title : "";
    $("text").value = savedText = await (await api(fileUrl + "?raw=1")).text();
    $("text").disabled = false; $("save").disabled = false;
    // A slide plan whose deck exists can be pushed to it, or refreshed from it.
    const deck = name.startsWith("slides.") && (state.overview.decks || {})[language];
    hasDeck = Boolean(deck && deck.url);
    if (hasDeck) { $("deckLink").href = deck.url; $("deckTools").classList.remove("hidden"); }
    say(hasTitle ? t("editor.savingPost") : hasDeck ? t("editor.savingPlanWithDeck") : t("editor.savingPlanNoDeck"));
    // Start at the top: focusing alone would put the caret, and the view, at the end.
    $("text").focus(); $("text").setSelectionRange(0, 0); $("text").scrollTop = 0;
    // Opened by "Update deck…" of the page: ask at once what "Update deck…" here asks.
    if (hasDeck && params.get("push") === "1") await pushToDeck(false);
  } catch (error) { say(error.message, true); }
}

// Saves the text. Returns true when it is saved and, for a slide plan, still reads as one.
async function save() {
  if ($("save").disabled) return false;
  const body = {markdown: $("text").value};
  // A post cannot be without a title: an emptied field goes back to the saved one.
  if (hasTitle && !$("title").value.trim()) $("title").value = savedTitle;
  if (hasTitle) body.title = $("title").value;
  $("save").disabled = true; say(t("editor.saving"));
  let good = false;
  try {
    const data = await (await api(fileUrl, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})).json();
    savedText = $("text").value; savedTitle = $("title").value;
    const notes = [t("editor.saved")];
    good = !data.check || data.check.passed;
    if (hasTitle) notes.push(good ? t("editor.postMatches") : t("editor.postDiffers", {errors: data.check.errors.join(" ")}));
    else if (!good) notes.push(t("editor.planBroken", {errors: data.check.errors.join(" ")}));
    if (data.secrets && data.secrets.length) notes.push(t("editor.hasSecrets"));
    say(notes.join(" "), good ? "" : "warn");
    // The server keeps a slide plan in its regular form; show that form.
    if (good && !hasTitle) await reloadText();
  } catch (error) { say(error.message, true); }
  $("save").disabled = false;
  return good;
}

async function reloadText() {
  const box = $("text"), top = box.scrollTop, at = box.selectionStart;
  const fresh = await (await api(fileUrl + "?raw=1")).text();
  if (fresh !== box.value) { box.value = fresh; box.setSelectionRange(at, at); box.scrollTop = top; }
  savedText = fresh;
}

function confirmAction({title, summary, note, okLabel}) {
  return new Promise((resolve) => {
    $("confirmTitle").textContent = title;
    $("confirmSummary").replaceChildren(...summary.flatMap(([k, v]) => {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k; dd.textContent = v; return [dt, dd];
    }));
    $("confirmNote").textContent = note || "";
    $("confirmOk").textContent = okLabel;
    const dialog = $("confirmDialog");
    const done = (value) => { dialog.close(); $("confirmOk").onclick = $("confirmCancel").onclick = dialog.oncancel = null; resolve(value); };
    $("confirmOk").onclick = () => done(true);
    $("confirmCancel").onclick = () => done(false);
    dialog.oncancel = () => done(false);
    dialog.showModal();
  });
}

// Lists: the catalog has the mark that goes between two items, and each
// counted item as a whole text for one and for more than one.
const listed = (items) => items.join(t("listSeparator"));
const slideNames = (numbers) => numbers.map((n) => (n === 0 ? t("slideName.title") : n === 999 ? t("slideName.thanks") : String(n)));
function slideList(numbers) {
  const names = slideNames(numbers);
  return t("slideList", {names: listed(names.length > 12 ? [...names.slice(0, 12), "…"] : names), count: tn("slideCount", numbers.length)});
}
function changeList(s) {
  return listed([s.titles && tn("change.titles", s.titles), s.bullets && tn("change.bullets", s.bullets),
    s.notes && tn("change.notes", s.notes)].filter(Boolean));
}
// One request to the deck at a time: the buttons wait while one is under way.
// ``changeId`` names the changes the person was shown; the server writes only those.
async function syncDeck(direction, approved, changeId) {
  for (const id of ["push", "pull", "save"]) $(id).disabled = true;
  try {
    return await (await api(`/api/jobs/${job}/deck/sync`, {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({language, direction, approved, change_id: changeId})})).json();
  } finally { for (const id of ["push", "pull", "save"]) $(id).disabled = false; }
}

// File to deck. Nothing is written until the person approves what the dialog lists.
async function pushToDeck(quietIfSame) {
  try {
    const plan = await syncDeck("push", false);
    if (!plan.slides.length) { if (!quietIfSame) say(t("editor.deckSame")); return; }
    const notes = [];
    if (plan.conflicts.length) notes.push(t("editor.pushConflicts", {slides: listed(slideNames(plan.conflicts))}));
    if (!plan.baseline_known) notes.push(t("editor.noBaselinePush"));
    if (plan.only_in_file.length) notes.push(t("editor.onlyInFile", {slides: listed(slideNames(plan.only_in_file))}));
    const ok = await confirmAction({title: t("editor.pushTitle"),
      summary: [[t("editor.slidesThatChange"), slideList(plan.slides)], [t("editor.whatChanges"), changeList(plan)], [t("editor.staysLabel"), t("editor.staysValue")]],
      note: notes.join(" "), okLabel: t("editor.pushOk")});
    if (!ok) { say(t("editor.deckUnchanged")); return; }
    say(t("editor.updatingDeck"));
    const done = await syncDeck("push", true, plan.change_id);
    if (done.changed_since_preview) { say(t("editor.changedSincePreview"), "warn"); return; }
    say(tn("editor.deckUpdated", done.slides.length));
  } catch (error) { say(error.message, true); }
}

// Deck to file. The file is replaced only after the person approves.
async function pullFromDeck() {
  if (dirty()) { say(t("editor.saveBeforePull"), true); return; }
  try {
    const plan = await syncDeck("pull", false);
    if (!plan.slides.length) { say(t("editor.pullNothing")); return; }
    const notes = [];
    if (plan.conflicts.length) notes.push(t("editor.pullConflicts", {slides: listed(slideNames(plan.conflicts))}));
    if (!plan.baseline_known) notes.push(t("editor.noBaselinePull"));
    const ok = await confirmAction({title: t("editor.pullTitle"),
      summary: [[t("editor.slidesThatChange"), slideList(plan.slides)], [t("editor.whatChanges"), changeList(plan)], [t("editor.theDeckLabel"), t("editor.deckOnlyRead")]],
      note: notes.join(" "), okLabel: t("editor.pullOk")});
    if (!ok) return;
    const done = await syncDeck("pull", true, plan.change_id);
    if (done.changed_since_preview) { say(t("editor.changedSincePreview"), "warn"); return; }
    await reloadText();
    say(tn("editor.fileUpdated", done.slides.length));
  } catch (error) { say(error.message, true); }
}

$("save").addEventListener("click", async () => { if (await save() && hasDeck) await pushToDeck(true); });
$("push").addEventListener("click", async () => { if (!dirty() || await save()) await pushToDeck(false); });
$("pull").addEventListener("click", pullFromDeck);
$("cancel").addEventListener("click", () => { location.href = valid ? previewUrl : "/"; });
document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); $("save").click(); } });
// Leaving with unsaved changes asks first.
window.addEventListener("beforeunload", (e) => { if (dirty()) e.preventDefault(); });
// The editor starts once the texts are read.
loadCatalogs().then(() => { applyStaticTexts(); load(); });
