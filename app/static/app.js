const $ = (id) => document.getElementById(id);
const UI_TRANSLATIONS = {
  en: {
    subtitle: "Automatically transform an article into blog posts and presentation slides across three languages.",
    settings: "Settings",
    sourceTitle: "Source document",
    sourceHint: "Share a post URL, a .md file, or your document.",
    clearBtn: "Clear / New article",
    attachBtn: "Attach .md file",
    dropHint: "or drop a file anywhere on this card",
    slideLegend: "Slide options",
    slideHint: "Choose the slide length to create Google Slides in your configured Google Drive.",
    lengthLabel: "Length",
    languagesLabel: "Languages",
    audienceLabel: "Audience",
    formFoot: "Takes a few minutes. You review everything before anything is posted.",
    runBtn: "Run",
    askTitle: "Ask the agent for a change",
    askPlaceholder: "Ask the agent for a change (e.g. replace examples, adjust tone)...",
    sendBtn: "Send",
    logTitle: "Agent activity log",
    settingsTitle: "Platforms & integrations",
    settingsHint: "Blog platforms per language and Google Slides integration status.",
    setupGuideLink: "Setup guide",
    settingsNoteText: "Credentials stay on the server; this page never sees them.",
    howApi: "Posted through the API",
    howHtml: "Paste-ready HTML export",
    statusReady: "✓ Ready",
    statusConnected: "✓ Connected",
    statusNoCred: "No credential · Setup",
    statusSlidesOk: "✓ Access granted",
    statusSlidesNo: "Not set up · Setup",
    diagramsOn: "Diagrams drawn",
    diagramsOff: "Diagrams off",
    langSettingLabel: "Interface language",
    langSettingHint: "Choose the display language for this web page.",
    doneBtn: "Done",
    colLang: "Language",
    colTitle: "Title",
    colFiles: "Files",
    colSlides: "Slides",
    colNext: "Next step",
    createSlidesBtn: "Create slides…",
    createDeckBtn: "Create deck…",
    recreateDeckBtn: "Re-create slides…",
    speakerNotes: "Speaker notes",
    morePromptsBtn: "More prompts…",
    promptsModalTitle: "Prompt library & history",
    promptsModalHint: "Click a prompt to fill the box, edit if needed, and press Send.",
    essentialPromptsLabel: "Essential actions",
    historyPromptsLabel: "Your popular prompts",
    clearHistoryBtn: "Clear history",
    noHistoryText: "No custom prompts sent yet.",
    recentSourcesBtn: "Recent sources…",
    sourcesModalTitle: "Recent sources",
    sourcesModalHint: "Click an item to fill the source box, or open its previous run.",
    noSourceHistoryText: "No source documents recorded yet.",
    openRunBtn: "Open run",
    openOriginal: "Open original",
    openPost: "Open post",
    openDraft: "Open draft",
    finalizeBtn: "Finalize",
    postBtn: "Post…",
    updatePostBtn: "Update post…",
    registerUrlBtn: "Register URL",
    finishedText: "Finished. Review the results below.",
    stoppedText: "Stopped.",
    summaryRunText: "· Summary of this run: ",
    workingText: "Working",
    elapsedText: "elapsed",
    stepNames: {},
    langNames: {Korean: "Korean", English: "English", Japanese: "Japanese"},
    statusLabels: {
      not_posted: "Not posted",
      exported_for_manual_post: "Ready to paste",
      draft: "Draft created",
      published: "Published",
      existing_source: "Original",
    },
  },
  ko: {
    subtitle: "글 하나를 3개 국어 블로그 포스트와 발표 슬라이드로 자동 변환합니다.",
    settings: "설정",
    sourceTitle: "원본 문서",
    sourceHint: "블로그 글 URL, .md 파일 또는 본문 텍스트를 입력하세요.",
    clearBtn: "초기화 / 새 글 작성",
    attachBtn: ".md 파일 첨부",
    dropHint: "또는 이 카드 위에 파일을 끌어다 놓으세요",
    slideLegend: "슬라이드 옵션",
    slideHint: "연결된 Google Drive에 생성할 발표 슬라이드 분량을 선택하세요.",
    lengthLabel: "발표 분량",
    languagesLabel: "생성 언어",
    audienceLabel: "대상 청중",
    formFoot: "약 1~2분 소요됩니다. 모든 결과물은 게시 전에 직접 검토하고 수정할 수 있습니다.",
    runBtn: "실행",
    askTitle: "에이전트에게 수정 요청",
    askPlaceholder: "수정할 내용을 입력하세요 (예: 제목 축약, 문체 변경, 슬라이드 재생성)...",
    sendBtn: "전송",
    logTitle: "에이전트 실행 로그",
    settingsTitle: "연동 플랫폼 및 서비스",
    settingsHint: "언어별 블로그 플랫폼 및 Google Slides 연동 상태입니다.",
    setupGuideLink: "설정 가이드",
    settingsNoteText: "인증 정보는 서버에만 안전하게 보관되며 웹 페이지로 노출되지 않습니다.",
    howApi: "REST API 자동 게시",
    howHtml: "붙여넣기용 HTML 생성",
    statusReady: "✓ 사용 가능",
    statusConnected: "✓ 연결됨",
    statusNoCred: "미설정 · 가이드",
    statusSlidesOk: "✓ 연결됨",
    statusSlidesNo: "미설정 · 가이드",
    diagramsOn: "다이어그램 이미지 변환 켜짐",
    diagramsOff: "다이어그램 이미지 변환 꺼짐",
    langSettingLabel: "화면 표시 언어",
    langSettingHint: "웹 페이지에 표시할 언어를 선택하세요.",
    doneBtn: "완료",
    colLang: "언어",
    colTitle: "제목",
    colFiles: "파일",
    colSlides: "슬라이드",
    colNext: "다음 단계",
    createSlidesBtn: "슬라이드 생성…",
    createDeckBtn: "덱 생성…",
    recreateDeckBtn: "슬라이드 재생성…",
    speakerNotes: "발표자 노트",
    morePromptsBtn: "전체 프롬프트…",
    promptsModalTitle: "프롬프트 라이브러리 및 히스토리",
    promptsModalHint: "항목을 클릭해 입력창에 채운 뒤 필요시 수정하고 전송을 누르세요.",
    essentialPromptsLabel: "필수 기능 프롬프트",
    historyPromptsLabel: "자주 사용한 프롬프트 (인기순)",
    clearHistoryBtn: "기록 비우기",
    noHistoryText: "아직 직접 전송한 프롬프트 기록이 없습니다.",
    recentSourcesBtn: "최근 원본 문서…",
    sourcesModalTitle: "최근 원본 문서",
    sourcesModalHint: "항목을 클릭해 입력창에 채우거나 이전 실행 결과를 바로 여세요.",
    noSourceHistoryText: "아직 저장된 원본 문서 기록이 없습니다.",
    openRunBtn: "결과 열기",
    openOriginal: "원본 글 열기",
    openPost: "게시된 글 열기",
    openDraft: "초안 열기",
    finalizeBtn: "검토 완료",
    postBtn: "게시하기…",
    updatePostBtn: "게시물 업데이트…",
    registerUrlBtn: "URL 등록",
    finishedText: "완료되었습니다. 아래에서 결과를 확인하세요.",
    stoppedText: "중지됨.",
    summaryRunText: "· 실행 요약 보고서: ",
    workingText: "작업 진행 중",
    elapsedText: "경과",
    stepNames: {
      "Source document": "원본 문서 확인",
      "Transform": "번역 및 변환",
      "Prepare posts": "블로그 글 준비",
      "Prepare slides": "슬라이드 제작",
      "Summarize": "결과 요약",
      "Review & edit": "검토 및 편집",
      "Finalize posts": "원고 확정",
      "Approve & draft": "승인 및 초안 생성",
      "Publish posts": "블로그 게시",
      "Verify & complete": "게시 확인 및 완료",
    },
    langNames: {Korean: "한국어", English: "영어", Japanese: "일본어"},
    statusLabels: {
      not_posted: "미게시",
      exported_for_manual_post: "붙여넣기 준비됨",
      draft: "초안 생성됨",
      published: "게시 완료",
      existing_source: "원본",
    },
  },
  ja: {
    subtitle: "記事から3言語のブログ投稿と発表スライドを自動生成します。",
    settings: "設定",
    sourceTitle: "元記事（ソース）",
    sourceHint: "記事のURL、.mdファイル、または本文テキストを入力してください。",
    clearBtn: "クリア / 新規作成",
    attachBtn: ".mdファイルを添付",
    dropHint: "またはこのカードにファイルをドロップ",
    slideLegend: "スライド作成オプション",
    slideHint: "連携済みのGoogle Driveに作成する発表スライドの分量を指定します。",
    lengthLabel: "発表時間・枚数",
    languagesLabel: "作成する言語",
    audienceLabel: "対象読者・聴衆",
    formFoot: "1〜2分ほどかかります。公開前にすべての内容を確認・編集できます。",
    runBtn: "実行",
    askTitle: "エージェントに修正を依頼",
    askPlaceholder: "修正したい内容を入力してください（例: タイトルの短縮、トーン調整、スライド再作成）...",
    sendBtn: "送信",
    logTitle: "エージェント実行ログ",
    settingsTitle: "連携プラットフォーム・サービス",
    settingsHint: "言語別のブログ投稿先とGoogle Slidesの連携ステータスです。",
    setupGuideLink: "セットアップガイド",
    settingsNoteText: "認証情報はサーバー側で安全に管理され、この画面には表示されません。",
    howApi: "REST API 自動連携",
    howHtml: "貼り付け用 HTML 出力",
    statusReady: "✓ 利用可能",
    statusConnected: "✓ 連携済み",
    statusNoCred: "未設定 · ガイド",
    statusSlidesOk: "✓ 連携済み",
    statusSlidesNo: "未設定 · ガイド",
    diagramsOn: "図表レンダリング有効",
    diagramsOff: "図表レンダリング無効",
    langSettingLabel: "画面の表示言語",
    langSettingHint: "このページの表示言語を選択してください。",
    doneBtn: "完了",
    colLang: "言語",
    colTitle: "タイトル",
    colFiles: "ファイル",
    colSlides: "スライド",
    colNext: "次のステップ",
    createSlidesBtn: "スライド作成…",
    createDeckBtn: "デッキ作成…",
    recreateDeckBtn: "スライド再作成…",
    speakerNotes: "発表者ノート",
    morePromptsBtn: "すべてのプロンプト…",
    promptsModalTitle: "プロンプトライブラリ＆履歴",
    promptsModalHint: "項目をクリックして入力欄に入れ、必要に応じて編集し送信します。",
    essentialPromptsLabel: "定番プロンプト",
    historyPromptsLabel: "よく使うプロンプト（人気順）",
    clearHistoryBtn: "履歴をクリア",
    noHistoryText: "まだ送信されたカスタムプロンプトはありません。",
    recentSourcesBtn: "最近のソース…",
    sourcesModalTitle: "最近のソース記事",
    sourcesModalHint: "項目をクリックして入力欄に入れるか、前回の実行結果を開きます。",
    noSourceHistoryText: "まだ記録されたソース記事はありません。",
    openRunBtn: "結果を開く",
    openOriginal: "元記事を開く",
    openPost: "公開記事を開く",
    openDraft: "下書きを開く",
    finalizeBtn: "確定する",
    postBtn: "投稿する…",
    updatePostBtn: "投稿を更新…",
    registerUrlBtn: "URLを登録",
    finishedText: "完了しました。以下の結果をご確認ください。",
    stoppedText: "停止しました。",
    summaryRunText: "· 実行サマリー: ",
    workingText: "処理中",
    elapsedText: "経過",
    stepNames: {
      "Source document": "元記事の取得",
      "Transform": "翻訳・変換",
      "Prepare posts": "ブログ記事の作成",
      "Prepare slides": "スライドの作成",
      "Summarize": "サマリー作成",
      "Review & edit": "確認・編集",
      "Finalize posts": "記事の確定",
      "Approve & draft": "承認・下書き作成",
      "Publish posts": "記事の公開",
      "Verify & complete": "公開確認・完了",
    },
    langNames: {Korean: "韓国語", English: "英語", Japanese: "日本語"},
    statusLabels: {
      not_posted: "未投稿",
      exported_for_manual_post: "貼り付け準備完了",
      draft: "下書き作成済み",
      published: "公開済み",
      existing_source: "原文",
    },
  }
};
const UI_LANG_KEY = "tba.ui_lang";
let currentUiLang = localStorage.getItem(UI_LANG_KEY) || "en";

function applyUiLanguage(lang) {
  if (!UI_TRANSLATIONS[lang]) lang = "en";
  currentUiLang = lang;
  localStorage.setItem(UI_LANG_KEY, lang);
  document.documentElement.lang = lang;
  if ($("uiLanguageSelect")) $("uiLanguageSelect").value = lang;
  const t = UI_TRANSLATIONS[lang];
  for (const [key, text] of Object.entries(t)) {
    const nodes = document.querySelectorAll(`[data-i18n="${key}"]`);
    nodes.forEach((node) => {
      if (node.tagName === "INPUT" || node.tagName === "TEXTAREA") {
        node.placeholder = text;
      } else {
        node.textContent = text;
      }
    });
  }
  const ths = document.querySelectorAll("table.results thead th");
  if (ths.length >= 5) {
    ths[0].textContent = t.colLang;
    ths[1].textContent = t.colTitle;
    ths[2].textContent = t.colFiles;
    ths[3].textContent = t.colSlides;
    ths[4].textContent = t.colNext;
  }
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

const STATUS = {
  not_posted: ["Not posted", ""], exported_for_manual_post: ["Ready to paste", "warn"],
  draft: ["Draft created", "ok"], published: ["Published", "ok"], existing_source: ["Original", "ok"],
};
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
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
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
const fileUrl = (name) => `/api/jobs/${jobId}/files/${encodeURIComponent(name)}`;

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
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
  // "Ready to paste" would be untrue for a text nobody has finalized yet.
  const shown = row.status === "exported_for_manual_post" && row.next === "review" ? "not_posted" : row.status;
  const [, tone] = STATUS[shown] || [shown, ""];
  const label = (t.statusLabels && t.statusLabels[shown]) || (STATUS[shown] ? STATUS[shown][0] : shown);
  const langName = (t.langNames && t.langNames[row.language_name]) || row.language_name;

  // Files: the posts. Editing starts from the preview a link opens.
  const files = el("td", {class: "files"}, ...row.files.map((name) => el("div", {},
    el("a", {href: fileUrl(name), target: "_blank", rel: "noopener"}, name))));

  // Slides: the deck and the speaker notes of this language, nothing of another.
  const slideItems = [];
  if (row.deck && row.deck.url) slideItems.push(el("div", {},
    el("a", {href: row.deck.url, target: "_blank", rel: "noopener", style: "font-weight:600"}, "Google Slides")));
  if (row.slides_file) {
    slideItems.push(el("div", {class: "muted small"},
      el("a", {href: fileUrl(row.slides_file), target: "_blank", rel: "noopener", title: row.slides_file}, t.speakerNotes || "Speaker notes")));
    if (!(row.deck && row.deck.url)) slideItems.push(el("div", {},
      el("button", {type: "button", class: "ghost", disabled: slidesBusy, onclick: () => createDeck(row)}, t.createDeckBtn || "Create deck…")));
  } else {
    // No plan yet: slides are asked for here, with a person's approval (INV-6).
    slideItems.push(el("div", {},
      el("button", {type: "button", class: "ghost", disabled: slidesBusy, onclick: () => requestSlides(row)}, t.createSlidesBtn || "Create slides…")));
  }
  const slidesCol = el("td", {class: "slides-col"}, ...slideItems);

  // Next step: review, finalize, approve, post. Only the one that is next is shown.
  const next = el("div", {class: "next"});
  const link = (text) => el("a", {href: row.url, target: "_blank", rel: "noopener"}, text);
  const drafted = row.status === "draft";
  if (row.next === "original") next.append(link(t.openOriginal || "Open original"));
  else if (row.next === "public") next.append(link(t.openPost || "Open post"));
  else {
    if (row.url) next.append(link(drafted ? (t.openDraft || "Open draft") : (t.openPost || "Open post")));
    if (row.next === "review") {
      const reviewHint = currentUiLang === "ko"
        ? (drafted ? "초안 생성 후 제목이나 본문이 수정되었습니다. 검토 완료를 눌러 반영하세요." : `post.${row.language}.md 파일을 검토하고 필요하면 수정한 뒤 검토 완료를 누르세요.`)
        : currentUiLang === "ja"
        ? (drafted ? "下書き作成後にタイトルまたは本文が変更されました。確定して更新してください。" : `post.${row.language}.md を確認・編集し、準備ができたら確定してください。`)
        : (drafted ? "The title or text changed after the draft was made. Finalize it to update the post." : `Review post.${row.language}.md and edit it if needed. Finalize it when it is ready.`);
      next.append(el("span", {class: "muted small"}, reviewHint),
        el("div", {class: "inline"}, el("button", {class: "primary", type: "button", disabled: pBusy, onclick: () => finalize(row)}, t.finalizeBtn || "Finalize")));
    } else if (row.next === "post") {
      const postHint = currentUiLang === "ko"
        ? "원고가 확정되었습니다. 게시 승인 시 태그와 초안/공개 여부를 선택할 수 있습니다."
        : currentUiLang === "ja"
        ? "確定済みです。承認時にタグと下書き・公開を選択できます。"
        : "Finalized. You choose tags and draft or public when you approve.";
      next.append(
        el("span", {class: "muted small"}, postHint),
        el("div", {class: "inline"}, el("button", {class: "primary", type: "button", disabled: pBusy,
          onclick: () => postDraft(row, drafted)}, drafted ? (t.updatePostBtn || "Update post…") : (t.postBtn || "Post…"))));
    } else if (row.next === "paste") {
      const pasteHint = currentUiLang === "ko"
        ? "원고가 확정되었습니다. naver_blog.html 내용을 네이버 에디터에 붙여넣어 발행한 뒤 URL을 등록하세요."
        : currentUiLang === "ja"
        ? "確定済みです。naver_blog.html をNaverエディタに貼り付けて公開し、URLを登録してください。"
        : "Finalized. Paste naver_blog.html into the Naver editor, post, then register the URL.";
      const url = keep(`url:${row.platform}`, {placeholder: "URL after you post it", "aria-label": `URL on ${row.platform_name}`});
      next.append(el("div", {class: "inline"}, url, el("button", {type: "button", disabled: pBusy, onclick: () => recordUrl(row, url.value)}, t.registerUrlBtn || "Register URL")),
        el("span", {class: "muted small"}, pasteHint));
    }
  }
  // A localization made from an earlier text of the source says so, and
  // offers to make it again: the request is put in the message box, unsent.
  const stale = row.older_than_source ? el("div", {class: "stale"},
    el("span", {class: "badge warn", title: "The source article was edited after this post was translated from it."}, "Older than the source"),
    el("button", {type: "button", class: "ghost small", onclick: offerRetranslation}, "Re-translate…")) : null;
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
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
  const readingText = currentUiLang === "ko" ? "원본 문서를 읽는 중…" : currentUiLang === "ja" ? "元記事を読み込み中…" : "Reading the source document…";
  const noArticleText = currentUiLang === "ko" ? "저장된 원고가 없습니다" : currentUiLang === "ja" ? "記事が保存されませんでした" : "No article was stored";
  $("articleTitle").textContent = o.title || (busy ? readingText : noArticleText);
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
    const label = (t.stepNames && t.stepNames[step.name]) || step.name;
    return el("li", {class: step.done ? "done" : (isNow ? "now" : "")}, (step.done ? "✓ " : "") + label);
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
  if (data.error) notices.push(el("div", {class: "notice err"}, el("span", {class: "grow"}, "Stopped with an error: " + data.error)));
  if (data.notice) notices.push(el("div", {class: "notice note"}, el("span", {class: "grow"}, data.notice)));
  if (!postsBusy) for (const approval of data.approvals) notices.push(el("div", {class: "notice ask"},
    el("span", {class: "grow"}, approval.hint, data.approvals.length > 1
      ? el("div", {class: "muted small"}, "The agent takes one answer at a time: answering this one declines the others.") : null),
    el("button", {type: "button", onclick: () => act("approval", {id: approval.id, confirmed: false})}, "Decline"),
    el("button", {type: "button", class: "primary", onclick: () => act("approval", {id: approval.id, confirmed: true})}, "Approve")));
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
  $("send").title = postsBusy ? "The agent is still working." : "";
}

function drawLive() {
  if (!state || !jobId || !state.overview) return;
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
  const lastLine = (state.log && state.log.length ? state.log[state.log.length - 1] : "").split("\n")[0];
  if (missed >= 2) {
    liveKey = null;
    $("elapsed").textContent = "";
    $("live").replaceChildren(el("span", {class: "muted"}, "Lost contact with the server. Trying again…"));
  } else if (state.busy) {
    liveKey = null;
    $("elapsed").textContent = `${clock(Date.now() - (busySince || Date.now()))} ${t.elapsedText || "elapsed"}`;
    $("live").replaceChildren(el("span", {class: "spinner"}), el("span", {}, t.workingText || "Working"),
      el("span", {class: "last"}, `· ${lastLine || "starting"} · quiet for ${clock(Date.now() - lastLogAt)}`));
  } else {
    const manifest = state.overview.manifest;
    const stopped = Boolean(state.error || (!state.overview.title && state.notice));
    const key = `${jobId}|${currentUiLang}|${stopped ? "stopped" : "finished"}|${manifest || ""}`;
    if (key === liveKey) return;   // keep the link in place; a rebuilt link can swallow a click
    liveKey = key;
    $("elapsed").textContent = "";
    const done = [el("span", {class: "muted"}, stopped ? (t.stoppedText || "Stopped.") : (t.finishedText || "Finished. Review the results below."))];
    if (manifest) done.push(el("span", {class: "muted"}, t.summaryRunText || "· Summary of this run: ", el("a", {href: fileUrl(manifest), target: "_blank", rel: "noopener"}, manifest)));
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
    if (error.status === 404) forget(state && state.overview ? "That run is not on this server any more. Its files remain in the workspace folder." : "");
    else { missed += 1; if (missed === 2) $("run").disabled = false; drawLive(); }
  }
}

const TAG_RULES = {
  qiita:
    'Qiita takes one to five tags without spaces, separated by commas.',
  devto:
    'DEV Community takes up to four tags, lowercase letters and digits only, separated by commas.',
};
function checkTags(platform, tags) {
  if (platform === "qiita") {
    if (tags.length < 1 || tags.length > 5 || tags.some((t) => /\s/.test(t)))
      return TAG_RULES.qiita;
  } else if (platform === "devto") {
    if (tags.length > 4 || tags.some((t) => !/^[a-z0-9]{1,30}$/.test(t)))
      return TAG_RULES.devto;
  }
  return null;
}
async function postDraft(row, update) {
  const isQiita = row.platform === "qiita";
  const ruleText = TAG_RULES[row.platform] || "";
  const tagInput = keep(`tags:${row.platform}`, {
    id: "confirmTags",
    placeholder: isQiita ? "Python, Docker, GCP, AI, Architecture" : "python, docker, gcp, tutorial",
    "aria-label": `Tags for ${row.platform_name}`,
  });
  const tagError = el("span", {class: "small hidden", style: "color:var(--bad); font-weight:400"}, "");
  tagInput.addEventListener("input", () => tagError.classList.add("hidden"));
  let parsedTags = [];
  // The person approving decides the tags and how far the post goes; the agent cannot.
  const visibility = await confirmAction({
    title: `${update ? "Update the post" : "Post"} on ${row.platform_name}?`,
    summary: [["Title", row.title]],
    extra: [el("label", {class: "field", for: "confirmTags"}, "Tags", tagInput, el("span", {class: "hint"}, ruleText), tagError)],
    validate: () => {
      parsedTags = tagInput.value.split(",").map((t) => (isQiita ? t.trim() : t.trim().toLowerCase())).filter(Boolean);
      const err = checkTags(row.platform, parsedTags);
      if (err) { tagError.textContent = err; tagError.classList.remove("hidden"); tagInput.focus(); return false; }
      return true;
    },
    choices: [
      {value: "draft", label: "Draft", hint: "Not public. You publish it yourself on the site."},
      {value: "public", label: "Public", hint: "Visible to everyone as soon as you approve."}],
    note: "This uses your account on that platform.", okLabel: "Approve and post"});
  if (visibility) act("draft", {platform: row.platform, tags: parsedTags, approved: true, public: visibility === "public"});
}
async function finalize(row) {
  try {
    let done = await api(`/api/jobs/${jobId}/finalize`, {language: row.language});
    if (done.needs_acknowledgement) {
      // Shown as type and line only; the values never leave the server.
      const accepted = await confirmAction({
        title: `The ${row.language_name} text has values that look like credentials`,
        summary: done.findings.slice(0, 40).map((f) => [`Line ${f.line}`, f.type.replaceAll("_", " ").toLowerCase()]),
        note: `${done.findings.length} in all${done.findings.length > 40 ? ", the first 40 shown" : ""}. Remove them in the editor if they are real. If they are examples, you can finalize anyway.`,
        okLabel: "They are not real: finalize"});
      if (!accepted) return;
      done = await api(`/api/jobs/${jobId}/finalize`, {language: row.language, acknowledge_findings: true});
    }
    if (done.check && !done.check.passed) toast(`Finalized. Note: its structure differs from the source: ${done.check.errors.join(" ")}`, 9000);
    await poll();
  } catch (error) { toast(error.message, 6000); }
}
function recordUrl(row, url) {
  if (!/^https:\/\/\S+$/.test(url.trim())) { toast("Enter the https URL of the post."); return; }
  act("url", {platform: row.platform, url: url.trim()});
}
async function loadConfig() {
  if (!config) config = await api("/api/config");
  return config;
}
// Slides for a language: a plan and its deck. The dialog is the approval; it
// names the language, the length, the audience and the account (INV-6), and
// says what an approval replaces.
async function requestSlides(row) {
  let account = null;
  try { account = (await loadConfig()).slides_account; } catch (e) {}
  const hasPlan = Boolean(row.slides_file), hasDeck = Boolean(row.deck && row.deck.url);
  const summary = [
    ["Language", row.language_name],
    ["Length", `${row.slides_minutes} minutes, one slide per minute`],
    ["Audience", (state && state.overview && state.overview.slides_audience) || "software engineers"],
    ["From", `the latest post.${row.language}.md`],
    ["Where", account ? `a new file in the Google Drive of ${account}` : "a new file in your Google Drive"],
  ];
  if (hasPlan) summary.push(["Plan", "there is a plan already; what was changed in its speaker notes is replaced by the new plan"]);
  if (hasDeck) summary.push(["Deck", "there is a deck already; a new file is made and the earlier deck stays in Drive"]);
  const ok = await confirmAction({
    title: hasPlan ? `Make the ${row.language_name} slides and deck again?` : `Make ${row.language_name} slides and a Google Slides deck?`,
    summary: summary,
    okLabel: hasPlan ? "Approve and make again" : "Approve and make"});
  if (ok) act("slides", {languages: [row.language], minutes: 0, audience: "", approved: true});
}
// A deck from the plan as it is; the plan is not touched.
async function createDeck(row) {
  // Name the account, so a deck never lands in a Drive nobody expected.
  let account = null;
  try { account = (await loadConfig()).slides_account; } catch (e) {}
  const whereDesc = account ? `a new file in the Google Drive of ${account}` : "a new file in your Google Drive";
  const ok = await confirmAction({
    title: `Create the ${row.language_name} Google Slides deck?`,
    summary: [["From", row.slides_file || "speaker notes"], ["Where", whereDesc], ["Contains", "one slide per minute, script in the speaker notes"]],
    okLabel: "Approve and create"});
  if (ok) act("deck", {language: row.language, approved: true});
}

const MAX_FILE_BYTES = 80000;
async function loadFile(file) {
  if (!file) return;
  if (!/\.(md|markdown|txt)$/i.test(file.name)) { toast("Attach a Markdown (.md) or text file."); return; }
  if (file.size > MAX_FILE_BYTES) { toast("The file is larger than 80 KB, the limit for one article."); return; }
  if ($("source").value.trim() && !(await confirmAction({
      title: "Replace what is in the box?", summary: [["With", file.name]], okLabel: "Replace"}))) return;
  const reader = new FileReader();
  reader.onload = () => { $("source").value = reader.result; $("fileInfo").textContent = `${file.name} loaded (${reader.result.length.toLocaleString()} characters). You can edit it above.`; };
  reader.onerror = () => toast("The file could not be read.");
  reader.readAsText(file, "utf-8");
}

function setupUrlForLang(lang) {
  const file = lang === "ko" ? "kr-SETUP.md" : lang === "ja" ? "ja-SETUP.md" : "SETUP.md";
  return `https://github.com/agenthekim/trilingual-blog-agent/blob/main/docs/${file}`;
}

function renderSettingsRows() {
  if ($("setupGuideAnchor")) $("setupGuideAnchor").href = setupUrlForLang(currentUiLang);
  if (!config) return;
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
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
      const sub = p.how === "api" ? (t.howApi || "Posted through the API") : (t.howHtml || "Paste-ready HTML export");
      let badge;
      if (p.how !== "api") {
        badge = tag(t.statusReady || "✓ Ready", "ok");
      } else if (p.configured) {
        badge = tag(t.statusConnected || "✓ Connected", "ok");
      } else {
        const anchor = p.platform.toLowerCase().includes("qiita") ? "3-qiita-access-token" : "2-dev-community-api-key";
        badge = setupLink(t.statusNoCred || "No credential · Setup", anchor);
      }
      return row(p.language, p.platform, sub, badge);
    }),
    row(
      "Slides",
      "Google Slides",
      `${config.slides_account ? config.slides_account + " · " : ""}${config.diagrams ? (t.diagramsOn || "Diagrams drawn") : (t.diagramsOff || "Diagrams off")}`,
      config.google_slides
        ? tag(t.statusSlidesOk || "✓ Access granted", "ok")
        : setupLink(t.statusSlidesNo || "Not set up · Setup", "4-google-slides-oauth")
    )
  );
  $("settingsNote").textContent = t.settingsNoteText || "Credentials stay on the server; this page never sees them.";
}

// Settings: the fixed mapping, and what the server says is set up right now.
async function openSettings() {
  $("settingsRows").replaceChildren(el("span", {class: "muted small"}, "Checking…"));
  $("settingsNote").textContent = "";
  $("settingsModal").showModal();
  try {
    config = await api("/api/config");
    renderSettingsRows();
  } catch (error) {
    $("settingsRows").replaceChildren(el("span", {class: "muted small"}, "The settings could not be read: " + error.message));
  }
}
$("openSettings").addEventListener("click", openSettings);
$("closeSettings").addEventListener("click", () => $("settingsModal").close());
if ($("uiLanguageSelect")) {
  $("uiLanguageSelect").addEventListener("change", (e) => applyUiLanguage(e.target.value));
}
$("clearRun").addEventListener("click", () => {
  $("source").value = ""; $("file").value = "";
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
  $("fileInfo").textContent = t.dropHint || "or drop a file anywhere on this card";
  forget();
});
$("attach").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { loadFile(e.target.files[0]); e.target.value = ""; });
const zone = $("dropZone");
zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
zone.addEventListener("dragleave", () => zone.classList.remove("over"));
zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); loadFile(e.dataTransfer.files[0]); });

const PROMPT_HISTORY_KEY = "tba.prompt_history";
const ESSENTIAL_PROMPTS_BY_LANG = {
  en: [
    "Re-create the English slides and Google Slides deck",
    "Re-create the Korean slides and Google Slides deck",
    "Re-create the Japanese slides and Google Slides deck",
    "Re-translate the updated source article into the other two languages",
    "Shorten the English title",
    "Make the Korean intro less formal",
  ],
  ko: [
    "영어 발표 슬라이드와 Google Slides 덱을 다시 만들어줘",
    "한국어 발표 슬라이드와 Google Slides 덱을 다시 만들어줘",
    "일본어 발표 슬라이드와 Google Slides 덱을 다시 만들어줘",
    "수정된 원본 글을 바탕으로 다른 두 언어를 다시 번역해줘",
    "영어 블로그 제목을 더 간결하게 줄여줘",
    "한국어 도입부를 부드럽고 친근한 톤으로 다듬어줘",
  ],
  ja: [
    "英語のスライドとGoogle Slidesデッキを再作成して",
    "韓国語のスライドとGoogle Slidesデッキを再作成して",
    "日本語のスライドとGoogle Slidesデッキを再作成して",
    "更新された元記事から他の2言語を再翻訳して",
    "英語記事のタイトルをもっと簡潔にして",
    "韓国語記事の導入部をやわらかいトーンに整えて",
  ],
};
const DEFAULT_QUICK_INDICES = [4, 5, 0];

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
  const t = UI_TRANSLATIONS[lang] || UI_TRANSLATIONS.en;
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
      historyBox.replaceChildren(el("span", {class: "muted small"}, t.noHistoryText || "No custom prompts sent yet."));
    } else {
      historyBox.replaceChildren(...history.map((item) => {
        const shownText = localizePromptItem(item, lang);
        return el("div", {class: "prompt-modal-item"},
          el("button", {type: "button", class: "prompt-fill", onclick: () => fillPromptInput(shownText)}, shownText),
          el("span", {class: "badge", style: "font-size:11px"}, `×${item.count || 1}`),
          el("button", {
            type: "button",
            class: "prompt-del",
            title: "Remove from history",
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
  const t = UI_TRANSLATIONS[currentUiLang] || UI_TRANSLATIONS.en;
  const box = $("sourcesHistoryList");
  if (!box) return;
  const history = getSourceHistory();
  if (history.length === 0) {
    box.replaceChildren(el("span", {class: "muted small"}, t.noSourceHistoryText || "No source documents recorded yet."));
    return;
  }
  box.replaceChildren(...history.map((item) => {
    const isUrl = /^https?:\/\/\S+$/i.test(item.text);
    const firstLine = item.text.split("\n").find((l) => l.trim()) || item.text;
    const primaryTitle = item.title || (isUrl ? item.text : firstLine.replace(/^#+\s*/, "").slice(0, 80));
    const subLabel = isUrl ? item.text : `${item.text.length.toLocaleString()} chars · Markdown draft`;
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
      }, t.openRunBtn || "Open run") : null,
      el("button", {
        type: "button",
        class: "prompt-del",
        title: "Remove from history",
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
  if (state && state.lanes && state.lanes.main.busy) { toast("The agent is still working. Try again when it has finished."); return; }
  recordPromptUsage(text);
  $("message").value = "";
  $("logBox").open = true;
  act("message", {text});
}
$("send").addEventListener("click", submitMessage);
$("message").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitMessage(); } });
renderPromptChips();

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
  if (text.trim().length < 10) { toast("Paste a URL or a draft first."); $("source").focus(); return; }
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
