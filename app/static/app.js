const $ = (id) => document.getElementById(id);
function applyUiLanguage(lang) {
  if (!UI_LANGS.includes(lang)) lang = "en";
  currentUiLang = lang;
  localStorage.setItem(UI_LANG_KEY, lang);
  if ($("uiLanguageSelect")) $("uiLanguageSelect").value = lang;
  applyStaticTexts();
  if ($("setupGuideAnchor") && typeof setupUrlForLang === "function") {
    $("setupGuideAnchor").href = setupUrlForLang(lang);
  }
  if (typeof renderSettingsRows === "function" && config && $("settingsModal") && $("settingsModal").open) {
    renderSettingsRows();
  }
  if (typeof renderPromptChips === "function") {
    renderPromptChips();
  }
  if (typeof renderPromptsModal === "function" && $("promptsModal") && $("promptsModal").open) {
    renderPromptsModal();
  }
  if (typeof renderSourcesModal === "function" && $("sourcesModal") && $("sourcesModal").open) {
    renderSourcesModal();
  }
  if (state && state.overview && typeof render === "function") {
    liveKey = null;
    shownRows = "";
    shownNotices = "";
    render(state);
  }
}

// The colour of a status badge; its words are the texts status.<status>.
const STATUS_TONE = {not_posted: "", exported_for_manual_post: "warn", draft: "ok", published: "ok", existing_source: "ok"};
const SETTINGS_KEY = "tba.settings", LAST_RUN_KEY = "tba.lastRun";
const RUN_ID = /^[0-9a-f]{32}$/;
let jobId = null, timer = null, state = null, missed = 0, restoredJob = null;
let shownRows = "", shownNotices = "", config = null;
let busySince = null, lastLogAt = Date.now(), lastLogLength = 0, liveKey = null;
const typed = {};  // values typed into the table survive re-rendering

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}
function toast(text, ms = 4000) {
  $("toast").textContent = text; $("toast").classList.remove("hidden");
  clearTimeout(toast.t); toast.t = setTimeout(() => $("toast").classList.add("hidden"), ms);
}
function clock(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? t("clockSeconds", {s}) : t("clockMinutes", {m: Math.floor(s / 60), s: String(s % 60).padStart(2, "0")});
}
async function api(path, body, method) {
  const response = await fetch(path, body === undefined ? {} : {
    method: method || "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  let data = null;
  try { data = await response.json(); } catch (e) {}
  if (!response.ok) {
    const error = new Error(refusalText(data, response.statusText));
    error.status = response.status;
    throw error;
  }
  return data;
}
// A preview is shown in the language of the page: the server picks its words.
const fileUrl = (name) => `/api/jobs/${jobId}/files/${encodeURIComponent(name)}?lang=${currentUiLang}`;
// The name of a language is written in that language, whatever the page's.
const languageName = (row) => t("lang." + row.language, null, row.language_name);
// The steps come from the server by their English names; step.<name> is their text.
const stepText = (name) => t("step." + name.toLowerCase().replace(/[^a-z]+/g, "_"), null, name);

// One dialog for every action that writes outside this machine.
// Resolves to true or false; with ``choices`` to the chosen value, or null when cancelled.
function confirmAction({title, summary, extra, validate, note, okLabel, choices}) {
  return new Promise((resolve) => {
    $("confirmTitle").textContent = title;
    $("confirmSummary").replaceChildren(...(summary || []).flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    $("confirmExtra").replaceChildren(...(extra || []));
    $("confirmChoices").replaceChildren(...(choices || []).map((choice, i) => el("label", {class: "choice"},
      el("input", {type: "radio", name: "confirmChoice", value: choice.value, checked: i === 0}),
      el("span", {}, el("strong", {}, choice.label), el("span", {class: "muted"}, " · " + choice.hint)))));
    $("confirmNote").textContent = note || "";
    $("confirmOk").textContent = okLabel;
    const dialog = $("confirmDialog");
    const done = (ok) => {
      if (ok && validate && !validate()) return;
      const picked = dialog.querySelector("input[name=confirmChoice]:checked");
      dialog.close(); $("confirmOk").onclick = $("confirmCancel").onclick = dialog.oncancel = null;
      resolve(choices ? (ok && picked ? picked.value : null) : ok);
    };
    $("confirmOk").onclick = () => done(true);
    $("confirmCancel").onclick = () => done(false);
    dialog.oncancel = () => done(false);
    dialog.showModal();
  });
}

async function act(path, body) {
  try { await api(`/api/jobs/${jobId}/${path}`, body); busySince = Date.now(); await poll(); }
  catch (error) { toast(error.message, 6000); }
}

function keep(key, attrs) {
  const input = el("input", {type: "text", value: typed[key] || "", ...attrs});
  input.addEventListener("input", () => { typed[key] = input.value; });
  return input;
}

// One thing the Slides column offers, by the name the server gives it.
function slideAction(row, action, slidesBusy) {
  const link = (href, text, attrs) => el("a", {href, target: "_blank", rel: "noopener", ...attrs}, text);
  const button = (text, run) => el("button", {type: "button", class: "ghost", disabled: slidesBusy, onclick: run}, text);
  if (action === "create_slides") return el("div", {}, button(t("createSlidesBtn"), () => requestSlides([row])));
  if (action === "open_deck") return el("div", {}, link(row.deck.url, "Google Slides", {style: "font-weight:600"}));
  if (action === "notes") return el("div", {class: "muted small"}, link(fileUrl(row.slides_file), t("speakerNotes"), {title: row.slides_file}));
  if (action === "create_deck") return el("div", {}, button(t("createDeckBtn"), () => createDeck(row)));
  // The speaker notes changed after the deck was made: the editor of the
  // notes opens and asks, listing the slides, before the deck is written.
  if (action === "update_deck") return el("div", {}, link(`/edit?job=${jobId}&file=${encodeURIComponent(row.slides_file)}&push=1`, t("updateDeckBtn")));
  return null;
}

// slidesBusy and pBusy: whether the slides lane and the main lane are working.
// A button is held back only while its own lane works.
function languageRow(row, slidesBusy, pBusy) {
  // "Ready to paste" would be untrue for a text nobody has finalized yet.
  const shown = row.status === "exported_for_manual_post" && row.next === "review" ? "not_posted" : row.status;
  const tone = STATUS_TONE[shown] || "";
  const label = t("status." + shown, null, shown);
  const langName = languageName(row);

  // Files: the posts. Editing starts from the preview a link opens.
  const files = el("td", {class: "files"}, ...row.files.map((name) => el("div", {},
    el("a", {href: fileUrl(name), target: "_blank", rel: "noopener"}, name))));

  // Slides: what the server says the state of this language allows, and
  // nothing else (WEB-5); while the slides lane works on it, where it is.
  const slideItems = row.slides_progress
    ? [el("div", {class: "muted small"}, el("span", {class: "spinner"}), t("slidePhase." + row.slides_progress))]
    : (row.slides_actions || []).map((action) => slideAction(row, action, slidesBusy)).filter(Boolean);
  const slidesCol = el("td", {class: "slides-col"}, ...slideItems);

  // Next step: review, finalize, approve, post. Only the one that is next is shown.
  const next = el("div", {class: "next"});
  const link = (text) => el("a", {href: row.url, target: "_blank", rel: "noopener"}, text);
  const drafted = row.status === "draft";
  if (row.next === "original") next.append(link(t("openOriginal")));
  else if (row.next === "public") next.append(link(t("openPost")));
  else {
    if (row.url) next.append(link(drafted ? t("openDraft") : t("openPost")));
    if (row.next === "review") {
      const reviewHint = drafted ? t("reviewHintDrafted") : t("reviewHint", {file: `post.${row.language}.md`});
      next.append(el("span", {class: "muted small"}, reviewHint),
        el("div", {class: "inline"}, el("button", {class: "primary", type: "button", disabled: pBusy, onclick: () => finalize(row)}, t("finalizeBtn"))));
    } else if (row.next === "post") {
      next.append(
        el("span", {class: "muted small"}, t("postHint")),
        el("div", {class: "inline"}, el("button", {class: "primary", type: "button", disabled: pBusy,
          onclick: () => postDraft(row, drafted)}, drafted ? t("updatePostBtn") : t("postBtn"))));
    } else if (row.next === "paste") {
      const url = keep(`url:${row.platform}`, {placeholder: t("urlPlaceholder"), "aria-label": t("urlAria", {platform: row.platform_name})});
      next.append(el("div", {class: "inline"}, url, el("button", {type: "button", disabled: pBusy, onclick: () => recordUrl(row, url.value)}, t("registerUrlBtn"))),
        el("span", {class: "muted small"}, t("pasteHint")));
    }
  }
  // A localization made from an earlier text of the source says so, and
  // offers to make it again: the request is put in the message box, unsent.
  const stale = row.older_than_source ? el("div", {class: "stale"},
    el("span", {class: "badge warn", title: t("olderThanSourceTitle")}, t("olderThanSource")),
    el("button", {type: "button", class: "ghost small", onclick: offerRetranslation}, t("retranslateBtn"))) : null;
  return el("tr", {},
    el("td", {class: "lang"}, el("div", {class: "who"}, langName), el("span", {class: "badge " + tone}, label), stale),
    el("td", {class: "title"}, row.title),
    files,
    slidesCol,
    el("td", {}, next));
}

function render(data) {
  state = {...state, ...data};
  const o = data.overview, busy = data.busy;
  if (busy && !busySince) { busySince = Date.now(); $("logBox").open = true; }
  if (!busy) busySince = null;
  if (data.log.length !== lastLogLength) {
    lastLogLength = data.log.length;
    lastLogAt = Date.now();
    $("log").textContent = data.log.join("\n\n");
    $("log").scrollTop = $("log").scrollHeight;
    if (busy) $("logBox").open = true;
  }

  $("progress").classList.remove("hidden");
  $("clearRun").classList.remove("hidden");
  if (restoredJob !== jobId && o.source_text) {
    restoredJob = jobId;
    if (!$("source").value.trim() && document.activeElement !== $("source")) {
      $("source").value = cleanSourceText(o.source_text);
    }
  }
  $("articleTitle").textContent = o.title || (busy ? t("readingSource") : t("noArticle"));
  // The server says which steps are done, judged against what this run was asked for.
  const firstOpen = data.progress.findIndex((step) => !step.done);
  const postSteps = data.post_progress || [];
  // Two lanes (RUN-1): the main lane makes and posts the posts and talks to
  // the agent, the slides lane makes plans and decks.
  const lanes = data.lanes || {main: {busy}, slides: {busy}};
  const postsBusy = lanes.main.busy, slidesBusy = lanes.slides.busy;
  const postsReady = !postsBusy && data.progress.length > 0;
  const firstPostOpen = postSteps.findIndex((step) => !step.done);
  const stepLi = (step, isNow) => {
    return el("li", {class: step.done ? "done" : (isNow ? "now" : "")}, (step.done ? "✓ " : "") + stepText(step.name));
  };
  const spacerLi = () => el("li", {class: "spacer"}, "");

  if (data.progress.length >= 5 && postSteps.length >= 5) {
    // Row 1: Source document | Transform | Prepare posts | Summarize | Review & edit
    $("steps").replaceChildren(
      stepLi(data.progress[0], postsBusy && firstOpen === 0),
      stepLi(data.progress[1], postsBusy && firstOpen === 1),
      stepLi(data.progress[2], postsBusy && firstOpen === 2),
      stepLi(data.progress[4], postsBusy && firstOpen === 4),
      stepLi(postSteps[0], postsReady && firstPostOpen === 0)
    );
    // Row 2: Prepare slides directly beneath Prepare posts (column 3)
    $("slideSteps").replaceChildren(
      spacerLi(),
      spacerLi(),
      stepLi(data.progress[3], slidesBusy && !data.progress[3].done),
      spacerLi(),
      spacerLi()
    );
    // Row 3: Finalize posts | Approve & draft | Publish posts | Verify & complete
    $("nextSteps").replaceChildren(
      stepLi(postSteps[1], postsReady && firstPostOpen === 1),
      stepLi(postSteps[2], postsReady && firstPostOpen === 2),
      stepLi(postSteps[3], postsReady && firstPostOpen === 3),
      stepLi(postSteps[4], postsReady && firstPostOpen === 4),
      spacerLi()
    );
  } else {
    $("steps").replaceChildren(...data.progress.map((step, i) => stepLi(step, busy && i === firstOpen)));
    $("slideSteps").replaceChildren();
    $("nextSteps").replaceChildren(...postSteps.map((step, i) => stepLi(step, postsReady && i === firstPostOpen)));
  }
  drawLive();

  const notices = [];
  // Each lane says how its last action ended, in the language of the page.
  for (const lane of Object.values(data.lanes || {})) {
    if (lane.error) notices.push(el("div", {class: "notice err"}, el("span", {class: "grow"}, t("stoppedWithError", {error: serverSentence(lane.errors) || lane.error}))));
    if (lane.notice) notices.push(el("div", {class: "notice note"}, el("span", {class: "grow"}, serverSentence(lane.notices) || lane.notice)));
  }
  // Slides the run was asked for and has not got, after a server stop or a
  // failure: one request for all of them is offered, with the options of the
  // run. Nothing is made before the dialog is approved (RUN-4, INV-6).
  const missing = (data.missing_slides || []).map((code) => o.rows.find((row) => row.language === code)).filter(Boolean);
  if (missing.length) notices.push(el("div", {class: "notice ask"},
    el("span", {class: "grow"}, t("missingSlides", {languages: missing.map(languageName).join(", ")})),
    el("button", {type: "button", class: "primary", disabled: slidesBusy, onclick: () => requestSlides(missing, data.options)}, t("missingSlidesBtn"))));
  if (!postsBusy) for (const approval of data.approvals) notices.push(el("div", {class: "notice ask"},
    el("span", {class: "grow"}, approval.hint, data.approvals.length > 1
      ? el("div", {class: "muted small"}, t("oneAnswerAtATime")) : null),
    el("button", {type: "button", onclick: () => act("approval", {id: approval.id, confirmed: false})}, t("declineBtn")),
    el("button", {type: "button", class: "primary", onclick: () => act("approval", {id: approval.id, confirmed: true})}, t("approveBtn"))));
  // Rebuilt only when something changed: a button replaced between a press
  // and its release would swallow the click.
  const noticesKey = JSON.stringify([data.error, data.notice, postsBusy ? [] : data.approvals, data.missing_slides, data.options, slidesBusy]);
  if (noticesKey !== shownNotices) { shownNotices = noticesKey; $("notices").replaceChildren(...notices); }

  const rows = o.rows.filter((r) => r.title)
    .sort((a, b) => (b.language === o.source_language) - (a.language === o.source_language));
  $("langs").classList.toggle("hidden", rows.length === 0);
  // Rebuilding a row would wipe what is being typed into it, so wait while a
  // text box in the table has the focus; a focused button does not hold it up.
  const active = document.activeElement;
  const rowsKey = JSON.stringify([rows, slidesBusy, postsBusy]);
  if (rowsKey !== shownRows && !($("rows").contains(active) && active.tagName === "INPUT")) {
    shownRows = rowsKey;
    $("rows").replaceChildren(...rows.map((row) => languageRow(row, slidesBusy, postsBusy)));
  }

  $("askCard").classList.toggle("hidden", !o.title && !busy && data.log.length === 0);
  $("reply").classList.toggle("hidden", !data.reply);
  $("reply").textContent = data.reply || "";
  $("run").disabled = busy;
  $("send").disabled = postsBusy;
  $("send").title = postsBusy ? t("agentWorking") : "";
}

function drawLive() {
  if (!state || !jobId || !state.overview) return;
  const lastLine = (state.log && state.log.length ? state.log[state.log.length - 1] : "").split("\n")[0];
  if (missed >= 2) {
    liveKey = null;
    $("elapsed").textContent = "";
    $("live").replaceChildren(el("span", {class: "muted"}, t("lostContact")));
  } else if (state.busy) {
    liveKey = null;
    $("elapsed").textContent = t("elapsedText", {time: clock(Date.now() - (busySince || Date.now()))});
    $("live").replaceChildren(el("span", {class: "spinner"}), el("span", {}, t("workingText")),
      el("span", {class: "last"}, t("liveDetail", {last: lastLine || t("liveStarting"), time: clock(Date.now() - lastLogAt)})));
  } else {
    const manifest = state.overview.manifest;
    const stopped = Boolean(state.error || (!state.overview.title && state.notice));
    const key = `${jobId}|${currentUiLang}|${stopped ? "stopped" : "finished"}|${manifest || ""}`;
    if (key === liveKey) return;   // keep the link in place; a rebuilt link can swallow a click
    liveKey = key;
    $("elapsed").textContent = "";
    const done = [el("span", {class: "muted"}, stopped ? t("stoppedText") : t("finishedText"))];
    if (manifest) done.push(el("span", {class: "muted"}, t("summaryRunText"), el("a", {href: fileUrl(manifest), target: "_blank", rel: "noopener"}, manifest)));
    $("live").replaceChildren(...done);
  }
}
setInterval(drawLive, 1000);

function forget(message) {
  clearInterval(timer); jobId = null; state = null; missed = 0; restoredJob = null;
  history.replaceState(null, "", location.pathname);
  localStorage.removeItem(LAST_RUN_KEY);
  $("run").disabled = false;
  $("clearRun").classList.add("hidden");
  for (const id of ["progress", "langs", "askCard"]) $(id).classList.add("hidden");
  if (message) toast(message, 8000);
}

async function poll() {
  if (!jobId) return;
  try { render(await api(`/api/jobs/${jobId}`)); missed = 0; }
  catch (error) {
    if (error.status === 404) forget(state && state.overview ? t("runGone") : "");
    else {
      missed += 1;
      // Without the access cookie the server says so, in a sentence (SEC-7).
      if (error.status === 401 && missed === 1) toast(error.message, 8000);
      if (missed === 2) $("run").disabled = false;
      drawLive();
    }
  }
}

// The rule of each platform, in words, is the text tagRule.<platform>.
function checkTags(platform, tags) {
  if (platform === "qiita") {
    if (tags.length < 1 || tags.length > 5 || tags.some((tag) => /\s/.test(tag)))
      return t("tagRule.qiita");
  } else if (platform === "devto") {
    if (tags.length > 4 || tags.some((tag) => !/^[a-z0-9]{1,30}$/.test(tag)))
      return t("tagRule.devto");
  }
  return null;
}
async function postDraft(row, update) {
  const isQiita = row.platform === "qiita";
  const ruleText = t("tagRule." + row.platform, null, "");
  const tagInput = keep(`tags:${row.platform}`, {
    id: "confirmTags",
    placeholder: isQiita ? "Python, Docker, GCP, AI, Architecture" : "python, docker, gcp, tutorial",
    "aria-label": t("tagsAria", {platform: row.platform_name}),
  });
  const tagError = el("span", {class: "small hidden", style: "color:var(--bad); font-weight:400"}, "");
  tagInput.addEventListener("input", () => tagError.classList.add("hidden"));
  let parsedTags = [];
  // The person approving decides the tags and how far the post goes; the agent cannot.
  const visibility = await confirmAction({
    title: update ? t("updatePostTitle", {platform: row.platform_name}) : t("postTitle", {platform: row.platform_name}),
    summary: [[t("titleLabel"), row.title]],
    extra: [el("label", {class: "field", for: "confirmTags"}, t("tagsLabel"), tagInput, el("span", {class: "hint"}, ruleText), tagError)],
    validate: () => {
      parsedTags = tagInput.value.split(",").map((tag) => (isQiita ? tag.trim() : tag.trim().toLowerCase())).filter(Boolean);
      const err = checkTags(row.platform, parsedTags);
      if (err) { tagError.textContent = err; tagError.classList.remove("hidden"); tagInput.focus(); return false; }
      return true;
    },
    choices: [
      {value: "draft", label: t("visibility.draft"), hint: t("visibility.draftHint")},
      {value: "public", label: t("visibility.public"), hint: t("visibility.publicHint")}],
    note: t("postNote"), okLabel: t("approvePostBtn")});
  if (visibility) act("draft", {platform: row.platform, tags: parsedTags, approved: true, public: visibility === "public"});
}
async function finalize(row) {
  try {
    let done = await api(`/api/jobs/${jobId}/finalize`, {language: row.language});
    if (done.needs_acknowledgement) {
      // Shown as type and line only; the values never leave the server.
      const accepted = await confirmAction({
        title: t("credentialsTitle", {language: languageName(row)}),
        summary: done.findings.slice(0, 40).map((f) => [t("lineN", {line: f.line}), f.type.replaceAll("_", " ").toLowerCase()]),
        note: done.findings.length > 40 ? t("credentialsNoteMany", {count: done.findings.length}) : t("credentialsNote", {count: done.findings.length}),
        okLabel: t("credentialsOk")});
      if (!accepted) return;
      done = await api(`/api/jobs/${jobId}/finalize`, {language: row.language, acknowledge_findings: true});
    }
    if (done.check && !done.check.passed) toast(t("finalizedDiffers", {errors: done.check.errors.join(" ")}), 9000);
    await poll();
  } catch (error) { toast(error.message, 6000); }
}
function recordUrl(row, url) {
  if (!/^https:\/\/\S+$/.test(url.trim())) { toast(t("enterUrl")); return; }
  act("url", {platform: row.platform, url: url.trim()});
}
async function loadConfig() {
  if (!config) config = await api("/api/config");
  return config;
}
// An audience is sent to the server in English; the form has its name in
// the language of the page. One typed to the agent is shown as it was typed.
function audienceText(value) {
  const option = [...$("audience").options].find((o) => o.value === value);
  return option ? option.textContent : value;
}
// Slides for one language or several: a plan and its deck each, as one
// request (SLD-14). The dialog is the approval; it names the languages, the
// length, the audience and the account (INV-6), and says what an approval
// replaces. Without ``options`` each language keeps the length and the
// audience it has; with the options of a run (RUN-4) the slides are made as
// that run was asked to make them.
async function requestSlides(rows, options) {
  let account = null;
  try { account = (await loadConfig()).slides_account; } catch (e) {}
  const names = (some) => some.map(languageName).join(", ");
  const withPlan = rows.filter((row) => row.slides_file), withDeck = rows.filter((row) => row.deck && row.deck.url);
  // Which languages a remark is about is said when there are several.
  const about = (some, text) => (rows.length > 1 ? `${names(some)} · ${text}` : text);
  const minutes = (options && options.minutes) || rows[0].slides_minutes;
  const audience = (options && options.audience) || (state && state.overview && state.overview.slides_audience) || "software engineers";
  const summary = [
    [t("slidesLanguageLabel"), names(rows)],
    [t("slidesLengthLabel"), t("slidesLengthValue", {minutes})],
    [t("slidesAudienceLabel"), audienceText(audience)],
    [t("fromLabel"), t("slidesFromValue", {file: rows.map((row) => `post.${row.language}.md`).join(", ")})],
    [t("whereLabel"), account ? t("driveOf", {account}) : t("driveYours")],
  ];
  if (withPlan.length) summary.push([t("planLabel"), about(withPlan, t("slidesPlanReplaced"))]);
  if (withDeck.length) summary.push([t("deckLabel"), about(withDeck, t("slidesDeckKept"))]);
  const again = withPlan.length > 0;
  const title = options ? t("missingSlidesTitle") : t("slidesTitle", {language: names(rows)});
  const ok = await confirmAction({title, summary, okLabel: again ? t("slidesAgainOk") : t("slidesOk")});
  if (ok) act("slides", {
    languages: rows.map((row) => row.language),
    minutes: options ? minutes : 0,
    audience: options ? audience : "",
    approved: true});
}
// A deck from the plan as it is; the plan is not touched.
async function createDeck(row) {
  // Name the account, so a deck never lands in a Drive nobody expected.
  let account = null;
  try { account = (await loadConfig()).slides_account; } catch (e) {}
  const whereDesc = account ? t("driveOf", {account}) : t("driveYours");
  const ok = await confirmAction({
    title: t("deckTitle", {language: languageName(row)}),
    summary: [[t("fromLabel"), row.slides_file || t("deckFromNotes")], [t("whereLabel"), whereDesc], [t("containsLabel"), t("deckContainsValue")]],
    okLabel: t("deckOk")});
  if (ok) act("deck", {language: row.language, approved: true});
}

const MAX_FILE_BYTES = 80000;
async function loadFile(file) {
  if (!file) return;
  if (!/\.(md|markdown|txt)$/i.test(file.name)) { toast(t("attachWrongType")); return; }
  if (file.size > MAX_FILE_BYTES) { toast(t("attachTooLarge")); return; }
  if ($("source").value.trim() && !(await confirmAction({
      title: t("replaceBoxTitle"), summary: [[t("replaceWithLabel"), file.name]], okLabel: t("replaceBtn")}))) return;
  const reader = new FileReader();
  reader.onload = () => { $("source").value = reader.result; $("fileInfo").textContent = t("fileLoaded", {name: file.name, count: reader.result.length.toLocaleString()}); };
  reader.onerror = () => toast(t("fileUnreadable"));
  reader.readAsText(file, "utf-8");
}

function setupUrlForLang(lang) {
  const file = textIn(lang, "setupGuideFile", null, "SETUP.md");
  return `https://github.com/agenthekim/trilingual-blog-agent/blob/main/docs/${file}`;
}

function renderSettingsRows() {
  if ($("setupGuideAnchor")) $("setupGuideAnchor").href = setupUrlForLang(currentUiLang);
  if (!config) return;
  const tag = (text, tone) => el("span", {class: "platform-tag " + (tone || "")}, text);
  const setupLink = (text, anchor) => el("a", {
    href: `${setupUrlForLang(currentUiLang)}#${anchor}`,
    target: "_blank",
    rel: "noopener",
    class: "platform-tag warn",
    style: "text-decoration:none",
  }, text);
  const row = (language, name, subtext, badgeNode) => el("div", {class: "platform-row"},
    el("div", {class: "platform-meta"},
      el("span", {class: "platform-lang"}, language),
      el("span", {class: "platform-name"}, name),
      subtext ? el("span", {class: "muted small", style: "font-size:11.5px"}, subtext) : null),
    el("div", {class: "platform-tags"}, badgeNode));

  $("settingsRows").replaceChildren(
    ...config.platforms.map((p) => {
      const sub = p.how === "api" ? t("howApi") : t("howHtml");
      let badge;
      if (p.how !== "api") {
        badge = tag(t("statusReady"), "ok");
      } else if (p.configured) {
        badge = tag(t("statusConnected"), "ok");
      } else {
        const anchor = p.platform.toLowerCase().includes("qiita") ? "3-qiita-access-token" : "2-dev-community-api-key";
        badge = setupLink(t("statusNoCred"), anchor);
      }
      return row(t("lang." + p.code, null, p.language), p.platform, sub, badge);
    }),
    row(
      t("settingsSlidesRow"),
      "Google Slides",
      `${config.slides_account ? config.slides_account + " · " : ""}${config.diagrams ? t("diagramsOn") : t("diagramsOff")}`,
      config.google_slides
        ? tag(t("statusSlidesOk"), "ok")
        : setupLink(t("statusSlidesNo"), "4-google-slides-oauth")
    )
  );
  $("settingsNote").textContent = t("settingsNoteText");
}

// Settings: the fixed mapping, and what the server says is set up right now.
async function openSettings() {
  $("settingsRows").replaceChildren(el("span", {class: "muted small"}, t("checking")));
  $("settingsNote").textContent = "";
  $("settingsModal").showModal();
  try {
    config = await api("/api/config");
    renderSettingsRows();
  } catch (error) {
    $("settingsRows").replaceChildren(el("span", {class: "muted small"}, t("settingsUnreadable", {error: error.message})));
  }
}
$("openSettings").addEventListener("click", openSettings);
$("closeSettings").addEventListener("click", () => $("settingsModal").close());
if ($("uiLanguageSelect")) {
  $("uiLanguageSelect").addEventListener("change", (e) => applyUiLanguage(e.target.value));
}
$("clearRun").addEventListener("click", () => {
  $("source").value = ""; $("file").value = "";
  $("fileInfo").textContent = t("dropHint");
  forget();
});
$("attach").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { loadFile(e.target.files[0]); e.target.value = ""; });
const zone = $("dropZone");
zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
zone.addEventListener("dragleave", () => zone.classList.remove("over"));
zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); loadFile(e.dataTransfer.files[0]); });

// Under the message box (WEB-9). A suggestion is a text of the catalogs,
// prompt.<name>, shown in the language of the page; how often each was sent
// is counted under its name. What a person typed is kept as typed, under its
// own heading, and is never translated.
// The last three ask the agent to make the slides and the deck of a language
// again: the Slides column has no button for that (WEB-5). Like every
// suggestion they only fill the message box; once sent, the agent asks for
// the approval of the slide request (INV-6) before anything is made.
const SUGGESTIONS = [
  "shorten_title", "informal_intro", "retranslate",
  "recreate_slides_en", "recreate_slides_ko", "recreate_slides_ja",
];
// How many suggestions are shown as chips; "More prompts…" lists them all.
const SUGGESTION_CHIPS = 3;
const SUGGESTION_USE_KEY = "tba.suggestion_use";
const PROMPT_HISTORY_KEY = "tba.prompt_history";
const suggestionText = (name) => t("prompt." + name);
// The name of the suggestion a text is, in whichever language it was sent.
function suggestionNamed(text) {
  return SUGGESTIONS.find((name) => UI_LANGS.some((lang) => textIn(lang, "prompt." + name) === text)) || null;
}
function getSuggestionUse() {
  try {
    const raw = JSON.parse(localStorage.getItem(SUGGESTION_USE_KEY) || "{}");
    return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  } catch (e) {
    return {};
  }
}
// The suggestions, the most used first; unused ones stay in their own order.
function orderedSuggestions() {
  const use = getSuggestionUse();
  const count = (name) => (Number.isFinite(use[name]) ? use[name] : 0);
  return SUGGESTIONS.map((name, at) => ({name, at})).sort((a, b) => count(b.name) - count(a.name) || a.at - b.at).map((s) => s.name);
}

// What a person typed, the most used first. An entry is {text, count,
// lastUsed}; whatever else an entry of an earlier version holds (it kept
// translations) is not read and is dropped the next time the list is saved.
// An earlier entry that is the text of a suggestion is not a typed request.
function getPromptHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(PROMPT_HISTORY_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((item) => item && typeof item.text === "string" && item.text.trim() && !suggestionNamed(item.text.trim()))
      .map((item) => ({text: item.text, count: Number.isFinite(item.count) ? item.count : 1, lastUsed: Number.isFinite(item.lastUsed) ? item.lastUsed : 0}))
      .sort((a, b) => b.count - a.count || b.lastUsed - a.lastUsed);
  } catch (e) {
    return [];
  }
}

function savePromptHistory(list) {
  try { localStorage.setItem(PROMPT_HISTORY_KEY, JSON.stringify(list.slice(0, 50))); } catch (e) {}
}

// Called with what was sent: a suggestion sent as it is counts for the
// suggestion; anything else is the person's own text and is kept as typed.
function recordPromptUsage(text) {
  const clean = text.trim();
  if (!clean) return;
  const name = suggestionNamed(clean);
  if (name) {
    const use = getSuggestionUse();
    use[name] = (Number.isFinite(use[name]) ? use[name] : 0) + 1;
    try { localStorage.setItem(SUGGESTION_USE_KEY, JSON.stringify(use)); } catch (e) {}
  } else {
    const list = getPromptHistory();
    const found = list.find((item) => item.text === clean);
    if (found) { found.count += 1; found.lastUsed = Date.now(); }
    else list.push({text: clean, count: 1, lastUsed: Date.now()});
    savePromptHistory(list);
  }
  renderPromptChips();
}

function deletePromptHistoryItem(text) {
  savePromptHistory(getPromptHistory().filter((item) => item.text !== text));
  renderPromptsModal();
}

// The suggestion to localize again, in the language of the page.
function offerRetranslation() {
  fillPromptInput(suggestionText("retranslate"));
}

function fillPromptInput(text) {
  const input = $("message");
  input.value = text;
  if ($("promptsModal") && $("promptsModal").open) $("promptsModal").close();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// The chips are suggestions only: the first few in their order, so a
// suggestion that was used moves into the row.
function renderPromptChips() {
  const container = $("promptChipsList");
  if (!container) return;
  container.replaceChildren(...orderedSuggestions().slice(0, SUGGESTION_CHIPS).map((name) => {
    const text = suggestionText(name);
    return el("button", {type: "button", class: "prompt-chip", title: text, onclick: () => fillPromptInput(text)}, text);
  }));
}

// Two groups: the suggestions, and what the person typed, as typed.
function renderPromptsModal() {
  const suggestionBox = $("suggestionPromptsList");
  if (suggestionBox) {
    suggestionBox.replaceChildren(...orderedSuggestions().map((name) => {
      const text = suggestionText(name);
      return el("div", {class: "prompt-modal-item"},
        el("button", {type: "button", class: "prompt-fill", onclick: () => fillPromptInput(text)}, text));
    }));
  }
  const historyBox = $("historyPromptsList");
  if (historyBox) {
    const history = getPromptHistory();
    if (history.length === 0) {
      historyBox.replaceChildren(el("span", {class: "muted small"}, t("noHistoryText")));
    } else {
      historyBox.replaceChildren(...history.map((item) =>
        el("div", {class: "prompt-modal-item"},
          el("button", {type: "button", class: "prompt-fill", onclick: () => fillPromptInput(item.text)}, item.text),
          el("span", {class: "badge", style: "font-size:11px"}, `×${item.count}`),
          el("button", {
            type: "button",
            class: "prompt-del",
            title: t("removeFromHistory"),
            onclick: () => deletePromptHistoryItem(item.text),
          }, "×"))));
    }
  }
}

function openPromptsModal() {
  renderPromptsModal();
  $("promptsModal").showModal();
}
if ($("openPromptsModal")) $("openPromptsModal").addEventListener("click", openPromptsModal);
if ($("closePromptsModal")) $("closePromptsModal").addEventListener("click", () => $("promptsModal").close());
if ($("clearPromptHistory")) {
  $("clearPromptHistory").addEventListener("click", () => {
    savePromptHistory([]);
    renderPromptsModal();
  });
}

function cleanSourceText(raw) {
  const s = (raw || "").trim();
  if (/^https?:\/\/\S+$/i.test(s)) {
    try {
      const u = new URL(s);
      for (const k of ["trackingCode", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]) {
        u.searchParams.delete(k);
      }
      return u.toString();
    } catch (e) {}
  }
  return s;
}

// "Recent sources" is the server's list of runs and nothing else (WEB-14):
// the browser keeps no copy of it. An entry has a title, the kind of its
// source and its length; the source text is asked for when an entry is picked.
let recentSources = [];
// What an earlier version of the page kept of it in this browser is dropped.
for (const old of ["source_history", "source_hidden"]) localStorage.removeItem("tba." + old);

function closeSourcesModal() {
  if ($("sourcesModal").open) $("sourcesModal").close();
}

async function fillSourceFrom(entry) {
  try {
    const answer = await api(`/api/jobs/${entry.job_id}/source`);
    $("source").value = cleanSourceText(answer.source_text);
    closeSourcesModal();
    $("source").focus();
  } catch (e) { toast(e.message); }
}

function openRecentRun(entry) {
  // The run brings its source with it: render() fills the empty box.
  $("source").value = "";
  restoredJob = null;
  closeSourcesModal();
  watch(entry.job_id);
}

async function hideRecentSource(entry) {
  try {
    await api(`/api/jobs/${entry.job_id}/hide`, {});
    recentSources = recentSources.filter((other) => other.job_id !== entry.job_id);
    renderSourcesModal();
  } catch (e) { toast(e.message); }
}

function renderSourcesModal() {
  const box = $("sourcesHistoryList");
  if (recentSources.length === 0) {
    box.replaceChildren(el("span", {class: "muted small"}, t("noSourceHistoryText")));
    return;
  }
  box.replaceChildren(...recentSources.map((entry) => {
    // A kind of address has a name of its own; the rest are named by the page.
    const kind = entry.label || t("sourceKind." + entry.kind);
    const details = typeof entry.chars === "number"
      ? t("sourceKindAndLength", {kind, count: entry.chars.toLocaleString()})
      : kind;
    return el("div", {class: "prompt-modal-item"},
      el("button", {
        type: "button",
        class: "prompt-fill",
        style: "display:grid; gap:2px",
        onclick: () => fillSourceFrom(entry),
      },
        el("span", {style: "font-weight:600; font-size:13px"}, entry.title || entry.address || t("sourceUntitled")),
        el("span", {class: "muted small", style: "font-size:11.5px"}, details),
        entry.title && entry.address ? el("span", {class: "muted small", style: "font-size:11.5px; overflow-wrap:anywhere"}, entry.address) : null
      ),
      el("button", {
        type: "button",
        class: "ghost small",
        style: "font-size:11.5px; height:24px; padding:0 8px; border:1px solid var(--line); border-radius:4px; white-space:nowrap",
        onclick: () => openRecentRun(entry),
      }, t("openRunBtn")),
      el("button", {
        type: "button",
        class: "prompt-del",
        title: t("hideFromList"),
        "aria-label": t("hideFromList"),
        onclick: () => hideRecentSource(entry),
      }, "×")
    );
  }));
}

async function openSourcesModal() {
  renderSourcesModal();
  $("sourcesModal").showModal();
  try {
    const answer = await api("/api/jobs");
    recentSources = Array.isArray(answer.jobs) ? answer.jobs : [];
    renderSourcesModal();
  } catch (e) { toast(e.message); }
}
$("openSourcesModal").addEventListener("click", openSourcesModal);
$("closeSourcesModal").addEventListener("click", closeSourcesModal);

function submitMessage() {
  const text = $("message").value.trim();
  if (!text) return;
  if (state && state.lanes && state.lanes.main.busy) { toast(t("agentWorkingRetry")); return; }
  recordPromptUsage(text);
  $("message").value = "";
  $("logBox").open = true;
  act("message", {text});
}
$("send").addEventListener("click", submitMessage);
$("message").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitMessage(); } });

function watch(id) {
  jobId = id; lastLogLength = 0; lastLogAt = Date.now(); busySince = null; missed = 0; liveKey = null;
  shownRows = shownNotices = "";
  history.replaceState(null, "", "#" + id);   // a reload comes back to the same run
  localStorage.setItem(LAST_RUN_KEY, id);     // and so does opening the page again
  clearInterval(timer); timer = setInterval(poll, 2000);
  return poll();
}
zone.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("source").value;
  if (text.trim().length < 10) { toast(t("pasteFirst")); $("source").focus(); return; }
  const languages = ["En", "Ko", "Ja"].filter((code) => $("slideLang" + code).checked).map((code) => code.toLowerCase());
  const minutes = Number($("minutes").value);
  const settings = {
    talk_minutes: minutes,
    talk_languages: languages,
    audience: $("audience").value || "software engineers",
    // Choosing a length and a language is the request for a deck per language.
    create_deck: minutes > 0 && languages.length > 0,
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  $("run").disabled = true;
  try {
    const started = await api("/api/jobs", {text, ...settings});
    for (const key of Object.keys(typed)) delete typed[key];
    state = {log: []};
    $("rows").replaceChildren(); $("log").textContent = "";
    await watch(started.job_id);
    $("progress").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { toast(error.message, 6000); $("run").disabled = false; }
});

// The page starts once the texts are read: the texts written into the page
// and the suggestions, then the last settings and the run this browser had open.
loadCatalogs().then(() => {
  applyUiLanguage(currentUiLang);

  // Restore the last settings, then the run in the address or the last run of this browser.
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    $("minutes").value = String(saved.talk_minutes ?? 5);
    const savedLangs = Array.isArray(saved.talk_languages) ? saved.talk_languages : ["en", "ko", "ja"];
    for (const code of ["En", "Ko", "Ja"]) $("slideLang" + code).checked = savedLangs.includes(code.toLowerCase());
    if (saved.audience) $("audience").value = saved.audience;
  } catch (e) { $("minutes").value = "5"; }
  const wanted = location.hash.slice(1) || localStorage.getItem(LAST_RUN_KEY) || "";
  if (RUN_ID.test(wanted)) { state = {log: []}; watch(wanted); }
  else if (location.hash.length > 1) history.replaceState(null, "", location.pathname);
});
