// The texts the page, the editor and the previews show come from one catalog
// per language (i18n/en.json, ko.json, ja.json) with the same keys (WEB-13).
// Nothing a person reads is written in the scripts; they ask for it by key.
const UI_LANGS = ["en", "ko", "ja"];
const UI_LANG_KEY = "tba.ui_lang";
const CATALOGS = {};
let currentUiLang = UI_LANGS.includes(localStorage.getItem(UI_LANG_KEY)) ? localStorage.getItem(UI_LANG_KEY) : "en";

// All three are read: a suggestion is recognised in whichever language it was sent.
async function loadCatalogs() {
  await Promise.all(UI_LANGS.map(async (lang) => {
    try {
      const response = await fetch(`/static/i18n/${lang}.json`);
      if (response.ok) CATALOGS[lang] = await response.json();
    } catch (e) {}
  }));
}

// The text of ``key`` in ``lang``, with each {name} replaced from ``params``.
// A text that language lacks is shown in English. When no catalog has the key,
// ``otherwise`` is shown: the English sentence the server sent with a code, or
// what the page already holds. Without ``otherwise`` the key itself is shown.
function textIn(lang, key, params, otherwise) {
  let text = (CATALOGS[lang] || {})[key];
  if (typeof text !== "string") text = (CATALOGS.en || {})[key];
  if (typeof text !== "string") return otherwise === undefined ? key : otherwise;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (params && params[name] !== undefined ? String(params[name]) : whole));
}
function t(key, params, otherwise) {
  return textIn(currentUiLang, key, params, otherwise);
}
// A sentence of the server in the language of the page. A notice or a refusal
// comes in parts, each the English sentence with a code and the values it was
// filled with; server.<code> is its text. A part without a code, or with a
// code no catalog has, is shown as the server wrote it. A value that is a
// language code, or a list of them, is written as the page names languages;
// every other value (what a platform answered, a title) is shown as it is.
function serverText(part) {
  if (!part || !part.code) return part ? part.text : "";
  const params = {};
  for (const [name, value] of Object.entries(part.params || {})) {
    const named = (code) => t("lang." + code, null, code);
    params[name] = Array.isArray(value) ? value.map(named).join(", ") : name === "language" ? named(value) : value;
  }
  return t("server." + part.code, params, part.text);
}
const serverSentence = (parts) => (parts || []).map(serverText).join(" ");
// Why the server refused a request, from its answer: in the language of the
// page when the answer carries codes, else the sentence it wrote.
function refusalText(data, otherwise) {
  if (data && Array.isArray(data.notices) && data.notices.length) return serverSentence(data.notices);
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  return Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : otherwise;
}
// A text that counts: ``key.one`` for exactly one, ``key.other`` otherwise.
function tn(key, n, params) {
  return t(`${key}.${n === 1 ? "one" : "other"}`, {n, ...params});
}

// Texts written into the page itself: data-i18n names the text of an element,
// data-i18n-placeholder, data-i18n-title and data-i18n-aria-label the text of
// that attribute, data-i18n-params (JSON) the values the text takes.
const I18N_ATTRIBUTES = [["i18nPlaceholder", "placeholder"], ["i18nTitle", "title"], ["i18nAriaLabel", "aria-label"]];
function applyStaticTexts() {
  document.documentElement.lang = currentUiLang;
  const params = (node) => (node.dataset.i18nParams ? JSON.parse(node.dataset.i18nParams) : undefined);
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n, params(node), node.textContent);
  }
  for (const [name, attribute] of I18N_ATTRIBUTES) {
    for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) {
      node.setAttribute(attribute, t(node.dataset[name], params(node), node.getAttribute(attribute) || ""));
    }
  }
}
