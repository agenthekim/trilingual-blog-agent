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
  if (typeof ensurePromptHistoryLocalized === "function") {
    ensurePromptHistoryLocalized();
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
    const detail = data && data.detail;
    const error = new Error(typeof detail === "string" ? detail : detail ? detail.map((d) => d.msg).join("; ") : response.statusText);
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

  // Slides: the deck and the speaker notes of this language, nothing of another.
  const slideItems = [];
  if (row.deck && row.deck.url) slideItems.push(el("div", {},
    el("a", {href: row.deck.url, target: "_blank", rel: "noopener", style: "font-weight:600"}, "Google Slides")));
  if (row.slides_file) {
    slideItems.push(el("div", {class: "muted small"},
      el("a", {href: fileUrl(row.slides_file), target: "_blank", rel: "noopener", title: row.slides_file}, t("speakerNotes"))));
    if (!(row.deck && row.deck.url)) slideItems.push(el("div", {},
      el("button", {type: "button", class: "ghost", disabled: slidesBusy, onclick: () => createDeck(row)}, t("createDeckBtn"))));
  } else {
    // No plan yet: slides are asked for here, with a person's approval (INV-6).
    slideItems.push(el("div", {},
      el("button", {type: "button", class: "ghost", disabled: slidesBusy, onclick: () => requestSlides(row)}, t("createSlidesBtn"))));
  }
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
  if (o.source_text) {
    recordSourceUsage(o.source_text, o.title || "", jobId || "");
  }
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
  // the agent, the slides lane makes plans, decks and PDFs.
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
  if (data.error) notices.push(el("div", {class: "notice err"}, el("span", {class: "grow"}, t("stoppedWithError", {error: data.error}))));
  if (data.notice) notices.push(el("div", {class: "notice note"}, el("span", {class: "grow"}, data.notice)));
  if (!postsBusy) for (const approval of data.approvals) notices.push(el("div", {class: "notice ask"},
    el("span", {class: "grow"}, approval.hint, data.approvals.length > 1
      ? el("div", {class: "muted small"}, t("oneAnswerAtATime")) : null),
    el("button", {type: "button", onclick: () => act("approval", {id: approval.id, confirmed: false})}, t("declineBtn")),
    el("button", {type: "button", class: "primary", onclick: () => act("approval", {id: approval.id, confirmed: true})}, t("approveBtn"))));
  // Rebuilt only when something changed: a button replaced between a press
  // and its release would swallow the click.
  const noticesKey = JSON.stringify([data.error, data.notice, postsBusy ? [] : data.approvals]);
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
    else { missed += 1; if (missed === 2) $("run").disabled = false; drawLive(); }
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
// Slides for a language: a plan and its deck. The dialog is the approval; it
// names the language, the length, the audience and the account (INV-6), and
// says what an approval replaces.
async function requestSlides(row) {
  let account = null;
  try { account = (await loadConfig()).slides_account; } catch (e) {}
  const hasPlan = Boolean(row.slides_file), hasDeck = Boolean(row.deck && row.deck.url);
  const summary = [
    [t("slidesLanguageLabel"), languageName(row)],
    [t("slidesLengthLabel"), t("slidesLengthValue", {minutes: row.slides_minutes})],
    [t("slidesAudienceLabel"), audienceText((state && state.overview && state.overview.slides_audience) || "software engineers")],
    [t("fromLabel"), t("slidesFromValue", {file: `post.${row.language}.md`})],
    [t("whereLabel"), account ? t("driveOf", {account}) : t("driveYours")],
  ];
  if (hasPlan) summary.push([t("planLabel"), t("slidesPlanReplaced")]);
  if (hasDeck) summary.push([t("deckLabel"), t("slidesDeckKept")]);
  const ok = await confirmAction({
    title: hasPlan ? t("slidesAgainTitle", {language: languageName(row)}) : t("slidesTitle", {language: languageName(row)}),
    summary: summary,
    okLabel: hasPlan ? t("slidesAgainOk") : t("slidesOk")});
  if (ok) act("slides", {languages: [row.language], minutes: 0, audience: "", approved: true});
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

const PROMPT_HISTORY_KEY = "tba.prompt_history";
// The suggestions are texts of the catalogs (prompt.0 to prompt.5), in the
// same order in every language. They are filled in once the catalogs are read.
const ESSENTIAL_PROMPT_COUNT = 6;
const ESSENTIAL_PROMPTS_BY_LANG = {en: [], ko: [], ja: []};
function readEssentialPrompts() {
  for (const lang of UI_LANGS) {
    ESSENTIAL_PROMPTS_BY_LANG[lang] = Array.from({length: ESSENTIAL_PROMPT_COUNT}, (_, i) => textIn(lang, "prompt." + i));
  }
}
const DEFAULT_QUICK_INDICES = [4, 5, 0];

// i18n:legacy-begin
// Reading a request typed in words, and writing it again in another language,
// is taken out by WP-13 together with the words of three languages it needs.
function parseSlidePromptIntent(text) {
  const s = (text || "").trim();
  if (!s || s.length > 90) return null;
  if (!/(slide|slides|deck|슬라이드|덱|スライド|デッキ)/i.test(s)) return null;
  if (!/(create|re-create|recreate|generate|make|build|제작|생성|만들|다시|재생성|재제작|作成|再作成|作っ)/i.test(s)) return null;
  const langHits = [];
  const patterns = [
    ["en", /\b(english|en)\b|영어|영문|英語/i],
    ["ko", /\b(korean|ko)\b|한국어|국문|韓国語/i],
    ["ja", /\b(japanese|ja)\b|일본어|일문|日本語/i],
  ];
  for (const [code, rx] of patterns) {
    const m = rx.exec(s);
    if (m) langHits.push([m.index, code]);
  }
  if (langHits.length === 0) return null;
  langHits.sort((a, b) => a[0] - b[0]);
  const langs = langHits.map((x) => x[1]);
  const recreate = /(re-create|recreate|again|다시|재생성|재제작|再作成|作り直)/i.test(s);
  const withDeck = /(google slides|deck|덱|デッキ)/i.test(s);
  return {langs, recreate, withDeck};
}

function formatSlidePromptIntent(intent, targetLang) {
  const {langs, recreate, withDeck} = intent;
  if (targetLang === "ko") {
    const names = {en: "영어", ko: "한국어", ja: "일본어"};
    const joined = langs.length === 2
      ? `${names[langs[0]]}와 ${names[langs[1]]}`
      : langs.map((l) => names[l]).join(", ");
    const targetObj = withDeck ? "발표 슬라이드와 Google Slides 덱을" : "슬라이드를";
    return recreate ? `${joined} ${targetObj} 다시 만들어줘` : `${joined} ${targetObj} 제작해`;
  }
  if (targetLang === "ja") {
    const names = {en: "英語", ko: "韓国語", ja: "日本語"};
    const joined = langs.map((l) => names[l]).join("と");
    const targetObj = withDeck ? "のスライドとGoogle Slidesデッキを" : "のスライドを";
    return recreate ? `${joined}${targetObj}再作成して` : `${joined}${targetObj}作成して`;
  }
  const names = {en: "English", ko: "Korean", ja: "Japanese"};
  const joined = langs.length === 1
    ? names[langs[0]]
    : langs.length === 2
      ? `${names[langs[0]]} and ${names[langs[1]]}`
      : `${names[langs[0]]}, ${names[langs[1]]}, and ${names[langs[2]]}`;
  const targetObj = withDeck ? "slides and Google Slides deck" : "slides";
  return `${recreate ? "Re-create" : "Create"} the ${joined} ${targetObj}`;
}

function localizeBuiltInPrompt(text, targetLang) {
  const clean = (text || "").trim().toLowerCase();
  const targetList = ESSENTIAL_PROMPTS_BY_LANG[targetLang] || ESSENTIAL_PROMPTS_BY_LANG.en;
  for (const list of Object.values(ESSENTIAL_PROMPTS_BY_LANG)) {
    const idx = list.findIndex((p) => p.toLowerCase() === clean);
    if (idx >= 0 && targetList[idx]) return targetList[idx];
  }
  const intent = parseSlidePromptIntent(text);
  if (intent) return formatSlidePromptIntent(intent, targetLang);
  return text;
}

function localizePromptItem(item, targetLang) {
  if (!item) return "";
  const builtIn = localizeBuiltInPrompt(item.text, targetLang);
  if (builtIn !== item.text) return builtIn;
  if (item.i18n && typeof item.i18n[targetLang] === "string" && item.i18n[targetLang].trim()) {
    return item.i18n[targetLang].trim();
  }
  return item.text;
}

function isConversationalReply(text) {
  return /^(yes|no|ok|okay|sure|네|예|응|아니)\b/i.test(text.trim());
}
// i18n:legacy-end

function getPromptHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(PROMPT_HISTORY_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((item) => item && typeof item.text === "string" && item.text.trim() && !isConversationalReply(item.text))
      .sort((a, b) => (b.count || 0) - (a.count || 0) || (b.lastUsed || 0) - (a.lastUsed || 0));
  } catch (e) {
    return [];
  }
}

function savePromptHistory(list) {
  try { localStorage.setItem(PROMPT_HISTORY_KEY, JSON.stringify(list.slice(0, 50))); } catch (e) {}
}

let _localizingPrompts = false;
async function ensurePromptHistoryLocalized() {
  if (_localizingPrompts) return;
  const list = getPromptHistory();
  const missing = list.filter((item) => {
    const isStructured = localizeBuiltInPrompt(item.text, "en") !== item.text
      || localizeBuiltInPrompt(item.text, "ko") !== item.text
      || localizeBuiltInPrompt(item.text, "ja") !== item.text;
    if (isStructured) return false;
    return !item.i18n || !item.i18n.en || !item.i18n.ko || !item.i18n.ja;
  });
  if (missing.length === 0) return;
  _localizingPrompts = true;
  try {
    const res = await api("/api/prompts/localize", {texts: missing.map((m) => m.text)});
    const map = (res && res.translations) || {};
    let changed = false;
    const updated = getPromptHistory();
    for (const item of updated) {
      const tr = map[item.text];
      if (tr && typeof tr === "object" && (tr.en || tr.ko || tr.ja)) {
        item.i18n = {
          en: tr.en || item.text,
          ko: tr.ko || item.text,
          ja: tr.ja || item.text,
        };
        changed = true;
      }
    }
    if (changed) {
      savePromptHistory(updated);
      renderPromptChips();
      if ($("promptsModal") && $("promptsModal").open) renderPromptsModal();
    }
  } catch (e) {
  } finally {
    _localizingPrompts = false;
  }
}

function recordPromptUsage(text) {
  const clean = text.trim();
  if (!clean || isConversationalReply(clean)) return;
  const canonical = localizeBuiltInPrompt(clean, "en");
  const list = getPromptHistory();
  const idx = list.findIndex((item) => localizeBuiltInPrompt(item.text, "en").toLowerCase() === canonical.toLowerCase());
  if (idx >= 0) {
    list[idx].text = canonical;
    list[idx].count = (list[idx].count || 1) + 1;
    list[idx].lastUsed = Date.now();
  } else {
    list.push({text: canonical, count: 1, lastUsed: Date.now()});
  }
  savePromptHistory(list);
  renderPromptChips();
  ensurePromptHistoryLocalized();
}

function deletePromptHistoryItem(text) {
  const canonical = localizeBuiltInPrompt(text, "en").toLowerCase();
  const list = getPromptHistory().filter((item) => localizeBuiltInPrompt(item.text, "en").toLowerCase() !== canonical);
  savePromptHistory(list);
  renderPromptChips();
  renderPromptsModal();
}

// The built-in request to localize again, in the language of the page.
const RETRANSLATE_PROMPT = 3;  // its place in ESSENTIAL_PROMPTS_BY_LANG
function offerRetranslation() {
  const list = ESSENTIAL_PROMPTS_BY_LANG[currentUiLang] || ESSENTIAL_PROMPTS_BY_LANG.en;
  fillPromptInput(list[RETRANSLATE_PROMPT]);
}

function fillPromptInput(text) {
  const input = $("message");
  input.value = text;
  if ($("promptsModal") && $("promptsModal").open) $("promptsModal").close();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function getTopPromptChips(limit = 3) {
  const lang = currentUiLang || "en";
  const essentials = ESSENTIAL_PROMPTS_BY_LANG[lang] || ESSENTIAL_PROMPTS_BY_LANG.en;
  const defaults = DEFAULT_QUICK_INDICES.map((i) => essentials[i]);
  const history = getPromptHistory().map((item) => localizePromptItem(item, lang));
  const picked = [];
  for (const h of history) {
    if (picked.length >= limit) break;
    if (!picked.some((p) => p.toLowerCase() === h.toLowerCase())) picked.push(h);
  }
  for (const seed of defaults) {
    if (picked.length >= limit) break;
    if (!picked.some((p) => p.toLowerCase() === seed.toLowerCase())) picked.push(seed);
  }
  return picked.slice(0, limit);
}

function renderPromptChips() {
  const container = $("promptChipsList");
  if (!container) return;
  const chips = getTopPromptChips(3).map((text) =>
    el("button", {
      type: "button",
      class: "prompt-chip",
      title: text,
      onclick: () => fillPromptInput(text),
    }, text)
  );
  container.replaceChildren(...chips);
}

function renderPromptsModal() {
  const lang = currentUiLang || "en";
  const essentials = ESSENTIAL_PROMPTS_BY_LANG[lang] || ESSENTIAL_PROMPTS_BY_LANG.en;
  const essentialBox = $("essentialPromptsList");
  if (essentialBox) {
    essentialBox.replaceChildren(...essentials.map((text) =>
      el("div", {class: "prompt-modal-item"},
        el("button", {type: "button", class: "prompt-fill", onclick: () => fillPromptInput(text)}, text))
    ));
  }
  const historyBox = $("historyPromptsList");
  if (historyBox) {
    const history = getPromptHistory();
    if (history.length === 0) {
      historyBox.replaceChildren(el("span", {class: "muted small"}, t("noHistoryText")));
    } else {
      historyBox.replaceChildren(...history.map((item) => {
        const shownText = localizePromptItem(item, lang);
        return el("div", {class: "prompt-modal-item"},
          el("button", {type: "button", class: "prompt-fill", onclick: () => fillPromptInput(shownText)}, shownText),
          el("span", {class: "badge", style: "font-size:11px"}, `×${item.count || 1}`),
          el("button", {
            type: "button",
            class: "prompt-del",
            title: t("removeFromHistory"),
            onclick: () => deletePromptHistoryItem(item.text),
          }, "×"));
      }));
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
    renderPromptChips();
    renderPromptsModal();
  });
}

const SOURCE_HISTORY_KEY = "tba.source_history";
const SOURCE_HIDDEN_KEY = "tba.source_hidden";

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

function getHiddenSources() {
  try { return JSON.parse(localStorage.getItem(SOURCE_HIDDEN_KEY) || "[]"); } catch (e) { return []; }
}

function getSourceHistory() {
  try {
    const hidden = new Set(getHiddenSources());
    const raw = JSON.parse(localStorage.getItem(SOURCE_HISTORY_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((item) => item && typeof item.text === "string" && item.text.trim() && !hidden.has(cleanSourceText(item.text)))
      .sort((a, b) => (b.lastUsed || 0) - (a.lastUsed || 0));
  } catch (e) {
    return [];
  }
}

function saveSourceHistory(list) {
  try { localStorage.setItem(SOURCE_HISTORY_KEY, JSON.stringify(list.slice(0, 30))); } catch (e) {}
}

function recordSourceUsage(text, title = "", runJobId = "", timestamp = null) {
  const clean = cleanSourceText(text);
  if (!clean) return;
  const hidden = new Set(getHiddenSources());
  if (timestamp !== null && hidden.has(clean)) return;
  const list = getSourceHistory();
  const idx = list.findIndex((item) => cleanSourceText(item.text) === clean);
  const now = timestamp || Date.now();
  if (idx >= 0) {
    list[idx].text = clean;
    if (title) list[idx].title = title;
    if (runJobId) list[idx].jobId = runJobId;
    if (!timestamp || now > (list[idx].lastUsed || 0)) list[idx].lastUsed = now;
  } else {
    list.push({text: clean, title: title || "", jobId: runJobId || "", lastUsed: now});
  }
  saveSourceHistory(list);
}

function deleteSourceHistoryItem(text) {
  const clean = cleanSourceText(text);
  const hidden = getHiddenSources();
  if (!hidden.includes(clean)) {
    hidden.push(clean);
    try { localStorage.setItem(SOURCE_HIDDEN_KEY, JSON.stringify(hidden)); } catch (e) {}
  }
  const list = getSourceHistory().filter((item) => cleanSourceText(item.text) !== clean);
  saveSourceHistory(list);
  renderSourcesModal();
}

function fillSourceInput(text) {
  $("source").value = text;
  if ($("sourcesModal") && $("sourcesModal").open) $("sourcesModal").close();
  $("source").focus();
}

function renderSourcesModal() {
  const box = $("sourcesHistoryList");
  if (!box) return;
  const history = getSourceHistory();
  if (history.length === 0) {
    box.replaceChildren(el("span", {class: "muted small"}, t("noSourceHistoryText")));
    return;
  }
  box.replaceChildren(...history.map((item) => {
    const isUrl = /^https?:\/\/\S+$/i.test(item.text);
    const firstLine = item.text.split("\n").find((l) => l.trim()) || item.text;
    const primaryTitle = item.title || (isUrl ? item.text : firstLine.replace(/^#+\s*/, "").slice(0, 80));
    const subLabel = isUrl ? item.text : t("sourceDraftLabel", {count: item.text.length.toLocaleString()});
    return el("div", {class: "prompt-modal-item"},
      el("button", {
        type: "button",
        class: "prompt-fill",
        style: "display:grid; gap:2px",
        onclick: () => fillSourceInput(item.text),
      },
        el("span", {style: "font-weight:600; font-size:13px"}, primaryTitle),
        primaryTitle !== subLabel ? el("span", {class: "muted small", style: "font-size:11.5px"}, subLabel) : null
      ),
      item.jobId ? el("button", {
        type: "button",
        class: "ghost small",
        style: "font-size:11.5px; height:24px; padding:0 8px; border:1px solid var(--line); border-radius:4px; white-space:nowrap",
        onclick: () => {
          $("source").value = item.text;
          if ($("sourcesModal") && $("sourcesModal").open) $("sourcesModal").close();
          watch(item.jobId);
        },
      }, t("openRunBtn")) : null,
      el("button", {
        type: "button",
        class: "prompt-del",
        title: t("removeFromHistory"),
        onclick: () => deleteSourceHistoryItem(item.text),
      }, "×")
    );
  }));
}

async function openSourcesModal() {
  renderSourcesModal();
  $("sourcesModal").showModal();
  try {
    const res = await api("/api/jobs");
    if (res && Array.isArray(res.jobs)) {
      for (const j of res.jobs) {
        recordSourceUsage(j.source_text, j.title || "", j.job_id, (j.created || 0) * 1000);
      }
      renderSourcesModal();
    }
  } catch (e) {}
}
if ($("openSourcesModal")) $("openSourcesModal").addEventListener("click", openSourcesModal);
if ($("closeSourcesModal")) $("closeSourcesModal").addEventListener("click", () => $("sourcesModal").close());
if ($("clearSourceHistory")) {
  $("clearSourceHistory").addEventListener("click", () => {
    const all = getSourceHistory().map((i) => cleanSourceText(i.text));
    try { localStorage.setItem(SOURCE_HIDDEN_KEY, JSON.stringify([...new Set([...getHiddenSources(), ...all])])); } catch (e) {}
    saveSourceHistory([]);
    renderSourcesModal();
  });
}

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
    recordSourceUsage(text, "", started.job_id);
    for (const key of Object.keys(typed)) delete typed[key];
    state = {log: []};
    $("rows").replaceChildren(); $("log").textContent = "";
    await watch(started.job_id);
    $("progress").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { toast(error.message, 6000); $("run").disabled = false; }
});

// The page starts once the texts are read: the suggestions, the texts written
// into the page, then the last settings and the run this browser had open.
loadCatalogs().then(() => {
  readEssentialPrompts();
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
