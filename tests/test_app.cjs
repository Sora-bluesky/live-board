"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../skills/live-board/scripts/static/app.js"), "utf8");

async function app(hash = "#list", overrides = {}, options = {}) {
  const elements = new Map();
  const documentListeners = {};
  let activeElement = null;
  function element(selector) {
    if (!elements.has(selector)) {
      const classes = new Set();
      const attributes = new Map();
      elements.set(selector, {
      value: "", innerHTML: "", textContent: "", dataset: {}, hidden: false,
      style: { values: new Map(), setProperty(name, value) { this.values.set(name, value); } },
      getBoundingClientRect() { return { top: 0, bottom: 96, height: 96 }; },
      id: /^#[\w-]+$/.test(selector) ? selector.slice(1) : "",
      open: false, isConnected: true, tagName: "DIV",
      listeners: {}, classList: {
        toggle(name, enabled) { if (enabled ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      setAttribute(name, value) { attributes.set(name, value); },
      getAttribute(name) { return attributes.get(name); },
      removeAttribute(name) { attributes.delete(name); }, hasAttribute(name) { return attributes.has(name); },
      querySelector() { return element("nested-control"); },
      querySelectorAll(query) {
        if (selector !== "#content" || query !== "a.card-link[data-issue], a.issue-link[data-issue], a.issue-key[data-issue]") return [];
        return [...this.innerHTML.matchAll(/<a\b([^>]*)>/g)].flatMap(([, attributes]) => {
          const id = attributes.match(/\bdata-issue="([^"]*)"/)?.[1];
          const classes = attributes.match(/\bclass="([^"]*)"/)?.[1].split(" ") || [];
          if (id == null || !classes.some(name => ["card-link", "issue-link", "issue-key"].includes(name))) return [];
          const issue = id.replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[name]);
          return [{ dataset: { issue }, getClientRects: () => options.hiddenIssues?.includes(issue) ? [] : [{}], closest: () => null }];
        });
      },
      closest() { return null; }, contains(target) { return target === this; },
      matches(query) { return query.split(",").some(tag => tag.trim().toUpperCase() === this.tagName); },
      focus() { activeElement = this; },
      blur() { documentListeners.blur?.({ target: this }); if (activeElement === this) activeElement = null; },
      select() { this.setSelectionRange(0, this.value.length); },
      setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
      showModal() { this.open = true; }, close() { this.open = false; },
      getClientRects() { return this.hidden ? [] : [{}]; },
    });
    }
    return elements.get(selector);
  }
  const snapshot = {
    last_good: {
      loaded_at: "2026-10-31T00:00:00Z",
      meta: { product: "Fixture", updated: "2026-10-31", current_phase: "P1" },
      rows: {
        phases: [{ id: "P1", title: "Phase", order: 1 }],
        versions: [{ id: "V1", title: "Version", order: 1 }],
        tasks: [{ id: "T1", title: "First task", status: "todo", phase: "P1", version: "V1" }],
      },
    }, error: null, checked_at: new Date().toISOString(),
  };
  const projects = options.projects || [{ id: "first", name: "First project" }, { id: "second", name: "Second project" }];
  const summaries = options.summaries || [
    { id: "first", name: "First project", adapter: "winsmux-backlog", counts: { todo: 1, doing: 0, blocked: 0, done: 0 }, total: 1, owner_wait: 0, current_phase: "P1", source_mtime: 1000, checked_at: new Date().toISOString(), loaded_at: "2026-10-31T00:00:00Z", error: null, warnings: [] },
    { id: "second", name: "Second project", adapter: "wbs-cards", counts: { todo: 0, doing: 0, blocked: 1, done: 7 }, total: 8, owner_wait: 1, current_phase: "", source_mtime: 2000, checked_at: new Date().toISOString(), loaded_at: "2026-10-31T00:00:00Z", error: null, warnings: [] },
  ];
  const snapshots = { first: structuredClone(snapshot), second: structuredClone(snapshot) };
  snapshots.second.last_good.meta.product = "Second project data";
  for (const project of projects) snapshots[project.id] ||= structuredClone(snapshot);
  Object.assign(snapshots, overrides);
  const listeners = {};
  const events = {};
  const intervals = [];
  const requests = [];
  const transport = { failSummary: false, failProjects: options.failProjects || false, failSnapshots: new Set(),
    health: options.health || (() => ({ ok: true, json: async () => ({ fingerprint: "old" }) })) };
  const location = { hash, reloads: 0, reload() { this.reloads++; } };
  const storage = options.storage || new Map();
  const session = options.session || new Map();
  const sessionStorage = {
    getItem(key) { if (options.sessionThrows) throw new Error("Storage denied"); return session.get(key) ?? null; },
    setItem(key, value) { if (options.sessionThrows) throw new Error("Storage denied"); session.set(key, value); },
  };
  const localStorage = {
    getItem(key) { if (options.storageThrows || options.readThrows) throw new Error("Storage denied"); return storage.get(key) ?? null; },
    setItem(key, value) { if (options.storageThrows || options.writeThrows) throw new Error("Storage denied"); storage.set(key, value); },
  };
  const context = vm.createContext({
    URLSearchParams, Intl, Date: options.Date || Date, console, location, localStorage, sessionStorage, navigator: options.navigator || {},
    ResizeObserver: options.ResizeObserver,
    getComputedStyle: () => ({ visibility: "visible" }),
    history: {
      pushState(_state, _title, hash) { location.hash = hash; },
      replaceState(_state, _title, hash) { location.hash = hash; },
    },
    document: {
      querySelector: element, querySelectorAll(selector) { return options.querySelectorAll?.(selector) || []; },
      addEventListener(name, callback) { documentListeners[name] = callback; },
      get activeElement() { return activeElement; }, body: element("body"), documentElement: element("html"),
    },
    window: {
      addEventListener(name, callback) { listeners[name] = callback; },
      matchMedia(query) { return { matches: options.mobile && query === "(max-width: 599px)", addEventListener() {} }; },
    },
    CSS: { escape: value => value }, setInterval(callback, delay) { intervals.push({ callback, delay }); }, EventSource: class {
      constructor() { events.open = () => this.onopen(); }
      addEventListener(name, callback) { events[name] = callback; }
    },
    async fetch(url) {
      requests.push(url);
      if (url === "/healthz") return transport.health();
      const value = url === "/api/projects" ? projects : url === "/api/summary" ? summaries : snapshots[decodeURIComponent(url.split("/").pop())];
      const failed = (url === "/api/summary" && transport.failSummary) || (url === "/api/projects" && transport.failProjects) || transport.failSnapshots.has(url);
      return { ok: Boolean(value) && !failed, status: failed ? 503 : value ? 200 : 404, json: async () => structuredClone(value) };
    },
  });
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  function click(matches) {
    const target = { closest: selector => matches[selector] || null };
    documentListeners.click({ target, button: 0, preventDefault() {} });
  }
  function key(key, extra = {}) {
    const event = { key, target: { closest() { return null; } }, preventDefault() { this.prevented = true; }, ...extra };
    documentListeners.keydown(event);
    return event;
  }
  function replaceElement(selector) {
    element(selector).isConnected = false;
    elements.delete(selector);
    return element(selector);
  }
  return { run: code => vm.runInContext(code, context), element, replaceElement, location, listeners, documentListeners, snapshots, summaries, events, intervals, requests, transport, storage, session, click, key };
}

async function codeReload() {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const healthy = fingerprint => () => ({ ok: true, json: async () => ({ fingerprint }) });
  const triggers = [ui => ui.events.open(), ui => ui.listeners.focus(), ui => ui.documentListeners.visibilitychange()];
  let finishBaseline;
  const starting = await app("#list", {}, { health: () => new Promise(resolve => { finishBaseline = resolve; }) });
  starting.transport.health = healthy("new");
  starting.events.open();
  starting.listeners.focus();
  await settle();
  assert.equal(starting.location.reloads, 0, "Checks must wait for the startup baseline");
  finishBaseline({ ok: true, json: async () => ({ fingerprint: "old" }) });
  await settle();
  assert.equal(starting.location.reloads, 1, "Overlapping checks must compare against the startup read only");
  for (const trigger of triggers) {
    const ui = await app("#board?project=first&issue=T1");
    trigger(ui);
    await settle();
    assert.equal(ui.location.reloads, 0, "An unchanged fingerprint must not reload");
    ui.transport.health = healthy("new");
    trigger(ui);
    await settle();
    assert.equal(ui.location.reloads, 1, "Reconnect, focus and visibility must detect new code");
    assert.equal(ui.location.hash, "#board?project=first&issue=T1");
    trigger(ui);
    await settle();
    assert.equal(ui.location.reloads, 1, "Concurrent or repeated checks reload only once");
  }
  const hidden = await app();
  hidden.transport.health = healthy("new");
  hidden.run("document.hidden = true");
  hidden.documentListeners.visibilitychange();
  await settle();
  assert.equal(hidden.location.reloads, 0, "Hiding a tab must not trigger a code check");

  const failures = [
    () => { throw new Error("Offline"); },
    () => Promise.reject(new Error("Offline")),
    () => ({ ok: false, json: async () => ({ fingerprint: "new" }) }),
    () => ({ ok: true, json: async () => { throw new SyntaxError("Not JSON"); } }),
    ...[null, {}, { fingerprint: null }, { fingerprint: 123 }, { fingerprint: "" }]
      .map(data => () => ({ ok: true, json: async () => data })),
  ];
  for (const health of failures) {
    const ui = await app();
    ui.transport.health = health;
    ui.events.open();
    await settle();
    assert.equal(ui.location.reloads, 0, "A failed or invalid check must not reload");
    assert.equal(ui.element("#error-banner").hidden, true, "Health failures stay silent");
    ui.transport.health = healthy("new");
    ui.listeners.focus();
    await settle();
    assert.equal(ui.location.reloads, 1, "Failures must preserve the original baseline");

    const noBaseline = await app("#list", {}, { health });
    noBaseline.transport.health = healthy("new");
    for (const trigger of triggers) trigger(noBaseline);
    await settle();
    assert.equal(noBaseline.location.reloads, 0, "Without a startup baseline there must be no reload");
    assert.equal(noBaseline.element("#error-banner").hidden, true);
  }

  for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
    const ui = await app();
    const input = ui.element("reload-editor");
    input.tagName = tag;
    input.focus();
    ui.transport.health = healthy("new");
    ui.events.open();
    await settle();
    assert.equal(ui.location.reloads, 0, `${tag} focus must defer reloading`);
    input.blur();
    await settle();
    assert.equal(ui.location.reloads, 1, "Blur retries after focus leaves the editor");
  }
  const composing = await app();
  composing.documentListeners.compositionstart();
  composing.transport.health = healthy("new");
  composing.events.open();
  await settle();
  assert.equal(composing.location.reloads, 0, "IME composition defers reloading even without an active input");
  composing.documentListeners.compositionend();
  composing.listeners.focus();
  await settle();
  assert.equal(composing.location.reloads, 1);

  const pending = await app();
  const editor = pending.element("reload-editor");
  editor.tagName = "INPUT";
  editor.focus();
  pending.transport.health = healthy("new");
  pending.events.open();
  await settle();
  pending.transport.health = failures[0];
  editor.blur();
  await settle();
  assert.equal(pending.location.reloads, 0, "A failed blur check cannot apply a stale pending fingerprint");
  pending.transport.health = healthy("new");
  pending.listeners.focus();
  await settle();
  assert.equal(pending.location.reloads, 1);

  const guarded = await app("#list", {}, { session: pending.session });
  guarded.transport.health = healthy("new");
  for (const trigger of triggers) trigger(guarded);
  await settle();
  assert.equal(guarded.location.reloads, 0, "A stale page must not reload again for the same fingerprint in this tab");
  guarded.transport.health = healthy("newer");
  guarded.events.open();
  await settle();
  assert.equal(guarded.location.reloads, 1, "A different fingerprint can still reload");
  const current = await app("#list", {}, { health: healthy("newer"), session: guarded.session });
  current.events.open();
  await settle();
  assert.equal(current.location.reloads, 0, "A baseline read after restart already matches");
  const denied = await app("#list", {}, { sessionThrows: true });
  denied.transport.health = healthy("new");
  denied.events.open();
  await settle();
  assert.equal(denied.location.reloads, 1, "Unavailable storage must not throw");

  assert.equal(script.split('history[replace ? "replaceState" : "pushState"](null, "", hash)').length - 1, 1);
  assert.equal(script.split('history.replaceState(null, "", hashFor())').length - 1, 1);
  assert.equal(script.split("location.hash").length - 1, 4);
  console.log("PASS code reload: baseline, reconnect/focus/visibility, silent failures, typing/IME deferral, blur and per-tab loop guard");
}

async function navigation() {
  const ui = await app("", {
    first: { last_good: { loaded_at: "2026-10-31T00:00:00Z", meta: {}, rows: { phases: [], versions: [], tasks: [
      { id: "W1", title: "Winsmux blocked", status: "blocked" },
      { id: "D1", title: "Doing first", status: "doing" },
    ] } }, error: null },
    second: { last_good: { loaded_at: "2026-10-31T00:00:00Z", meta: {}, rows: { phases: [], versions: [], tasks: [
      { id: "S1", title: "Owner decision", status: "blocked", raw_status: "p-owner", ball: true },
      { id: "B1", title: "Ball held", status: "todo", ball: true },
      { id: "N1", title: "Ordinary wait", status: "blocked", raw_status: "p-wait" },
      { id: "D1", title: "Doing second", status: "doing" },
    ] } }, error: null },
  });
  assert.equal(ui.location.hash, "#for-you");
  assert.equal(ui.element("#project-tabs").hidden, true);
  assert.equal(ui.element("#filters").hidden, true);
  const html = () => ui.element("#content").innerHTML;
  assert.match(html(), /Winsmux blocked/);
  assert.match(html(), /Owner decision/);
  assert.match(html(), /Ball held/);
  assert.doesNotMatch(html(), /Ordinary wait/);
  assert.equal((html().match(/data-cross-issue=/g) || []).length, 3, "Overlapping p-owner and ball count once");
  ui.run('navigate({ homeTab: "doing" })');
  assert.match(html(), /Doing first/);
  assert.match(html(), /Doing second/);
  assert.equal((html().match(/data-cross-issue=/g) || []).length, 2);
  ui.click({ "a[data-cross-issue]": { dataset: { project: "second", crossIssue: "D1" } } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.location.hash, "#board?project=second&issue=D1");
  assert.match(ui.element("#issue-panel").innerHTML, /Doing second/);
  assert.equal(ui.run('state.recents[0].issue'), "D1");
  ui.key("Escape");
  assert.equal(ui.run("state.issue"), "");

  for (const options of [{ storageThrows: true }, { writeThrows: true }, {}]) {
    const viewer = await app("#board?project=second&issue=T1", {}, options);
    viewer.click({ "[data-star-project]": { dataset: { starProject: "second" } } });
    assert.equal(viewer.run('state.starred.includes("second")'), true);
    assert.match(viewer.element("#nav-starred").innerHTML, /Second project/);
    if (!options.storageThrows && !options.writeThrows) {
      const reload = await app("#for-you?tab=starred", {}, { storage: viewer.storage });
      assert.equal(reload.run('state.starred.includes("second")'), true);
      assert.match(reload.element("#content").innerHTML, /starred-project-card/);
    }
    viewer.run('navigate({ view: "board", projectId: "first", issue: "T1" })');
    await new Promise(resolve => setImmediate(resolve));
    assert.match(viewer.element("#nav-recent").innerHTML, /First project/);
    assert.match(viewer.element("#nav-recent").innerHTML, /T1/);
    assert.equal(viewer.run('state.recents[0].projectId'), "first");
    const recentCount = viewer.run('state.recents.length');
    viewer.run('navigate({ view: "list" }); render()');
    assert.equal(viewer.run('state.recents.length'), recentCount, "Rendering and switching project tabs must not add visits");
    viewer.run('navigate(parseHash("#for-you?tab=starred"))');
    assert.match(viewer.element("#content").innerHTML, /Second project/);
    assert.match(viewer.element("#content").innerHTML, /starred-project-card/);
    viewer.run('navigate({ homeTab: "recent" })');
    assert.match(viewer.element("#content").innerHTML, /T1/);
    viewer.click({ "[data-star-project]": { dataset: { starProject: "second" } } });
    assert.equal(viewer.run('state.starred.includes("second")'), false);
    if (!options.storageThrows && !options.writeThrows) {
      const reload = await app("#for-you?tab=recent", {}, { storage: viewer.storage });
      assert.match(reload.element("#content").innerHTML, /T1/);
    }
  }
  assert.equal(ui.key("/").prevented, true);
  assert.equal(ui.run('document.activeElement === $("#global-search")'), true);
  ui.element("#global-search").listeners.input({ target: { value: "Doing second" } });
  assert.match(ui.element("#search-results").innerHTML, /課題/);
  assert.match(ui.element("#search-results").innerHTML, /Doing second/);
  ui.element("#global-search").listeners.keydown({ key: "Enter", preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.location.hash, "#board?project=second&issue=D1");
  ui.key("Escape");
  ui.element("#global-search").listeners.input({ target: { value: "second" } });
  assert.match(ui.element("#search-results").innerHTML, /プロジェクト/);
  const editing = { closest() { return {}; } };
  assert.equal(ui.key("/", { target: editing }).prevented, undefined);
  assert.equal(ui.key("[", { ctrlKey: true }).prevented, true);
  assert.equal(ui.run("state.sidebarCollapsed"), true);
  ui.key("[", { ctrlKey: true });
  assert.equal(ui.run("state.sidebarCollapsed"), false);
  const mobile = await app("#for-you", {}, { mobile: true });
  mobile.element("#menu-toggle").listeners.click();
  assert.equal(mobile.run("state.sidebarOpen"), true);
  mobile.key("Escape");
  assert.equal(mobile.run("state.sidebarOpen"), false);
  const malformed = await app("#for-you", {}, { storage: new Map([["live-board.viewer", "{invalid"]]) });
  assert.equal(malformed.run("state.starred.length + state.recents.length"), 0);

  ui.run('navigate(parseHash("#for-you"))');
  await new Promise(resolve => setImmediate(resolve));
  ui.snapshots.second.last_good.rows.tasks.push({ id: "LIVE", title: "Live decision", raw_status: "p-owner", status: "blocked" });
  ui.snapshots.second.last_good.loaded_at = "2026-10-31T00:01:00Z";
  await ui.events.project({ data: '"second"' });
  assert.match(html(), /Live decision/);
  ui.element("#global-search").listeners.input({ target: { value: "Live decision" } });
  assert.match(ui.element("#search-results").innerHTML, /Live decision/);
  ui.transport.failSnapshots.add("/api/projects/second");
  await ui.events.project({ data: '"second"' });
  assert.match(html(), /Live decision/, "Failed fetches retain the last good cross-project issues");
  assert.match(ui.element("#error-banner").textContent, /HTTP 503/);
  assert.match(ui.element("#search-results").innerHTML, /一部の課題を取得できていません/);
  ui.transport.failSnapshots.clear();
  ui.snapshots.second.last_good.rows.tasks.pop();
  ui.snapshots.second.last_good.loaded_at = "2026-10-31T00:02:00Z";
  await ui.events.project({ data: '"second"' });
  assert.doesNotMatch(html(), /Live decision/);
  assert.doesNotMatch(ui.element("#search-results").innerHTML, /Live decision/);
  assert.equal(ui.element("#error-banner").hidden, true);
  console.log("PASS navigation: For you, cross-project waiting/doing, details, safe viewer storage, recent history, global search and keyboard shortcuts");
}

async function searchClicks() {
  for (const mobile of [false, true]) {
    for (const issue of ["T1", ""]) {
      const ui = await app("#board?project=first", {}, { mobile });
      ui.element("#global-search").listeners.input({ target: { value: issue ? "First task" : "Second project" } });
      const index = issue ? 1 : 0;
      const option = ui.element(`#search-option-${index}`);
      option.focus();
      const results = ui.element("#search-results");
      let html = results.innerHTML;
      let replacing = false;
      Object.defineProperty(results, "innerHTML", {
        get() { return html; },
        set(value) {
          assert.equal(replacing, false, "Removing a focused search result must not re-enter DOM replacement");
          replacing = true;
          ui.element("#global-search-form").listeners.focusout({ relatedTarget: null });
          html = value;
          replacing = false;
        },
      });
      ui.click({ "#global-search-form": {}, "[data-search-index]": { dataset: { searchIndex: String(index) } } });
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(ui.location.hash, `#board?project=second${issue ? `&issue=${issue}` : ""}`);
      assert.equal(ui.element("#issue-panel").hidden, !issue);
      if (issue) assert.match(ui.element("#issue-panel").innerHTML, /First task/);
      assert.equal(results.hidden, true);
      assert.equal(ui.element("#global-search").getAttribute("aria-expanded"), "false");
    }
  }
  for (const close of [
    ui => ui.element("#global-search-form").listeners.focusout({ relatedTarget: null }),
    ui => ui.key("Escape"),
    ui => ui.key("Tab"),
    ui => ui.click({}),
  ]) {
    const ui = await app();
    ui.element("#global-search").listeners.input({ target: { value: "First task" } });
    const results = ui.element("#search-results");
    const html = results.innerHTML;
    Object.defineProperty(results, "innerHTML", { get() { return html; }, set() { assert.fail("Closing search must retain result nodes until focus has left"); } });
    close(ui);
    assert.equal(results.hidden, true);
    assert.equal(ui.element("#global-search").getAttribute("aria-activedescendant"), undefined);
  }
  console.log("PASS search-click: desktop/mobile issue and project clicks open the correct route; blur, Escape, Tab and outside clicks close without replacing results");
}

async function crossProjectErrors() {
  const anchors = html => html.match(/<a\b[^>]*>[\s\S]*?<\/a>/g) || [];
  const badge = /class="project-error project-read-state"[^>]*>読み込み失敗・最終正常/;
  for (const failure of ["snapshot", "summary", "transport"]) {
    const ui = await app("#board?project=first");
    ui.run('state.connection = "接続中"; toggleStar("second"); recordRecent("second"); recordRecent("second", "T1")');
    const stale = ui.snapshots.second;
    stale.last_good.rows.tasks[0].status = "doing";
    stale.last_good.rows.tasks[0].raw_status = "p-owner";
    const error = { message: "broken <source&>", at: "2026-10-31T01:00:00Z" };
    if (failure === "snapshot") stale.error = error;
    if (failure === "summary") ui.summaries[1].error = error;
    if (failure === "transport") ui.transport.failSnapshots.add("/api/projects/second");
    // Prime the cached task data before testing the failed transport.
    if (failure === "transport") ui.run('state.projectSnapshots.get("second").last_good.rows.tasks[0].status = "doing"; state.projectSnapshots.get("second").last_good.rows.tasks[0].raw_status = "p-owner"');
    await ui.events.project({ data: '"second"' });
    ui.element("#global-search").listeners.input({ target: { value: "First task" } });
    const search = anchors(ui.element("#search-results").innerHTML);
    assert.equal(search.length, 2);
    assert.match(search.find(html => html.includes("project=second")), badge, `${failure}: mark the stale issue itself`);
    assert.doesNotMatch(search.find(html => html.includes("project=first")), /project-read-state/, "Healthy projects must remain unmarked");
    assert.match(ui.element("#search-results").innerHTML, /search-notice/);
    assert.equal(ui.element("#freshness").className, "freshness failed");
    assert.equal(ui.element("#error-banner").hidden, false);
    assert.match(ui.element("#error-banner").textContent, /Second project/);
    assert.ok(search[1].includes(ui.run('formatTime("2026-10-31T00:00:00Z")')));
    if (failure !== "transport") {
      assert.match(search[1], /broken &lt;source&amp;&gt;/);
      assert.doesNotMatch(search[1], /broken <source&>/);
    }
    ui.element("#global-search").listeners.input({ target: { value: "Second project" } });
    for (const result of anchors(ui.element("#search-results").innerHTML)) assert.match(result, badge, "Project search results also identify source errors");
    for (const tab of ["waiting", "doing", "starred", "recent"]) {
      ui.run(`navigate(parseHash("#for-you?tab=${tab}"))`);
      await new Promise(resolve => setImmediate(resolve));
      const html = ui.element("#content").innerHTML;
      const issue = anchors(html).find(item => item.includes('class="cross-issue-row"') && item.includes("project=second"));
      assert.ok(issue, `${failure}: preserve last-good issues in ${tab}`);
      assert.match(issue, badge, `${failure}: mark the stale row in ${tab}`);
      for (const selector of ["#nav-recent", "#nav-starred", "#nav-projects"]) {
        for (const link of anchors(ui.element(selector).innerHTML).filter(item => item.includes("project=second"))) assert.match(link, badge, `${failure}: ${selector}`);
      }
      assert.match(html.match(/<article class="starred-project-card">[\s\S]*?<\/article>/)[0], badge);
    }
    stale.error = null;
    ui.summaries[1].error = null;
    ui.transport.failSnapshots.clear();
    await ui.events.project({ data: '"second"' });
    for (const selector of ["#content", "#search-results", "#nav-recent", "#nav-starred", "#nav-projects"]) assert.doesNotMatch(ui.element(selector).innerHTML, /project-read-state|search-notice/, `${failure}: ${selector} recovers`);
    assert.equal(ui.element("#freshness").className, "freshness healthy");
    assert.equal(ui.element("#error-banner").hidden, true);
  }
  const unreadable = await app("#board?project=first", { second: { last_good: null, error: { message: "Missing source" }, checked_at: new Date().toISOString() } });
  Object.assign(unreadable.summaries[1], { counts: null, total: null, loaded_at: null, error: { message: "Missing source" } });
  await unreadable.events.project({ data: '"second"' });
  unreadable.element("#global-search").listeners.input({ target: { value: "Second project" } });
  assert.match(unreadable.element("#search-results").innerHTML, /読み込み失敗・未取得/);
  console.log("PASS cross-project-errors: source/summary/HTTP failures mark search, all For you tabs/cards and sidebar lists; healthy projects, escaping, no-data and recovery verified");
}

async function projectDates() {
  const originalTZ = process.env.TZ;
  try {
    for (const timezone of ["Asia/Tokyo", "America/Los_Angeles", "UTC"]) {
      process.env.TZ = timezone;
      const ui = await app("#board?project=first");
      assert.equal(ui.run('formatProjectUpdated("2026-10-31")'), "2026/10/31", timezone);
      assert.match(ui.element("#project-meta").textContent, /正本の更新日 2026\/10\/31(?: ·|$)/);
      for (const value of ["2026-10-31T00:00:00Z", "2026-10-31T23:30:00-07:00", 1793404800000]) {
        const expected = new Date(value).toLocaleString("ja-JP", {
          year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
        });
        assert.equal(ui.run(`formatProjectUpdated(${JSON.stringify(value)})`), expected, `${timezone}: ${value}`);
      }
      for (const value of [null, "invalid", "2026-13-31"]) {
        assert.equal(ui.run(`formatProjectUpdated(${JSON.stringify(value)})`), "—");
      }
    }
  } finally {
    if (originalTZ === undefined) delete process.env.TZ;
    else process.env.TZ = originalTZ;
  }
  console.log("PASS project-dates: date-only metadata keeps its calendar date and timestamps retain local time in three timezones");
}

async function directoryTimes() {
  const ui = await app("#projects");
  const first = ui.summaries[0], second = ui.summaries[1];
  first.source_mtime = 2000;
  const error = { message: "Broken source", at: "2026-10-31T01:00:00Z" };
  Object.assign(second, { source_mtime: 3000, last_good_mtime: 1000, error });
  Object.assign(ui.snapshots.second, { source_mtime: 3000, error });
  ui.snapshots.second.last_good.source_mtime = 1000;
  const html = () => ui.element("#content").innerHTML;
  const row = () => html().match(/<tr data-project="second">[\s\S]*?<\/tr>/)[0];
  const displayed = () => row().match(/<time datetime="([^"]+)"/)?.[1];
  const iso = seconds => new Date(seconds * 1000).toISOString();
  const refresh = () => ui.events.project({ data: '"second"' });
  await refresh();
  assert.equal(displayed(), iso(1000), "The updated column must date the preserved counts, not the broken source");
  assert.match(row(), /7 \/ 8 済み/);
  assert.match(row(), /<span class="project-error" title="[^"]*">読み込み失敗<\/span>/);
  assert.ok(html().indexOf('data-project="first"') < html().indexOf('data-project="second"'), "Descending order must use the displayed last-good time");
  assert.equal(ui.element("#file-time").title, ui.run("formatTime(2000, true)"), "The cross-project header also dates displayed data");
  ui.run('navigate({ direction: "asc" })');
  assert.ok(html().indexOf('data-project="second"') < html().indexOf('data-project="first"'));
  ui.run('navigate({ direction: "desc" })');
  for (const mtime of [5000, null]) {
    second.source_mtime = mtime;
    ui.snapshots.second.source_mtime = mtime;
    await refresh();
    assert.equal(displayed(), iso(1000), "Repeated failures and removed sources keep the last-good update");
    assert.ok(html().indexOf('data-project="first"') < html().indexOf('data-project="second"'));
  }
  const good = structuredClone(ui.snapshots.second.last_good);
  Object.assign(second, { source_mtime: 5000, last_good_mtime: null, counts: null, total: null, loaded_at: null });
  ui.snapshots.second.last_good = null;
  await refresh();
  assert.equal(displayed(), undefined, "Never-loaded data must not inherit the failed file's mtime");
  assert.match(row(), /<td class="project-updated">—<\/td><td class="project-state"><span class="project-error"/);
  assert.ok(html().indexOf('data-project="first"') < html().indexOf('data-project="second"'));
  Object.assign(second, { counts: { todo: 0, doing: 0, blocked: 1, done: 7 }, total: 8, loaded_at: good.loaded_at, last_good_mtime: 0 });
  ui.snapshots.second.last_good = { ...good, source_mtime: 0 };
  await refresh();
  assert.equal(displayed(), iso(0), "An epoch mtime is a valid last-good value");
  Object.assign(second, { error: null, source_mtime: 6000, last_good_mtime: 6000 });
  Object.assign(ui.snapshots.second, { error: null, source_mtime: 6000, last_good: { ...good, source_mtime: 6000 } });
  await refresh();
  assert.equal(displayed(), iso(6000));
  assert.doesNotMatch(row(), /読み込み失敗/);
  assert.ok(html().indexOf('data-project="second"') < html().indexOf('data-project="first"'), "Recovery restores sorting by the new good source");
  ui.run('navigate(parseHash("#board?project=second"))');
  await new Promise(resolve => setImmediate(resolve));
  Object.assign(second, { error, source_mtime: 9000 });
  Object.assign(ui.snapshots.second, { error, source_mtime: 9000 });
  await refresh();
  assert.equal(ui.element("#file-time").title, ui.run("formatTime(6000, true)"), "The selected-project header must also use last-good.source_mtime");
  ui.snapshots.second.last_good = null;
  await refresh();
  assert.equal(ui.element("#file-time").title, "—", "No last-good snapshot means no displayed-data mtime");
  console.log("PASS directory-times: displayed/sorted last-good updates, both sort directions, repeated/missing sources, no-data, epoch, recovery and global/selected headers verified");
}

async function filters() {
  const ui = await app();
  for (const [key, value] of [["phase", "P1"], ["version", "V1"], ["status", "todo"]]) {
    ui.run('Object.assign(state, { phase: "", version: "", status: "" })');
    ui.run(`navigate(${JSON.stringify({ [key]: value })})`);
    assert.equal(ui.run("filteredTasks().length"), 1);
    ui.run('state.snapshot.last_good.rows = { phases: [], versions: [], tasks: [] }; state.snapshot.last_good.loaded_at = "2026-10-31T00:00:01Z"; render()');
    const html = ui.element(`#filter-${key}`).innerHTML;
    assert.match(html, new RegExp(`<option value="${value}" selected>[^<]*（存在しません）</option>`));
    assert.equal(new URLSearchParams(ui.location.hash.split("?")[1]).get(key), value);
    assert.equal(ui.run(`state.${key}`), value);
    assert.equal(ui.run("filteredTasks().length"), 0);
    await ui.run("refresh()");
    assert.doesNotMatch(ui.element(`#filter-${key}`).innerHTML, /存在しません/);
  }
  ui.run('navigate({ version: "__none__" })');
  assert.match(ui.element("#filter-version").innerHTML, /value="__none__" selected>版なし/);
  assert.doesNotMatch(ui.element("#filter-version").innerHTML, /存在しません/);
  ui.run('navigate({ phase: \'<missing&"phase>\' }); render()');
  assert.match(ui.element("#filter-phase").innerHTML, /&lt;missing&amp;&quot;phase&gt;/);
  assert.doesNotMatch(ui.element("#filter-phase").innerHTML, /<missing/);
  const unknown = await app("#list?status=retired");
  assert.match(unknown.element("#filter-status").innerHTML, /value="retired" selected>retired（存在しません）/);
  assert.equal(unknown.run("filteredTasks().length"), 0);
  console.log("PASS removed-filter: phase/version/status stay selected, hash agrees, zero results explained, recovery and escaping verified");
}

async function ownerFilters() {
  const snapshot = { last_good: { loaded_at: "2026-10-31T00:00:00Z", meta: {}, rows: { phases: [], versions: [], tasks: [
    { id: "S1", title: "判断待ち", status: "blocked", raw_status: "p-owner", ball: false, ready: "no" },
    { id: "B1", title: "ボールあり", status: "todo", raw_status: "p-hold", ball: true, ready: "yes" },
    { id: "SB", title: "両方", status: "doing", raw_status: "p-owner", ball: true, ready: "yes" },
    { id: "W1", title: "普通の待ち", status: "blocked", raw_status: "p-wait", ball: false },
    { id: "N1", title: "文字列のボール", status: "todo", ball: "true" },
    { id: "N2", title: "正規化された状態だけ", status: "p-owner" },
  ] } }, error: null };
  for (const rawStatus of ["p-owner", "p-sora"]) {
    const variant = JSON.parse(JSON.stringify(snapshot).replaceAll("p-owner", rawStatus));
    for (const parameter of ["owner", "sora"]) {
      for (const view of ["board", "list"]) {
        const ui = await app(`#${view}?project=first&${parameter}=1`, { first: variant });
        assert.equal(ui.run("state.owner"), true);
        assert.equal(ui.run('Object.hasOwn(state, "sora")'), false);
        assert.equal(ui.run('filteredTasks().map(task => task.id).join(",")'), "S1,B1,SB");
        assert.equal(ui.run('homeIssues().map(({ task }) => task.id).join(",")'), "S1,B1,SB,W1,N2", "Cross-project winsmux waiting retains blocked tasks with either status spelling");
        assert.equal(ui.run('rows().tasks.filter(task => isOwnerWaiting({ project: { id: "second" }, task })).map(task => task.id).join(",")'), "S1,B1,SB,N2", "Cross-project WBS waiting accepts either status spelling and excludes ordinary blocked tasks");
        ui.element("#filters").listeners.change({ target: { dataset: { filter: "owner" }, checked: true } });
        assert.equal(ui.run('filteredTasks().map(task => task.id).join(",")'), "S1,B1,SB");
        assert.equal(ui.run('rows().tasks.filter(task => ownerMark(task)).map(task => task.id).join(",")'), "S1,B1,SB", "The marker and quick filter use the same exact predicate");
        assert.equal(ui.element("#filter-owner").checked, true);
        assert.equal(ui.element("#clear-filters").disabled, false);
        assert.equal(ui.element("#filter-note").hidden, false);
        assert.equal(new URLSearchParams(ui.location.hash.split("?")[1]).get("owner"), "1");
        assert.equal(new URLSearchParams(ui.location.hash.split("?")[1]).has("sora"), false);
        const reload = await app(ui.location.hash, { first: variant });
        assert.equal(reload.run("state.owner"), true);
        assert.equal(reload.run('filteredTasks().map(task => task.id).join(",")'), "S1,B1,SB");
        ui.run('navigate({ ready: true, q: "ボール", status: "todo" })');
        assert.equal(ui.run('filteredTasks().map(task => task.id).join(",")'), "B1", "Quick filters intersect with existing filters");
        assert.equal(ui.run('parseHash(hashFor()).ready && parseHash(hashFor()).owner'), true);
        ui.element("#clear-filters").listeners.click();
        assert.equal(ui.run("state.owner || hasFilters()"), false);
        assert.equal(ui.element("#filter-owner").checked, false);
        assert.equal(ui.element("#clear-filters").disabled, true);
        assert.equal(ui.location.hash, `#${view}?project=first`);
        assert.equal(ui.run("filteredTasks().length"), 6);
        for (const query of ["owner=true", "sora=true", "owner=0&sora=0"]) {
          assert.equal(ui.run(`parseHash("#board?${query}").owner`), false);
        }
        assert.equal(ui.run('parseHash("#board?owner=0&sora=1").owner'), true);
        assert.equal(ui.run('parseHash("#board?owner=1&sora=0").owner'), true);
        ui.element("#filters").listeners.change({ target: { dataset: { filter: "owner" }, checked: false } });
        assert.equal(ui.run("state.owner"), false);
      }
    }
  }
  console.log("PASS owner filter: canonical/legacy status and URL parity, shared marks/counting, board/list intersection, route reload and clear");
}

async function issueNavigation() {
  const snapshot = { last_good: { loaded_at: "2026-10-31T00:00:00Z", meta: {}, rows: {
    phases: [{ id: "P2", title: "後半", order: 2 }, { id: "P1", title: "前半", order: 1 }],
    versions: [{ id: "V2", title: "新版", order: 2 }, { id: "V1", title: "初版", order: 1 }],
    tasks: [
      { id: "D2", title: "Keep C", status: "doing", phase: "P2", version: "V2" },
      { id: "T10", title: "Keep B", status: "todo", phase: "P2", version: "V1" },
      { id: "C1", title: "Keep E", status: "done", phase: "P1", version: "V1" },
      { id: "T2", title: "Keep A", status: "todo", phase: "P1", version: "", deps: "HIDDEN, D2" },
      { id: "W1", title: "Keep D", status: "blocked", phase: "P1", version: "V2" },
      { id: "HIDDEN", title: "Excluded", status: "blocked", phase: "P1", version: "" },
    ],
  } }, error: null };
  for (const [route, expected] of [
    ["board", ["T10", "T2", "D2", "W1", "C1"]],
    ["board&group=phase", ["T2", "W1", "C1", "T10", "D2"]],
    ["board&group=version", ["T10", "C1", "D2", "W1", "T2"]],
    ["list", ["C1", "D2", "T2", "T10", "W1"]],
    ["list&sort=title&direction=desc", ["C1", "W1", "D2", "T10", "T2"]],
    ["list&sort=status", ["T2", "T10", "D2", "W1", "C1"]],
  ]) {
    const [view, ...params] = route.split("&");
    const ui = await app(`#${view}?project=first&q=Keep&${params.join("&")}&issue=${expected[0]}`, { first: snapshot });
    const disabled = step => /\bdisabled\b/.test(ui.element("#issue-panel").innerHTML.match(new RegExp(`<button[^>]*data-issue-step="${step}"[^>]*>`))[0]);
    assert.equal(disabled(-1), true, route);
    assert.equal(disabled(1), false, route);
    const initialHash = ui.location.hash;
    ui.key("k");
    assert.equal(ui.location.hash, initialHash, "Previous at the start must not wrap or create history");
    for (const id of expected.slice(1)) {
      ui.click({ "[data-issue-step]": { dataset: { issueStep: "1" } } });
      assert.equal(ui.run("state.issue"), id, route);
      assert.equal(new URLSearchParams(ui.location.hash.split("?")[1]).get("issue"), id);
      assert.equal(ui.element("#issue-panel").hidden, false);
    }
    assert.equal(disabled(-1), false);
    assert.equal(disabled(1), true);
    ui.key("j");
    assert.equal(ui.run("state.issue"), expected.at(-1), "Next at the end must not wrap");
    ui.click({ "[data-issue-step]": { dataset: { issueStep: "1" }, disabled: true } });
    assert.equal(ui.run("state.issue"), expected.at(-1));
    for (const id of expected.slice(0, -1).reverse()) {
      assert.equal(ui.key("K").prevented, true);
      assert.equal(ui.run("state.issue"), id, route);
    }
    assert.equal(ui.key("J").prevented, true);
    assert.equal(ui.run("state.issue"), expected[1]);
    for (const field of ["input", "textarea", "select", "[contenteditable]"]) {
      const target = { closest: selector => selector.includes(field) ? {} : null };
      for (const key of ["j", "k", "J", "K"]) assert.equal(ui.key(key, { target }).prevented, undefined);
      assert.equal(ui.run("state.issue"), expected[1], `Typing in ${field} must not navigate`);
    }
    for (const extra of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { isComposing: true }]) ui.key("j", extra);
    assert.equal(ui.run("state.issue"), expected[1]);
    ui.run('navigate({ issue: "HIDDEN" })');
    assert.equal(disabled(-1) && disabled(1), true, "Dependency links must not add filtered-out issues to the sequence");
    ui.key("j");
    assert.equal(ui.run("state.issue"), "HIDDEN");
    ui.run('navigate({ issue: "" })');
    assert.equal(ui.key("j").prevented, undefined, "J/K work only with an open detail panel");
  }
  const collapsed = await app("#board?project=first&issue=T2", { first: snapshot }, { hiddenIssues: ["D2", "T10", "W1", "C1", "HIDDEN"] });
  assert.equal(collapsed.run('issueNeighbors().previous + issueNeighbors().next'), "", "Collapsed issue links are not in the visible sequence");
  console.log("PASS issue navigation: board columns/groups, list sorting, filter boundaries, no wrap, route changes and J/K input guards");
}

async function clipboardVerbatim() {
  const writes = [];
  const ui = await app("#board?project=first&issue=T1", {}, { navigator: { clipboard: { writeText(text) { writes.push(text); return Promise.resolve(); } } } });
  const value = ["/goal", "a / b", "x86/arm64", String.raw`C:\work\file.txt`, "tokens=4096", "password=LEFT#RIGHT"].join(", ");
  ui.run(`Object.assign(rows().tasks[0], ${JSON.stringify({ title: value, done_when: value, deps: value })}); render()`);
  ui.click({ "[data-issue-copy]": { dataset: { issueCopy: "handoff" } } });
  assert.deepEqual(writes, [`First project T1: ${value}\n状態: 未着手\n段階: P1 · Phase\n版: V1\n完了条件: ${value}\n依存先: ${value}`], "Copy must preserve title, done_when and deps exactly as displayed");
  assert.equal(ui.run('clipboardField("first\\r\\nsecond\\rthird")'), "first\nsecond\nthird");
  assert.equal(ui.run("clipboardField(42)"), "42");
  assert.equal(ui.run("clipboardField(null)"), "");
  console.log("PASS clipboard verbatim: title, done_when and deps preserve displayed text; line endings and String coercion remain");
}

async function issueCopyPerformance() {
  const ui = await app("#board?project=first&issue=T1");
  const issueCopyText = ui.run("issueCopyText");
  const task = { id: "T1", title: "a".repeat(200_000), status: "todo" };
  const start = process.hrtime.bigint();
  const text = issueCopyText(task);
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  assert.equal(text, `First project T1: ${task.title}\n状態: 未着手\n完了条件: —`);
  assert.ok(elapsedMs < 50, `Copying a 200,000-character title took ${elapsedMs.toFixed(3)} ms; expected under 50 ms`);
  console.log(`PASS issue copy performance: 200,000-character title in ${elapsedMs.toFixed(3)} ms (< 50 ms)`);
}

async function issueCopy() {
  const writes = [];
  const navigator = { clipboard: { async writeText(text) { writes.push(text); } } };
  const ui = await app("#board?project=first&issue=T1", {}, { navigator });
  ui.run('Object.assign(rows().tasks[0], { done_when: "条件 <script> & 詳細\\n次の行", deps: "T2, T3" })');
  Object.assign(ui.snapshots.first.last_good.rows.tasks[0], linkedPR());
  await ui.events.project({ data: '"first"' });
  ui.run('Object.assign(rows().tasks[0], { done_when: "条件 <script> & 詳細\\n次の行", deps: "T2, T3", note: "PRIVATE_NOTE", source: "PRIVATE_TASK_PATH" }); state.projects[0].source = "PRIVATE_PROJECT_PATH"; state.projects[0].token = "PRIVATE_TOKEN"; state.snapshot.last_good.meta.source = "PRIVATE_META_PATH"');
  assert.equal(writes.length, 0, "Rendering must never copy without a click");
  const clickCopy = (viewer, kind) => viewer.click({ "[data-issue-copy]": { dataset: { issueCopy: kind } } });
  clickCopy(ui, "key");
  assert.deepEqual(writes, ["T1"], "Clipboard writes happen directly during the click");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.element("#issue-copy-status").textContent, "コピーしました");
  assert.equal(ui.element("#issue-copy-fallback").hidden, true);
  clickCopy(ui, "handoff");
  const expected = "First project T1: First task\n状態: 未着手\n段階: P1 · Phase\n版: V1\n完了条件: 条件 <script> & 詳細\n次の行\n依存先: T2, T3\nPR #6: マージ済み https://github.com/owner/repo/pull/6";
  assert.equal(writes.at(-1), expected);
  assert.doesNotMatch(writes.at(-1), /PRIVATE_/);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(ui.element("#issue-panel").innerHTML, /id="issue-copy-status"[^>]*aria-live="polite"/);
  assert.match(ui.element("#issue-panel").innerHTML, /<textarea[^>]*readonly[^>]*aria-describedby="issue-copy-status"/);
  assert.equal(ui.run('issueCopyText({ id: "S1", title: "簡素", status: "blocked", done_when: "" })'), "First project S1: 簡素\n状態: 待ち\n完了条件: —");
  assert.equal(ui.run('issueCopyText({ id: "S1", title: "簡素", status: "todo", pr: [7] })').split("\n").at(-1), "PR #7: 状態未確認");

  for (const navigator of [undefined, {}, { clipboard: {} }, { clipboard: { writeText() { throw new Error("denied"); } } }, { clipboard: { async writeText() { throw new Error("denied"); } } }]) {
    for (const kind of ["key", "handoff"]) {
      const fallback = await app("#list?project=first&issue=T1", {}, { navigator });
      clickCopy(fallback, kind);
      await new Promise(resolve => setImmediate(resolve));
      const textarea = fallback.element("#issue-copy-text");
      assert.equal(fallback.element("#issue-copy-fallback").hidden, false);
      assert.match(fallback.element("#issue-copy-status").textContent, /Ctrl\+C/);
      assert.equal(textarea.value, kind === "key" ? "T1" : "First project T1: First task\n状態: 未着手\n段階: P1 · Phase\n版: V1\n完了条件: —");
      assert.equal(fallback.run('document.activeElement === $("#issue-copy-text")'), true);
      assert.equal(textarea.selectionStart, 0);
      assert.equal(textarea.selectionEnd, textarea.value.length);
      fallback.run("render()");
      assert.equal(fallback.run('document.activeElement === $("#issue-copy-text")'), true, "A live render must keep the fallback selected");
      assert.equal(textarea.selectionEnd, textarea.value.length);
      assert.match(fallback.element("#issue-panel").innerHTML, /自動コピーできません/);
      fallback.run('navigate({ issue: "" }); navigate({ issue: "T1" })');
      assert.equal(fallback.run("state.copyFeedback"), null);
    }
  }
  ui.run('rows().tasks[0].prs[0].url += "?extra=value#fragment"');
  assert.equal(ui.run('issueCopyText(rows().tasks[0])').split("\n").at(-1), "PR #6: マージ済み https://github.com/owner/repo/pull/6");
  ui.run('rows().tasks[0].prs[0].url = "https://example.test/pull/6"');
  assert.equal(ui.run('issueCopyText(rows().tasks[0])').split("\n").at(-1), "PR #6: マージ済み");
  ui.run('rows().tasks[0].prs[0].status = "error"');
  assert.equal(ui.run('issueCopyText(rows().tasks[0])').split("\n").at(-1), "PR #6: 前回: マージ済み");

  let reject;
  const pending = await app("#board?project=first&issue=T1", {}, { navigator: { clipboard: { writeText() { return new Promise((_resolve, fail) => { reject = fail; }); } } } });
  clickCopy(pending, "handoff");
  pending.run('rows().tasks.push({ id: "T2", title: "次", status: "todo" }); navigate({ issue: "T2" })');
  reject(new Error("denied"));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.run("state.copyFeedback"), null, "A late clipboard result must not affect a different issue");
  assert.doesNotMatch(pending.element("#issue-panel").innerHTML, /自動コピーできません/);
  const jobs = [];
  const repeated = await app("#board?project=first&issue=T1", {}, { navigator: { clipboard: { writeText() { return new Promise((resolve, reject) => jobs.push({ resolve, reject })); } } } });
  clickCopy(repeated, "key");
  clickCopy(repeated, "handoff");
  jobs[1].resolve();
  jobs[0].reject(new Error("Old request denied"));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(repeated.element("#issue-copy-status").textContent, "コピーしました");
  assert.equal(repeated.element("#issue-copy-fallback").hidden, true, "An old rejection must not replace the latest copy result");
  console.log("PASS issue copy: plain handoff/key, PR states/URLs, allowlisted fields, clipboard denial/absence and stale results");
}

async function shortcuts() {
  const ui = await app("#board?project=first&issue=T1");
  const dialog = ui.element("#shortcuts-dialog");
  const close = ui.element("#shortcuts-close");
  dialog.querySelectorAll = () => [close];
  const opener = ui.element("#content");
  opener.focus();
  const route = ui.location.hash;
  assert.equal(ui.key("?", { shiftKey: true }).prevented, true);
  assert.equal(dialog.open, true);
  assert.equal(ui.run('document.activeElement === $("#shortcuts-close")'), true);
  assert.equal(ui.key("Tab").prevented, true);
  assert.equal(ui.key("Tab", { shiftKey: true }).prevented, true);
  for (const key of ["j", "k", "/", "?"]) ui.key(key);
  ui.key("[", { ctrlKey: true });
  assert.equal(ui.location.hash, route, "Help must not navigate the underlying detail panel");
  assert.equal(ui.run("state.sidebarCollapsed"), false);
  assert.equal(ui.key("Escape").prevented, true);
  assert.equal(dialog.open, false);
  assert.equal(ui.run('document.activeElement === $("#content")'), true);
  assert.equal(ui.location.hash, route, "Escape closes only the top dialog");
  ui.key("?");
  close.listeners.click();
  assert.equal(dialog.open, false);
  assert.equal(ui.run('document.activeElement === $("#content")'), true);
  ui.key("?");
  let cancelled = false;
  dialog.listeners.cancel({ preventDefault() { cancelled = true; } });
  assert.equal(cancelled, true);
  assert.equal(dialog.open, false);
  assert.equal(ui.run('document.activeElement === $("#content")'), true);
  for (const field of ["input", "textarea", "select", "[contenteditable]"]) {
    assert.equal(ui.key("?", { target: { closest: selector => selector.includes(field) ? {} : null } }).prevented, undefined);
    assert.equal(dialog.open, false, `Typing ? in ${field} must not open help`);
  }
  for (const extra of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { isComposing: true }]) ui.key("?", extra);
  assert.equal(dialog.open, false);
  ui.key("?");
  ui.replaceElement("#content");
  close.listeners.click();
  assert.equal(ui.run('document.activeElement === $("#content")'), true, "Focus can return to a replacement control after a live render");
  assert.equal(ui.key("/").prevented, true);
  assert.equal(ui.run('document.activeElement === $("#global-search")'), true, "Slash keeps the existing search behavior");
  assert.equal(ui.run("state.issue"), "");
  const html = fs.readFileSync(path.join(__dirname, "../skills/live-board/scripts/static/index.html"), "utf8");
  const help = html.match(/<dialog id="shortcuts-dialog"[\s\S]*?<\/dialog>/)[0];
  for (const key of ["/", "Esc", "Ctrl+[", "J", "K", "?"]) assert.ok(help.includes(`<kbd>${key}</kbd>`));
  assert.match(help, /aria-labelledby="shortcuts-heading"/);
  console.log("PASS shortcuts: help content, question mark, typing guards, Tab trap, Escape/close/cancel and focus return");
}

async function projects() {
  const unselected = await app("#board?project=first");
  assert.equal(unselected.run("document.activeElement === null"), true, "Opening a project without an issue must not move focus past its header and filters");
  const ui = await app("#list?project=second&issue=T1");
  assert.equal(ui.run("state.projectId"), "second");
  assert.equal(ui.element("#project-name").textContent, "Second project");
  assert.equal(ui.run("state.snapshot.last_good.meta.product"), "Second project data");
  assert.equal(ui.run("state.issue"), "T1");
  assert.equal(ui.element("#route-notice").hidden, true);

  ui.location.hash = "#board?project=first&issue=T1";
  ui.listeners.hashchange();
  assert.equal(ui.run("state.snapshot"), null, "History must not show the other project's snapshot");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.run("state.projectId"), "first");
  assert.equal(ui.element("#project-name").textContent, "First project");
  assert.equal(ui.run("state.snapshot.last_good.meta.product"), "Fixture");
  ui.location.hash = "#board?project=second";
  ui.listeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(new URLSearchParams(ui.location.hash.split("?")[1]).get("project"), "second");
  assert.equal(ui.run("state.issue"), "");
  ui.run('navigate({ issue: "T1" })');
  const reloaded = await app(ui.location.hash);
  assert.equal(reloaded.run("state.projectId"), "second");
  assert.equal(reloaded.run("state.issue"), "T1");

  for (const [hash, missing] of [
    ["#list?project=gone&issue=T1", "gone"],
    ["#list?project=second&issue=deleted", "deleted"],
    ["#list?project=first&issue=deleted", "deleted"],
  ]) {
    const fallback = await app(hash);
    assert.equal(fallback.run("state.projectId"), "first");
    assert.equal(fallback.run("state.issue"), "");
    assert.equal(fallback.element("#project-name").textContent, "First project");
    assert.equal(fallback.element("#route-notice").hidden, false);
    assert.ok(fallback.element("#route-notice").textContent.includes(missing));
    const params = new URLSearchParams(fallback.location.hash.split("?")[1]);
    assert.equal(params.get("project"), "first");
    assert.equal(params.has("issue"), false);
  }

  reloaded.snapshots.second.last_good.rows.tasks = [];
  reloaded.snapshots.second.last_good.loaded_at = "2026-10-31T00:01:00Z";
  await reloaded.run("refresh()");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reloaded.run("state.projectId"), "first");
  assert.equal(reloaded.run("state.issue"), "");
  assert.equal(reloaded.element("#route-notice").hidden, false);

  const legacy = await app("#list?task=T1");
  assert.equal(legacy.run("state.projectId"), "first");
  assert.equal(legacy.run("state.issue"), "T1");
  assert.equal(legacy.element("#route-notice").hidden, true);
  const unreadable = await app("#list?project=second&issue=T1", {
    second: { last_good: null, error: { message: "Read failure" }, checked_at: new Date().toISOString() },
  });
  assert.equal(unreadable.run("state.projectId"), "second");
  assert.equal(unreadable.run("state.issue"), "T1");
  assert.equal(unreadable.element("#route-notice").hidden, true);
  console.log("PASS project-route: switch/reload/history preserve project; missing project/issue visibly fall back to first and drop issue; live removal, legacy links and read failures verified");
}

async function directory() {
  const ui = await app("#projects");
  assert.equal(ui.run("state.view"), "projects");
  assert.equal(ui.location.hash, "#projects");
  assert.equal(ui.run("state.projectId"), "");
  assert.equal(ui.run("state.snapshot"), null);
  assert.deepEqual(ui.requests, ["/healthz", "/api/projects", "/api/summary", "/api/projects/first", "/api/projects/second"]);
  assert.equal(ui.element("#filters").hidden, true);
  assert.equal(ui.element("#project-tools").hidden, false);
  assert.match(ui.element("#nav-projects").innerHTML, /href="#projects">すべてのプロジェクトを表示/);
  const html = () => ui.element("#content").innerHTML;
  assert.ok(html().indexOf('data-project="second"') < html().indexOf('data-project="first"'));
  assert.match(html(), /href="#board\?project=second"/);
  assert.match(html(), /7 \/ 8 済み/);
  assert.match(html(), /待ち 1/);
  assert.match(html(), /wbs-cards/);
  assert.match(html(), /data-relative-time=/);
  assert.match(html(), />正常</);

  ui.element("#content").listeners.click({ target: { closest: selector => selector === "[data-sort]" ? { dataset: { sort: "name" } } : null } });
  assert.ok(html().indexOf('data-project="first"') < html().indexOf('data-project="second"'));
  ui.element("#project-search").listeners.input({ target: { value: "SECOND" } });
  assert.match(html(), /data-project="second"/);
  assert.doesNotMatch(html(), /data-project="first"/);
  const reloaded = await app(ui.location.hash);
  assert.equal(reloaded.run("state.view"), "projects");
  assert.equal(reloaded.run("state.q"), "SECOND");
  assert.equal(reloaded.run("state.sort"), "name");
  ui.element("#project-search").listeners.input({ target: { value: "missing" } });
  assert.match(html(), /検索条件に一致するプロジェクトはありません/);
  ui.element("#project-search").listeners.input({ target: { value: "" } });
  assert.equal(ui.requests.length, 5, "Search and sorting are local");
  const unaffectedReads = ui.requests.filter(url => url === "/api/projects/first").length;

  ui.summaries[1].error = { message: "broken <source&>", at: "2026-10-31T01:00:00Z" };
  ui.events.project({ data: '"second"' });
  await new Promise(resolve => setImmediate(resolve));
  assert.match(html(), /7 \/ 8 済み/);
  assert.match(html(), /class="project-error" title="[^"]*">読み込み失敗<\/span>/);
  assert.match(html(), /broken &lt;source&amp;&gt;/);
  assert.match(html(), /最後に読めた時刻:/);
  assert.ok(html().includes(ui.run('formatTime("2026-10-31T01:00:00Z", true)')));
  assert.equal(ui.requests.filter(url => url === "/api/projects/first").length, unaffectedReads, "SSE refreshes only the changed project for global search");
  ui.transport.failSummary = true;
  await ui.events.heartbeat();
  assert.match(ui.element("#error-banner").textContent, /HTTP 503/);
  assert.match(ui.element("#error-banner").textContent, /最後に取得できた集計/);
  assert.match(html(), /7 \/ 8 済み/);
  ui.transport.failSummary = false;
  ui.summaries[1].error = null;
  ui.summaries[1].warnings = ["unknown <status>"];
  await ui.events.heartbeat();
  assert.equal(ui.element("#error-banner").hidden, true);
  assert.match(html(), /検証警告.*unknown &lt;status&gt;/);

  const row = { dataset: { project: "second" } };
  ui.element("#content").listeners.click({ target: { closest: selector => selector === "tr[data-project]" ? row : null }, button: 0 });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.location.hash, "#board?project=second");
  assert.equal(ui.run("state.snapshot.last_good.meta.product"), "Second project data");
  ui.location.hash = "#projects";
  ui.listeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.location.hash, "#projects");
  assert.equal(ui.run("state.snapshot"), null);
  ui.location.hash = "#board?project=first";
  ui.listeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.location.hash, "#board?project=first");
  ui.location.hash = "#projects";
  ui.listeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.run("state.view"), "projects");

  const single = await app("", {}, { projects: [{ id: "first", name: "First project" }] });
  assert.equal(single.run("state.view"), "summary");
  assert.equal(single.run("state.projectId"), "first");
  assert.equal(single.element("#project-tabs").hidden, false);
  assert.equal((await app("#board?project=second")).run("state.view"), "board");
  const many = Array.from({ length: 51 }, (_, index) => ({
    ...ui.summaries[0], id: `p${index}`, name: `Project ${String(index).padStart(2, "0")}`,
    adapter: index < 26 ? "wbs-cards" : "winsmux-backlog", source_mtime: index,
  }));
  const paged = await app("#projects", {}, { projects: many.map(({ id, name }) => ({ id, name })), summaries: many });
  const pageHtml = () => paged.element("#content").innerHTML;
  assert.equal((pageHtml().match(/<tr data-project=/g) || []).length, 25);
  assert.match(pageHtml(), /data-project="p50"/);
  assert.match(pageHtml(), /data-page="3"/);
  assert.match(paged.element("#result-count").textContent, /51 件のプロジェクトが見つかりました/);
  paged.element("#content").listeners.click({ target: { closest: selector => selector === "[data-page]" ? { dataset: { page: "3" } } : null } });
  assert.equal((pageHtml().match(/<tr data-project=/g) || []).length, 1);
  assert.match(pageHtml(), /data-project="p0"/);
  assert.match(paged.location.hash, /page=3/);
  const pageReload = await app(paged.location.hash, {}, { projects: many, summaries: many });
  assert.equal(pageReload.run("state.page"), 3);
  paged.element("#project-type").listeners.change({ target: { value: "wbs-cards" } });
  assert.equal(paged.run("state.page"), 1);
  assert.match(paged.element("#result-count").textContent, /26 件/);
  assert.match(pageHtml(), /data-project="p25"/);
  assert.doesNotMatch(pageHtml(), /data-project="p50"/);
  assert.match(paged.location.hash, /type=wbs-cards/);
  paged.run('navigate({ page: 2 })');
  assert.equal((pageHtml().match(/<tr data-project=/g) || []).length, 1);
  paged.element("#project-search").listeners.input({ target: { value: "Project 03" } });
  assert.equal(paged.run("state.page"), 1);
  assert.match(pageHtml(), /data-project="p3"/);
  paged.run('navigate(parseHash("#projects?type=unknown&page=99"))');
  assert.equal(paged.run("state.page"), 1);
  assert.match(pageHtml(), /検索条件に一致/);
  const star = { dataset: { starProject: "p3" } };
  const beforeStar = paged.location.hash;
  paged.element("#content").listeners.click({ target: { closest: selector => selector === "[data-star-project]" ? star : null }, button: 0 });
  paged.click({ "[data-star-project]": star });
  assert.equal(paged.location.hash, beforeStar, "Star toggles must not open a directory row");
  const clean = await app("#projects?project=second&issue=T1");
  assert.equal(clean.location.hash, "#projects");
  assert.equal(clean.run("state.issue"), "");
  const unavailable = await app("#projects", {}, { summaries: [
    { id: "first", name: "<missing>", adapter: "wbs-cards", counts: null, total: null, owner_wait: null, source_mtime: null, checked_at: new Date().toISOString(), loaded_at: null, error: { message: "missing", at: "2026-10-31T01:00:00Z" }, warnings: [] },
  ] });
  assert.match(unavailable.element("#content").innerHTML, /&lt;missing&gt;/);
  assert.match(unavailable.element("#content").innerHTML, /未取得/);
  assert.doesNotMatch(unavailable.element("#content").innerHTML, /0 \/ 0 済み/);
  const initialFailure = await app("#projects", {}, { failProjects: true });
  assert.equal(initialFailure.element("#error-banner").hidden, false);
  assert.match(initialFailure.element("#error-banner").textContent, /プロジェクト一覧を取得できません/);
  initialFailure.transport.failProjects = false;
  await initialFailure.events.heartbeat();
  assert.equal(initialFailure.element("#error-banner").hidden, true);
  assert.match(initialFailure.element("#content").innerHTML, /data-project="second"/);
  console.log("PASS project-directory: route/history, search/sort, row navigation, changed-project SSE, cached errors, warning escaping and initial failures verified");
}

async function wbs() {
  const tasks = [
    { id: "S1", title: "<b>判断待ち</b>", raw_status: "p-owner", status: "blocked", ball: false },
    { id: "B1", title: "ボールあり", raw_status: "p-hold", status: "todo", ball: true },
    { id: "U1", title: "不明な状態", raw_status: "future-status", status: "todo", ball: false },
  ].map(task => ({ phase: "段階", version: "", note: "補足 <script>raw</script>\n原文", label: "元ラベル", done_when: "", deps: "", blocked_by: "", ready: "no", ...task }));
  const ui = await app("#board?project=first&issue=S1", { first: {
    last_good: {
      loaded_at: "2026-10-31T00:00:00Z", done_hidden: 4, warnings: ["不明な状態 future-status を未着手として扱いました。"],
      meta: { product: "WBS", updated: "2026-10-31", current_phase: "" },
      rows: { phases: [{ id: "段階", title: "段階", order: 0, status: "blocked" }], versions: [], tasks },
    }, error: null, checked_at: new Date().toISOString(),
  } });
  assert.equal((ui.element("#content").innerHTML.match(/class="board-card /g) || []).length, 3);
  assert.equal((ui.element("#content").innerHTML.match(/>自分待ち</g) || []).length, 2);
  assert.match(ui.element("#content").innerHTML, /非表示の完了 4 件/);
  assert.match(ui.element("#issue-panel").innerHTML, /自分待ち/);
  assert.match(ui.element("#issue-panel").innerHTML, /<dt>元の状態<\/dt><dd>自分待ち<\/dd>/);
  assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /p-owner/);
  assert.match(ui.element("#issue-panel").innerHTML, /補足 &lt;script&gt;raw&lt;\/script&gt;/);
  assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /<script>/);
  assert.match(ui.element("#error-banner").textContent, /検証警告.*future-status/);
  ui.run('navigate({ issue: "B1" })');
  assert.match(ui.element("#issue-panel").innerHTML, /自分待ち/);
  assert.match(ui.element("#issue-panel").innerHTML, /<dt>元の状態<\/dt><dd>未着手<\/dd>/);
  assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /p-hold/);
  ui.run('navigate({ view: "summary", issue: "" })');
  assert.match(ui.element("#content").innerHTML, /4 \/ 7 済み/);
  assert.equal(ui.run("rows().tasks.length"), 3);
  assert.equal(ui.element("#version-filter").hidden, true);
  ui.run('navigate({ view: "releases" })');
  assert.match(ui.element("#content").innerHTML, /このプロジェクトに版はありません/);
  ui.run('navigate({ view: "list", version: "__none__" })');
  assert.equal(ui.run("filteredTasks().length"), 3);
  assert.doesNotMatch(ui.element("#filter-version").innerHTML, /存在しません/);
  for (const [raw, status, label] of [["p-run", "doing", "進行中"], ["p-hold", "todo", "未着手"], ["p-wait", "blocked", "待ち"], ["p-owner", "blocked", "自分待ち"], ["p-sora", "blocked", "自分待ち"]]) {
    ui.run(`Object.assign(rows().tasks[0], { raw_status: ${JSON.stringify(raw)}, status: ${JSON.stringify(status)} }); navigate({ issue: "S1" })`);
    assert.ok(ui.element("#issue-panel").innerHTML.includes(`<dt>元の状態</dt><dd>${label}</dd>`));
    assert.ok(!ui.element("#issue-panel").innerHTML.includes(raw));
  }
  console.log("PASS WBS views: Japanese status labels, owner markers, escaped notes, hidden done totals, warnings and zero-version states verified");
}

async function styleWarnings() {
  const ui = await app("#board?project=first");
  ui.snapshots.first.last_good.warnings = [];
  ui.snapshots.first.last_good.style_warnings = Array.from({ length: 11 }, (_, i) => `書き方 ${i}: <note> & 補足`);
  ui.snapshots.first.last_good.loaded_at = "2026-11-01T00:00:00Z";
  ui.summaries[0].style_warnings = ui.snapshots.first.last_good.style_warnings;
  await ui.run("refresh()");
  ui.events.open();
  assert.equal(ui.element("#error-banner").hidden, true);
  assert.doesNotMatch(ui.element("#freshness-message").textContent, /検証警告あり/);
  assert.equal(ui.element("#freshness").className, "freshness healthy");
  assert.equal(ui.element("#style-warnings").hidden, false);
  assert.equal(ui.element("#style-warnings").open, false);
  assert.equal(ui.element("#style-warnings-summary").textContent, "書き方の警告 11 件");
  assert.equal((ui.element("#style-warnings-list").innerHTML.match(/<li>/g) || []).length, 11);
  assert.match(ui.element("#style-warnings-list").innerHTML, /&lt;note&gt; &amp; 補足/);
  assert.doesNotMatch(ui.element("#style-warnings-list").innerHTML, /<note>/);
  ui.element("#style-warnings").open = true;
  ui.run("render()");
  assert.equal(ui.element("#style-warnings").open, true, "Rerendering the project preserves the opened disclosure");
  for (const view of ["for-you", "projects"]) {
    await ui.run(`navigate({ view: "${view}" })`);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ui.element("#style-warnings").hidden, true);
    assert.equal(ui.element("#error-banner").hidden, true);
    assert.doesNotMatch(ui.element("#freshness-message").textContent, /検証警告あり/);
    assert.doesNotMatch(ui.element("#content").innerHTML, /書き方|project-error/);
  }
  await ui.run('navigate({ view: "board", projectId: "first" })');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.element("#style-warnings").open, false);
  ui.run('state.snapshot.last_good.warnings = ["不明な状態 future-status"]; renderFreshness()');
  assert.equal(ui.element("#error-banner").hidden, false);
  assert.match(ui.element("#error-banner").textContent, /検証警告.*future-status/);
  assert.doesNotMatch(ui.element("#error-banner").textContent, /書き方/);
  assert.match(ui.element("#freshness-message").textContent, /検証警告あり/);
  ui.run('state.snapshot.last_good.style_warnings = []; render()');
  assert.equal(ui.element("#style-warnings").hidden, true);
  assert.equal(ui.element("#style-warnings-list").innerHTML, "");
  await ui.run('navigate({ projectId: "second" })');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.element("#style-warnings").hidden, true, "Older snapshots without style_warnings remain supported");
  console.log("PASS style warnings: collapsed disclosure, escaped messages, zero warnings, cross-project views and separate validation errors");
}

async function unknownStatuses() {
  const ui = await app("#list?project=first&issue=T1");
  for (const raw of ["future-status", "constructor", "__proto__", "<future&status>", ""]) {
    ui.run(`Object.assign(rows().tasks[0], { raw_status: ${JSON.stringify(raw)}, status: "todo" }); render()`);
    const expected = ui.run(`escape(${JSON.stringify(raw)})`);
    assert.ok(ui.element("#issue-panel").innerHTML.includes(`<dt>元の状態</dt><dd>不明（${expected}）</dd>`), `Unknown source status must remain visible: ${raw}`);
    assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /<dt>元の状態<\/dt><dd>未着手<\/dd>/);
    assert.ok(ui.run(`lozenge(${JSON.stringify(raw)})`).includes(`>不明（${expected}）</span>`), "Status badges must handle unknown and inherited property names as data");
  }
  for (const [raw, label] of [["p-run", "進行中"], ["p-hold", "未着手"], ["p-wait", "待ち"], ["p-owner", "自分待ち"], ["p-sora", "自分待ち"]]) {
    ui.run(`Object.assign(rows().tasks[0], { raw_status: ${JSON.stringify(raw)}, status: "todo" }); render()`);
    assert.ok(ui.element("#issue-panel").innerHTML.includes(`<dt>元の状態</dt><dd>${label}</dd>`), "Source labels must follow the raw code, independently of its normalized status");
  }
  console.log("PASS unknown statuses: original raw codes, known Japanese labels, escaped values and inherited property names");
}

function linkedPR() {
  return {
    pr: [6], prs: [{ number: 6, url: "https://github.com/owner/repo/pull/6", status: "ok", error: null,
      fetched_at: "2026-10-31T00:00:00Z", age_seconds: 0,
      data: { number: 6, url: "https://github.com/owner/repo/pull/6", title: "PR の長い題 <script> & 詳細".repeat(8),
        state: "MERGED", is_draft: false, merged_at: "2026-10-31T00:00:00Z", ci: "success" } }],
    pr_flags: [{ kind: "mismatch", message: "PR 6番はマージ済み・課題は進行中" }],
  };
}

async function pullRequests() {
  const ui = await app("#board?project=first&issue=T1");
  const snapshot = ui.snapshots.first;
  const task = snapshot.last_good.rows.tasks[0];
  Object.assign(task, { status: "doing", ...linkedPR() });
  snapshot.pr_revision = 1;
  snapshot.last_good.source_mtime = new Date("2026-10-31T12:00:00").getTime() / 1000;
  snapshot.last_good.meta.updated = "2026-10-30";
  snapshot.last_good.source_date = { declared_local_date: "2026-10-30", file_local_date: "2026-10-31", mismatch: true };
  const summary = { linked_count: 1, mismatch_count: 1, ci_failure_count: 0, unknown_count: 0, errors: [] };
  snapshot.last_good.pr_summary = summary;
  ui.summaries[0].pr_summary = summary;
  const refresh = () => ui.events.project({ data: '"first"' });
  await refresh();
  for (const selector of ["#content", "#issue-panel"]) {
    const html = ui.element(selector).innerHTML;
    assert.match(html, /PR #6 · マージ済み/);
    assert.match(html, /href="https:\/\/github.com\/owner\/repo\/pull\/6" target="_blank" rel="noreferrer"/);
    assert.match(html, /PR 6番はマージ済み・課題は進行中/);
    assert.match(html, /PR の長い題 &lt;script&gt; &amp; 詳細/);
    assert.doesNotMatch(html, /<script>/);
  }
  assert.match(ui.element("#project-meta").textContent, /記載 2026\/10\/30・ファイル 2026\/10\/31/);
  assert.equal(ui.element("#project-meta").classList.contains("source-date-mismatch"), true);
  assert.doesNotMatch(ui.element("#content").innerHTML, /<a\b[^>]*data-issue[^>]*>(?:(?!<\/a>)[\s\S])*<a\b/, "A PR link must not be nested inside the card's issue link");
  ui.run('navigate({ view: "list" })');
  assert.match(ui.element("#content").innerHTML, /PR #6 · マージ済み/);
  assert.match(ui.element("#content").innerHTML, /data-pr-flag="mismatch"/);

  task.prs[0].status = "error";
  task.prs[0].error = { message: "PR の状態を取得できません（gh の認証を確認してください）" };
  task.pr_flags = [];
  snapshot.pr_revision++;
  Object.assign(summary, { mismatch_count: 0, unknown_count: 1, errors: [task.prs[0].error.message] });
  await refresh();
  for (const selector of ["#content", "#issue-panel"]) {
    const html = ui.element(selector).innerHTML;
    assert.match(html, /PR の状態を取得できません（gh の認証を確認してください）/);
    assert.match(html, /PR #6 · 前回: マージ済み/);
    assert.match(html, /前回の取得から [\d,]+ 秒/);
    assert.match(html, /CI 成功（前回）/);
    assert.doesNotMatch(html, /data-pr-flag=/, "Last-good PRs must never generate mismatch flags while unknown");
  }
  task.prs[0].data = null;
  task.prs[0].fetched_at = null;
  snapshot.pr_revision++;
  await refresh();
  assert.match(ui.element("#content").innerHTML, /PR #6 · 状態未確認/);
  assert.doesNotMatch(ui.element("#content").innerHTML, /CI なし|前回の取得から/);
  ui.run('toggleStar("first"); navigate({ view: "for-you" })');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(ui.element("#content").innerHTML, /確認済み: PR の食い違い 0 件 · CI 失敗 0 件 · PR 未確認 1 件/);
  ui.run('navigate({ view: "projects" })');
  assert.match(ui.element("#content").innerHTML, /PR 未確認 1 件/);

  Object.assign(task, linkedPR());
  Object.assign(summary, { mismatch_count: 1, unknown_count: 0, errors: [] });
  snapshot.pr_revision++;
  await refresh();
  assert.match(ui.element("#content").innerHTML, /PR の食い違い 1 件/);
  ui.run('navigate({ view: "for-you", homeTab: "doing" })');
  assert.match(ui.element("#content").innerHTML, /PR の食い違い 1 件/);
  assert.match(ui.element("#content").innerHTML, /PR 6番はマージ済み・課題は進行中/);
  ui.run('navigate({ view: "board", projectId: "first", issue: "T1" })');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(ui.element("#content").innerHTML, /data-pr-flag="mismatch"/);
  assert.doesNotMatch(ui.element("#content").innerHTML, /PR の状態を取得できません/);
  snapshot.last_good.source_date.mismatch = false;
  snapshot.last_good.loaded_at = "2026-10-31T00:01:00Z";
  await refresh();
  assert.equal(ui.element("#project-meta").classList.contains("source-date-mismatch"), false);
  snapshot.last_good.source_date.file_local_date = null;
  snapshot.last_good.source_date.mismatch = null;
  snapshot.pr_revision++;
  await refresh();
  assert.match(ui.element("#project-meta").textContent, /ファイル 日付不明/);
  console.log("PASS PR views: badge links, mismatch flags, safe titles, stale age/CI, initial fetch failure, PR-only refresh, summaries and source date mismatch/recovery");
}

async function activity() {
  const heartbeat = "2026-11-02T00:00:00Z";
  let currentTime = Date.parse(heartbeat) + 120_000;
  class Clock extends Date { static now() { return currentTime; } }
  const liveLines = [];
  const ui = await app("#board?project=first&issue=T1", {}, {
    Date: Clock,
    querySelectorAll: selector => selector === "[data-activity-heartbeat]" ? liveLines : [],
  });
  assert.doesNotMatch(ui.element("#content").innerHTML, /activity-line/);
  const snapshot = ui.snapshots.first;
  const loaded = snapshot.last_good.loaded_at;
  const entry = { file: "T1.json", card: "T1", step: "試験 <script>", detail: "Python & CJS", started_at: heartbeat, heartbeat_at: heartbeat };
  snapshot.activity = { count: 1, items: [entry], errors: [], unmatched: [] };
  snapshot.activity_revision = 1;
  const refresh = () => ui.events.project({ data: '"first"' });
  await refresh();
  assert.equal(snapshot.last_good.loaded_at, loaded, "Activity must refresh without a source reload");
  for (const view of ["board", "list", "backlog"]) {
    ui.run(`navigate({ view: ${JSON.stringify(view)} })`);
    for (const selector of ["#content", "#issue-panel"]) {
      const html = ui.element(selector).innerHTML;
      assert.match(html, /いま: 試験 &lt;script&gt;（Python &amp; CJS）・<span class="activity-age">2分前<\/span>/, `${view}: ${selector}`);
      assert.doesNotMatch(html, /<script>|activity-stale/);
    }
  }
  const line = ui.element("#issue-panel").innerHTML.match(/<div class="activity-line" data-activity-heartbeat="([^"]+)"[^>]*>[\s\S]*?<\/div>/);
  assert.ok(line);
  const classes = new Set();
  const age = { textContent: "2分前" };
  liveLines.push({
    dataset: { activityHeartbeat: line[1] },
    classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } },
    querySelector: selector => { assert.equal(selector, ".activity-age"); return age; },
  });
  const timer = ui.intervals.find(interval => interval.delay === 1000);
  assert.ok(timer, "The existing second timer updates heartbeat age");
  const requests = ui.requests.length;
  for (const [elapsed, expected, stale] of [
    [180_000, "3分前", false], [600_000, "10分前", false],
    [600_001, "止まっている可能性（最終 10分前）", true],
  ]) {
    currentTime = Date.parse(heartbeat) + elapsed;
    timer.callback();
    assert.equal(age.textContent, expected);
    assert.equal(classes.has("activity-stale"), stale);
  }
  assert.equal(ui.requests.length, requests, "Aging must not reload or fetch the page");
  ui.run("render()");
  assert.match(ui.element("#content").innerHTML, /class="activity-line activity-stale"/);
  assert.match(ui.element("#issue-panel").innerHTML, /止まっている可能性（最終 10分前）/);
  entry.heartbeat_at = new Date(currentTime).toISOString();
  delete entry.detail;
  snapshot.activity_revision++;
  await refresh();
  assert.match(ui.element("#content").innerHTML, /いま: 試験 &lt;script&gt;・<span class="activity-age">0分前<\/span>/);
  assert.doesNotMatch(ui.element("#content").innerHTML, /activity-stale/);
  snapshot.activity = { count: 0, items: [], unmatched: [], errors: [{ file: "T1.json", message: "JSONの構文エラー" }] };
  snapshot.activity_revision++;
  ui.run('state.connection = "接続中"');
  await refresh();
  for (const selector of ["#content", "#issue-panel"]) assert.doesNotMatch(ui.element(selector).innerHTML, /activity-line/);
  assert.equal(ui.element("#error-banner").hidden, false);
  assert.match(ui.element("#error-banner").textContent, /First project.*実行状況の読み込み失敗.*T1.json.*JSONの構文エラー/);
  assert.equal(ui.element("#freshness").className, "freshness failed");
  snapshot.activity = { count: 1, items: [], errors: [], unmatched: [{ ...entry, card: "GONE", file: "GONE.json" }] };
  snapshot.activity_revision++;
  await refresh();
  assert.match(ui.element("#error-banner").textContent, /対応する課題のない実行状況.*GONE.json.*GONE.*試験 <script>/);
  assert.doesNotMatch(ui.element("#content").innerHTML, /activity-line/);
  snapshot.activity = { count: 0, items: [], errors: [], unmatched: [] };
  snapshot.activity_revision++;
  await refresh();
  assert.equal(ui.element("#error-banner").hidden, true);
  for (const view of ["board", "list", "backlog"]) {
    ui.run(`navigate({ view: ${JSON.stringify(view)} })`);
    for (const selector of ["#content", "#issue-panel"]) assert.doesNotMatch(ui.element(selector).innerHTML, /activity-line/);
  }
  snapshot.activity = { count: 1, items: [entry], errors: [], unmatched: [] };
  snapshot.activity_revision++;
  await refresh();
  ui.run('navigate({ projectId: "second" })');
  await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(ui.element("#content").innerHTML, /activity-line/, "Identical task ids in other projects must not inherit activity");
  console.log("PASS activity: board/list/backlog/detail, activity-only SSE, safe text, live age and ten-minute boundary, recovery/removal, file errors and unmatched ids");
}

// Run with `node tests/test_app.cjs layout`; PLAYWRIGHT_MODULE and
// PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH may point to an existing offline installation.
async function viewerActions(page, artifacts, width, theme, key) {
  const goto = async hash => {
    await page.goto(`http://127.0.0.1:8823/${hash}`);
    await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
  };
  for (const [view, sort, ids] of [
    ["board", "", ["UI-START", key, "UI-END"]],
    ["list", "&sort=status&direction=desc", ["UI-END", key, "UI-START"]],
  ]) {
    await goto(`#${view}?project=first${sort}`);
    await page.locator(`#content a[data-issue="${ids[0]}"]`).first().click();
    const previous = page.locator('#issue-panel [data-issue-step="-1"]');
    const next = page.locator('#issue-panel [data-issue-step="1"]');
    assert.equal(await previous.isDisabled(), true);
    assert.equal(await next.isDisabled(), false);
    const initial = page.url();
    await page.keyboard.press("k");
    assert.equal(page.url(), initial);
    await next.click();
    assert.equal(await page.evaluate(() => state.issue), ids[1]);
    assert.equal(new URL(page.url()).hash.includes(`issue=${ids[1]}`), true);
    await page.keyboard.press("j");
    assert.equal(await page.evaluate(() => state.issue), ids[2]);
    assert.equal(await next.isDisabled(), true);
    assert.equal(await previous.isDisabled(), false);
    const end = page.url();
    await page.keyboard.press("j");
    assert.equal(page.url(), end);
    await previous.click();
    assert.equal(await page.evaluate(() => state.issue), ids[1]);
    await page.keyboard.press("k");
    assert.equal(await page.evaluate(() => state.issue), ids[0]);
    assert.equal(await page.locator("#issue-panel").isVisible(), true);
  }
  await goto(`#board?project=first&issue=${key}`);
  await page.evaluate(() => { window.clipboardMode = "ok"; window.clipboardWrites = []; });
  await page.locator('[data-issue-copy="key"]').click();
  await page.waitForFunction(() => document.querySelector("#issue-copy-status").textContent === "コピーしました");
  assert.equal(await page.evaluate(() => window.clipboardWrites.at(-1)), key);
  await page.locator('[data-issue-copy="handoff"]').click();
  const handoff = await page.evaluate(() => window.clipboardWrites.at(-1));
  assert.ok(handoff.startsWith(`First project ${key}: `));
  assert.match(handoff, /\n状態: 進行中\n段階: P1 · Phase\n版: V1\n完了条件: 表示専用を保つ\n依存先: UI-END\nPR #6: マージ済み https:\/\/github.com\/owner\/repo\/pull\/6$/);
  await page.evaluate(() => { window.clipboardMode = "reject"; });
  await page.locator('[data-issue-copy="handoff"]').click();
  const textarea = page.locator("#issue-copy-text");
  await textarea.waitFor({ state: "visible" });
  assert.equal(await textarea.inputValue(), handoff);
  assert.equal(await textarea.getAttribute("readonly"), "");
  assert.equal(await page.locator("#issue-copy-status").getAttribute("aria-live"), "polite");
  assert.equal(await textarea.evaluate(element => document.activeElement === element && element.selectionStart === 0 && element.selectionEnd === element.value.length), true);
  assert.equal(await textarea.evaluate(element => element.scrollWidth <= element.clientWidth + 1), true, "The long handoff must wrap in the fallback textarea");
  for (const key of ["j", "k", "?"]) await page.keyboard.press(key);
  assert.equal(await page.evaluate(() => state.issue), key);
  assert.equal(await page.locator("#shortcuts-dialog").evaluate(dialog => dialog.open), false, "Typing in the fallback textarea must not run shortcuts");
  await page.screenshot({ path: path.join(artifacts, `viewer-copy-${width}-${theme}.png`), fullPage: true });
  await page.evaluate(() => { window.clipboardMode = "missing"; });
  await page.locator('[data-issue-copy="key"]').click();
  await page.waitForFunction(key => document.querySelector("#issue-copy-text").value === key, key);
  assert.equal(await textarea.inputValue(), key);
  if (width < 1200) {
    await page.evaluate(() => {
      const controls = [...document.querySelectorAll('#issue-panel a[href], #issue-panel button:not(:disabled), #issue-panel textarea')].filter(control => control.getClientRects().length);
      window.panelFocusOrder = controls;
      controls.at(-1).focus();
    });
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement === window.panelFocusOrder[0]), true);
    await page.keyboard.press("Shift+Tab");
    assert.equal(await page.evaluate(() => document.activeElement === window.panelFocusOrder.at(-1)), true);
  }
  await page.locator('[data-issue-copy="key"]').focus();
  await page.keyboard.press("?");
  const dialog = page.locator("#shortcuts-dialog");
  await dialog.waitFor({ state: "visible" });
  assert.equal(await page.locator("#shortcuts-close").evaluate(element => document.activeElement === element), true);
  for (const key of ["Tab", "Shift+Tab"]) {
    await page.keyboard.press(key);
    assert.equal(await page.locator("#shortcuts-close").evaluate(element => document.activeElement === element), true);
  }
  await page.keyboard.press("j");
  assert.equal(await page.evaluate(() => state.issue), key);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  for (const selector of [".issue-navigation", ".issue-copy-actions", "#issue-copy-text", "#shortcuts-dialog"]) {
    assert.equal(await page.locator(selector).evaluate(element => element.scrollWidth <= element.clientWidth + 1), true, `${width}px ${theme} ${selector} must not overflow`);
  }
  await page.screenshot({ path: path.join(artifacts, `viewer-shortcuts-${width}-${theme}.png`), fullPage: true });
  await page.keyboard.press("Escape");
  assert.equal(await dialog.evaluate(element => element.open), false);
  assert.equal(await page.locator('[data-issue-copy="key"]').evaluate(element => document.activeElement === element), true);
  assert.equal(await page.evaluate(() => state.issue), key);
  await page.keyboard.press("?");
  await page.evaluate(() => render());
  await page.locator("#shortcuts-close").click();
  assert.equal(await dialog.evaluate(element => element.open), false);
  assert.equal(await page.locator('[data-issue-copy="key"]').evaluate(element => document.activeElement === element), true, "Help returns focus after a live render replaces its opener");
  await page.keyboard.press("/");
  assert.equal(await page.locator("#global-search").evaluate(element => document.activeElement === element), true);
  assert.equal(await page.evaluate(() => state.issue), "");
  await page.keyboard.press("?");
  assert.equal(await dialog.evaluate(element => element.open), false, "Typing ? in global search must not open help");
  for (const view of ["board", "list"]) {
    await goto(`#${view}?project=first`);
    await page.locator("#filter-owner").check();
    assert.equal(new URLSearchParams(new URL(page.url()).hash.split("?")[1]).get("owner"), "1");
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
    assert.equal(await page.locator("#filter-owner").isChecked(), true);
    const tasks = page.locator(view === "board" ? "#content .board-card" : "#content .issue-table tbody tr");
    assert.equal(await tasks.count(), 2);
    await page.locator("#clear-filters").click();
    assert.equal(await page.locator("#filter-owner").isChecked(), false);
    assert.equal(new URLSearchParams(new URL(page.url()).hash.split("?")[1]).has("owner"), false);
    assert.equal(await tasks.count(), 3);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
}

async function collapsedIssueNavigation(page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const view of ["board", "timeline"]) {
    await page.goto(`http://127.0.0.1:8823/#${view}?project=first&group=phase`);
    await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
    await page.evaluate(view => {
      rows().phases = [1, 2, 3].map(number => ({ id: `P${number}`, title: `Phase ${number}`, order: number }));
      rows().tasks = [
        { id: "COLLAPSED-P1", title: "Closed first phase", status: "todo", phase: "P1" },
        { id: "VISIBLE-1", title: "Visible first", status: "todo", phase: "P2" },
        { id: "VISIBLE-2", title: "Visible last", status: "todo", phase: "P2" },
        { id: "COLLAPSED-P3", title: "Closed last phase", status: "todo", phase: "P3" },
      ];
      for (const number of [1, 2, 3]) state.disclosures.set(`${view === "board" ? "board:phase" : "phase"}:P${number}`, number === 2);
      render();
    }, view);
    await page.locator('#content a[data-issue="VISIBLE-1"]').first().click();
    const previous = page.locator('#issue-panel [data-issue-step="-1"]');
    const next = page.locator('#issue-panel [data-issue-step="1"]');
    assert.equal(await previous.isDisabled(), true, `Collapsed phases must not enter previous navigation (${view})`);
    await page.keyboard.press("k");
    assert.equal(await page.evaluate(() => state.issue), "VISIBLE-1");
    await next.click();
    assert.equal(await page.evaluate(() => state.issue), "VISIBLE-2");
    assert.equal(await next.isDisabled(), true, `Collapsed phases must not enter next navigation (${view})`);
    await page.keyboard.press("j");
    assert.equal(await page.evaluate(() => state.issue), "VISIBLE-2");
    await previous.click();
    const firstPhase = page.locator("#content details").first();
    await firstPhase.locator("summary").click();
    await page.waitForFunction(() => !document.querySelector('#issue-panel [data-issue-step="-1"]').disabled);
    await page.keyboard.press("k");
    assert.equal(await page.evaluate(() => state.issue), "COLLAPSED-P1", "Opening a phase restores its issues to navigation");
    await next.click();
    await firstPhase.locator("summary").click();
    await page.waitForFunction(() => document.querySelector('#issue-panel [data-issue-step="-1"]').disabled);
    assert.equal(await page.evaluate(() => {
      const first = document.querySelector('#content a[data-issue="VISIBLE-1"]');
      for (const mode of ["hidden", "display", "visibility"]) {
        const link = first.cloneNode(true);
        link.dataset.issue = `HIDDEN-${mode}`;
        if (mode === "hidden") link.hidden = true;
        else link.style[mode] = mode === "display" ? "none" : "hidden";
        first.before(link);
      }
      return issueNeighbors().previous;
    }), "", "Hidden and unrendered links must not enter navigation");
    assert.equal(await page.evaluate(() => {
      const details = document.querySelector("#content details");
      const link = details.querySelector("a[data-issue]").cloneNode(true);
      details.querySelector("summary").append(link);
      return issueNeighbors().previous;
    }), "COLLAPSED-P1", "An issue link in the summary remains rendered when its details is closed");
  }
  console.log("PASS collapsed navigation: board/timeline skip closed phases, restore opened phases and retain summary links");
}

async function shortcutFocusReturn(page, key) {
  await page.setViewportSize({ width: 375, height: 900 });
  const failures = [];
  const check = (condition, message) => { if (!condition) failures.push(message); };
  const focused = selector => page.locator(selector).first().evaluate(element => document.activeElement === element
    && element.isConnected && element.getClientRects().length > 0 && !element.closest("[hidden], [inert]") && !element.disabled);
  const goto = async () => {
    await page.goto("http://127.0.0.1:8823/#board?project=first");
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
  };
  await goto();
  await page.locator("#menu-toggle").click();
  const project = page.locator('#nav-projects a[href="#board?project=first"]');
  await project.focus();
  await page.keyboard.press("?");
  await page.evaluate(() => refreshOverview());
  await page.keyboard.press("Escape");
  check(await focused('#nav-projects a[href="#board?project=first"]'), "Sidebar refresh must restore focus to the replacement link in the same container");
  await page.keyboard.press("Tab");
  check(await page.evaluate(() => document.activeElement !== document.body && !document.activeElement.closest("#shortcuts-dialog")), "Tab must remain usable after sidebar refresh");

  await goto();
  await page.locator(`#content a[data-issue="${key}"]`).first().click();
  await page.locator('[data-issue-copy="key"]').focus();
  await page.keyboard.press("?");
  await page.goBack();
  await page.waitForFunction(() => state.issue === "");
  await page.keyboard.press("Escape");
  check(await focused("#content"), "Browser Back must reject the hidden copy opener and return focus to content");

  for (const selector of ['[data-issue-copy="handoff"]', '[data-issue-step="1"]']) {
    await goto();
    await page.locator('#content a[data-issue="UI-START"]').first().click();
    await page.locator(`#issue-panel ${selector}`).focus();
    await page.keyboard.press("?");
    await page.evaluate(() => render());
    await page.keyboard.press("Escape");
    check(await focused(`#issue-panel ${selector}`), `A live render must resolve the replacement ${selector}`);
  }
  await page.locator('#issue-panel [data-issue-step="1"]').focus();
  await page.keyboard.press("?");
  await page.evaluate(() => navigate({ issue: "UI-END" }));
  await page.keyboard.press("Escape");
  check(await focused("#issue-panel [data-close-issue]"), "A disabled replacement must fall back to the visible issue close button");

  await goto();
  await page.locator("#menu-toggle").click();
  await project.focus();
  await page.keyboard.press("?");
  await page.evaluate(() => {
    document.querySelector('#nav-recent').closest("details").open = true;
    document.querySelector('#nav-projects').replaceChildren();
  });
  await page.keyboard.press("Escape");
  check(await focused("#sidebar-toggle"), "A removed sidebar link must use the open sidebar toggle instead of another container's matching href");

  await goto();
  await page.locator("#menu-toggle").click();
  await project.focus();
  await page.keyboard.press("?");
  await page.evaluate(() => closeSidebar());
  await page.keyboard.press("Escape");
  check(await focused("#content"), "An inert sidebar opener must fall back to content");

  await page.locator("#content").focus();
  await page.keyboard.press("?");
  await page.evaluate(() => {
    const original = document.querySelector("#content");
    original.replaceWith(original.cloneNode(true));
  });
  await page.locator("#shortcuts-close").click();
  check(await focused("#content"), "A detached opener with an id must resolve its connected replacement");
  assert.deepEqual(failures, [], "Shortcut focus must resolve live visible targets after refresh and Back");
  console.log("PASS shortcut focus: mobile refresh/Back, same-container replacements and visible enabled fallback order");
}

async function stickyHeaderLayout(page) {
  await page.goto("http://127.0.0.1:8823/#board?project=first");
  await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
  await page.evaluate(() => {
    const title = '長いタイトル "quoted" <tag> & ' + "LongUnbrokenIdentifier".repeat(30);
    state.snapshot.last_good.rows.phases = [1, 2].map(number => ({ id: `P${number}`, title: "折り返すグループ名".repeat(8), order: number }));
    state.snapshot.last_good.rows.versions = [1, 2].map(number => ({ id: `V${number}`, title: "折り返す版の名前".repeat(8), order: number }));
    state.snapshot.last_good.rows.tasks = [1, 2].flatMap(number => statuses.flatMap(status => Array.from({ length: 20 }, (_, index) => ({
      id: `SCROLL-${number}-${status}-${index}`, title, status, phase: `P${number}`, version: `V${number}`,
    }))));
    state.view = "board";
    state.group = "none";
    render();
  });
  const settled = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const checkColumn = async selector => {
    await settled();
    await page.evaluate(selector => {
      const header = document.querySelector("#global-header").getBoundingClientRect();
      const column = document.querySelector(selector).getBoundingClientRect();
      window.scrollTo(0, scrollY + column.top - header.bottom + 400);
    }, selector);
    await settled();
    await page.evaluate(selector => {
      const header = document.querySelector("#global-header").getBoundingClientRect();
      const column = document.querySelector(selector);
      const heading = column.querySelector("h3").getBoundingClientRect();
      const summary = column.closest(".swimlane")?.querySelector(":scope > summary").getBoundingClientRect();
      const expected = summary ? summary.bottom : header.bottom;
      if (Math.abs(heading.top - expected) > 1 || heading.bottom > innerHeight) throw new Error(`Column heading must remain visible below its headers: ${heading.top} / ${expected}`);
      if (summary && Math.abs(summary.top - header.bottom) > 1) throw new Error("Group heading must remain below the global header");
      const measured = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--header-height"));
      if (Math.abs(measured - header.height) > 1) throw new Error("Header offset must match the rendered status bar plus navigation");
      const title = column.querySelector(".card-title");
      const style = getComputedStyle(title);
      if (style.webkitLineClamp !== "3" || title.clientHeight > parseFloat(style.lineHeight) * 3 + 1 || title.scrollHeight <= title.clientHeight) throw new Error("Long board titles must be clamped to three lines");
      if (title.closest("a").title !== title.textContent) throw new Error("Card tooltip must retain the full title");
    }, selector);
  };
  await checkColumn(".board-column");
  await page.locator("#freshness-message").evaluate(element => { element.textContent = "接続状態の長いお知らせ".repeat(15); });
  await checkColumn(".board-column");
  for (const group of ["phase", "version"]) {
    await page.evaluate(group => { window.scrollTo(0, 0); state.group = group; render(); }, group);
    await checkColumn(".swimlane:first-of-type .board-column");
    await checkColumn(".swimlane:last-of-type .board-column");
  }
  await page.evaluate(() => { window.scrollTo(0, 0); state.view = "list"; render(); });
  await settled();
  await page.evaluate(() => {
    const table = document.querySelector(".issue-table").getBoundingClientRect();
    window.scrollTo(0, scrollY + table.top - document.querySelector("#global-header").getBoundingClientRect().bottom + 400);
    document.querySelector(".table-box").scrollLeft = 120;
  });
  await settled();
  assert.equal(await page.evaluate(() => Math.abs(document.querySelector(".issue-table thead").getBoundingClientRect().top - document.querySelector("#global-header").getBoundingClientRect().bottom) <= 1), true, "List header must stay below the global header while its table scrolls horizontally");
  await page.evaluate(() => { window.scrollTo(0, 0); state.view = "board"; state.group = "none"; render(); });
  await page.locator(".card-link").first().click();
  assert.equal(await page.locator("#issue-heading").textContent(), await page.locator(".card-link").first().getAttribute("title"));
  assert.equal(await page.locator("#issue-heading").evaluate(element => getComputedStyle(element).webkitLineClamp), "none", "The detail title must not be clamped");
}

async function layout() {
  const http = require("node:http");
  const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  const root = path.resolve(__dirname, "..");
  const artifacts = path.join(root, "tmp", "layout-tests");
  fs.mkdirSync(artifacts, { recursive: true });
  const fixture = await app("#projects");
  const key = "UI-1234567890123"; // gitleaks:allow (dummy task id, not a secret)
  const longTitle = "長い課題のタイトルと連続した識別子LongUnbrokenIdentifier".repeat(8);
  fixture.snapshots.first.last_good.rows.tasks[0] = { id: key, title: longTitle, status: "doing", raw_status: "p-owner", phase: "P1", version: "V1", done_when: "表示専用を保つ", deps: "UI-END", blocked_by: "", ...linkedPR() };
  fixture.snapshots.first.last_good.rows.tasks[0].description_changed_at = new Date(Date.now() - 125 * 60_000).toISOString();
  fixture.snapshots.first.last_good.rows.tasks[0].description_baseline = true;
  fixture.snapshots.first.last_good.rows.tasks[0].label = "長いラベルLongUnbrokenIdentifier".repeat(8);
  fixture.snapshots.first.progress = { items: { [key]: { count: 44, total: 90, label: "結果ファイル", last_changed_at: new Date().toISOString() } }, errors: [] };
  fixture.snapshots.first.last_good.rows.tasks.unshift({ id: "UI-START", title: "最初の課題", status: "todo", phase: "P1", version: "V1", ball: true });
  fixture.snapshots.first.last_good.rows.tasks.push({ id: "UI-END", title: "最後の課題", status: "done", phase: "P1", version: "V1", ball: false });
  fixture.snapshots.second.last_good.rows.versions = [];
  const failedPR = linkedPR();
  failedPR.prs[0].status = "error";
  failedPR.prs[0].error = { message: "PR の状態を取得できません（GitHub の利用回数の上限に達しました）" };
  failedPR.pr_flags = [];
  fixture.snapshots.second.last_good.rows.tasks[0] = { id: key, title: longTitle, status: "blocked", phase: "P1", version: "", deps: "", blocked_by: "", ...failedPR };
  fixture.snapshots.second.last_good.rows.tasks[0].label = "長いラベルLongUnbrokenIdentifier".repeat(8);
  for (const project of fixture.summaries) {
    project.counts = { todo: 7890, doing: 7890, blocked: 7890, done: 7890 };
    project.total = 31560;
    project.owner_wait = 1234567;
    project.last_good_mtime = project.source_mtime;
    project.error = { message: "試験用の失敗", at: new Date().toISOString() };
    project.pr_summary = { linked_count: 1, mismatch_count: 1, ci_failure_count: 0, unknown_count: 1 };
  }
  const files = { "/": "index.html", "/static/app.js": "app.js", "/static/style.css": "style.css" };
  const sockets = new Set();
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push({ method: request.method, url: request.url });
    if (files[request.url]) {
      response.setHeader("Content-Type", ({ "/": "text/html", "/static/app.js": "text/javascript", "/static/style.css": "text/css" })[request.url] + "; charset=utf-8");
      return response.end(fs.readFileSync(path.join(root, "skills/live-board/scripts/static", files[request.url])));
    }
    if (request.url === "/events") {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.write(": connected\n\n");
      return;
    }
    const value = request.url === "/api/projects" ? fixture.run("state.projects") : request.url === "/api/summary" ? fixture.summaries : fixture.snapshots[request.url.split("/").pop()];
    response.writeHead(value ? 200 : 404, { "Content-Type": "application/json; charset=utf-8" });
    response.end(JSON.stringify(value || null));
  });
  server.on("connection", socket => { sockets.add(socket); socket.on("close", () => sockets.delete(socket)); });
  let browser;
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(8823, "127.0.0.1", resolve); });
    browser = await chromium.launch({
      headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
      args: ["--no-proxy-server", "--disable-background-networking", "--disable-component-update", "--disable-breakpad", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
    });
    const context = await browser.newContext({ serviceWorkers: "block", reducedMotion: "reduce" });
    await context.route("**/*", route => new URL(route.request().url()).origin === "http://127.0.0.1:8823" ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.clipboardWrites = [];
      window.clipboardMode = "ok";
      Object.defineProperty(navigator, "clipboard", { configurable: true, get() {
        return window.clipboardMode === "missing" ? undefined : { writeText(text) {
          window.clipboardWrites.push(text);
          return window.clipboardMode === "reject" ? Promise.reject(new Error("Clipboard denied in test")) : Promise.resolve();
        } };
      } });
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await collapsedIssueNavigation(page);
    await shortcutFocusReturn(page, key);
    for (const width of [375, 940, 1440]) for (const theme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme });
      for (const view of ["list?project=first", "list?project=second", "board?project=first", `board?project=second&issue=${key}`, "projects", "for-you?tab=doing"]) {
        await page.goto(`http://127.0.0.1:8823/#${view}`);
        await page.waitForFunction(() => document.querySelector("#content").getAttribute("aria-busy") === "false");
        const failures = await page.evaluate(() => {
          const failures = [];
          const check = (condition, message) => { if (!condition) failures.push(message); };
          const rect = element => { const range = document.createRange(); range.selectNodeContents(element); return range.getBoundingClientRect(); };
          check(document.documentElement.scrollWidth <= innerWidth, "Page must not overflow; wide tables scroll inside their container");
          for (const cell of document.querySelectorAll(".issue-table td:first-child, .issue-table td:nth-child(3), .project-counts, .project-waiting, .project-updated, .project-state")) {
            const style = getComputedStyle(cell), box = cell.getBoundingClientRect();
            if (!box.width) continue;
            const text = rect(cell);
            check(text.right <= box.right - parseFloat(style.paddingRight) + 1 && text.left >= box.left + parseFloat(style.paddingLeft) - 1, `Cell must contain all its data: ${cell.textContent}`);
            if (innerWidth >= 940) check(text.height <= 24, `Data must stay on one line: ${cell.textContent}`);
          }
          for (const row of document.querySelectorAll(".issue-table tbody tr")) {
            const key = row.querySelector(".issue-key"), title = row.querySelector(".issue-title");
            check(rect(key).right + 15 <= rect(title).left, "Issue key must leave the cell padding before the title");
          }
          for (const name of document.querySelectorAll(".project-name")) check(name.scrollWidth <= name.clientWidth, "Short project names must stay readable in the scrollable table");
          for (const badge of document.querySelectorAll(".directory-counts .lozenge, .project-waiting .lozenge")) {
            check(rect(badge).height <= 18, `Numbers must not wrap inside badges: ${badge.textContent}`);
          }
          for (const state of document.querySelectorAll("td.project-state")) check(state.textContent === "読み込み失敗", "Failed-load text must be preserved");
          for (const badge of document.querySelectorAll(".pr-badge")) check(badge.getAttribute("href") === "https://github.com/owner/repo/pull/6", "PR badges must retain their link");
          for (const meta of document.querySelectorAll(".pr-meta, .pr-entry, .pr-flag, .card-title, .label-badge, #issue-heading")) check(meta.scrollWidth <= meta.clientWidth + 1, `PR details, labels and long titles must wrap: ${meta.className}`);
          return failures;
        });
        await page.screenshot({ path: path.join(artifacts, `${view.replace(/[?=]/g, "-")}-${width}-${theme}.png`), fullPage: true });
        assert.deepEqual(failures, [], `${width}px ${theme} ${view}`);
      }
      await viewerActions(page, artifacts, width, theme, key);
    }
    for (const width of [375, 599, 600, 899, 900, 940, 1440]) for (const theme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme });
      await stickyHeaderLayout(page);
    }
    assert.deepEqual(errors, []);
    assert.ok(requests.every(request => request.method === "GET" && (Object.hasOwn(files, request.url) || ["/events", "/api/projects", "/api/summary", "/healthz", "/api/projects/first", "/api/projects/second"].includes(request.url))), "Viewer actions must use only existing read endpoints");
    console.log("PASS layout: long data, copy/fallback, view-order navigation, owner route/clear, shortcut focus and no overflow at 375/940/1440px in both themes");
    console.log("PASS sticky layout: long boards, phase/version groups, live header resizing, list headings and full title details at 375/599/600/899/900/940/1440px in both themes");
  } finally {
    await browser?.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  }
}

async function stickyOffsets() {
  const properties = () => ({ values: new Map(), setProperty(name, value) { this.values.set(name, value); } });
  let groupHeight = 72, tableTop = 300, tableHeight = 1000;
  const summary = { parentElement: { style: properties() }, getBoundingClientRect: () => ({ height: groupHeight }) };
  const table = { style: properties(), getBoundingClientRect: () => ({ top: tableTop, height: tableHeight }), tHead: { getBoundingClientRect: () => ({ height: 44 }) } };
  const observers = [];
  class ResizeObserver {
    constructor(callback) { this.callback = callback; this.targets = new Set(); observers.push(this); }
    disconnect() { this.targets.clear(); }
    observe(element) { this.targets.add(element); }
  }
  const ui = await app("#board?project=first", {}, { ResizeObserver, querySelectorAll(selector) {
    return selector === ".swimlane > summary" ? [summary] : selector === ".table-box table" ? [table]
      : selector === ".swimlane > summary, .table-box table" ? [summary, table] : [];
  } });
  const rootStyle = ui.element("html").style.values;
  const header = ui.element("#global-header");
  assert.equal(rootStyle.get("--header-height"), "96px");
  header.getBoundingClientRect = () => ({ height: 188, bottom: 188 });
  observers[0].callback();
  assert.equal(rootStyle.get("--header-height"), "188px", "Wrapped mobile status/header height must replace the desktop offset");
  assert.equal(summary.parentElement.style.values.get("--swimlane-header-height"), "72px");
  assert.equal(table.style.values.get("--table-header-offset"), "0px", "A table below the global header must stay in its original position");
  tableTop = -200;
  ui.listeners.scroll();
  assert.equal(table.style.values.get("--table-header-offset"), "388px", "Table heading must follow the global header during page scroll");
  tableTop = -2000;
  ui.listeners.scroll();
  assert.equal(table.style.values.get("--table-header-offset"), "956px", "Table heading must stop at the table's last row");
  groupHeight = 108;
  tableHeight = 44;
  observers[0].callback();
  assert.equal(summary.parentElement.style.values.get("--swimlane-header-height"), "108px", "Wrapped group names need their own measured offset");
  assert.equal(table.style.values.get("--table-header-offset"), "0px", "An empty table must not move its heading outside the table");
  ui.run("render()");
  assert.equal(observers[0].targets.size, 3, "Rerender must replace observed nodes rather than accumulate them");
  assert.ok(observers[0].targets.has(header));
  console.log("PASS sticky offsets: measured desktop/mobile and group heights, page-scroll table bounds and observer replacement");
}

async function presentation() {
  const ui = await app("#board?project=first&issue=T1");
  const html = () => ui.element("#content").innerHTML;
  const title = '長い題 "quoted" <tag> & \'single\' ' + "LongUnbrokenIdentifier".repeat(30);
  const escapedTitle = title.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  ui.run(`state.snapshot.last_good.rows.tasks[0].title = ${JSON.stringify(title)}; render()`);
  const cardLink = html().match(/<a class="card-link"[^>]*>/)[0];
  assert.ok(cardLink.includes(`title="${escapedTitle}"`), "Board link must retain the full, escaped title");
  assert.doesNotMatch(cardLink, /<tag>|title="[^"]*"quoted"/);
  assert.ok(ui.element("#issue-panel").innerHTML.includes(`<h2 id="issue-heading">${escapedTitle}</h2>`), "Details must keep the complete title");
  ui.run('state.snapshot.last_good.rows.tasks[0].title = "First task"; render()');
  assert.equal(ui.element("#version-filter").hidden, false);
  assert.match(html(), /class="version-badge">V1</);
  assert.match(html(), /<option value="version"/);
  assert.match(ui.element("#issue-panel").innerHTML, /<dt>版<\/dt>/);
  for (const [view, title] of Object.entries({ summary: "概要", timeline: "タイムライン", backlog: "バックログ", board: "ボード", list: "一覧", releases: "リリース" })) {
    ui.run(`navigate({ view: ${JSON.stringify(view)}, issue: "" })`);
    assert.equal(ui.run("document.title"), `${title} · First project · live-board`);
  }
  ui.run('navigate({ view: "list" })');
  assert.match(html(), /data-sort="version"/);
  assert.match(html(), /data-label="版"/);
  ui.run('state.snapshot.last_good.rows.versions = []; state.snapshot.last_good.rows.tasks[0].version = ""; render()');
  assert.equal(ui.element("#version-filter").hidden, true);
  assert.doesNotMatch(html(), /data-sort="version"|data-label="版"|version-badge/);
  ui.run('navigate({ view: "board", issue: "T1", group: "version" })');
  assert.doesNotMatch(html(), /version-badge|版なし|<option value="version"/);
  assert.match(html(), /First task/);
  assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /<dt>版<\/dt>|version-badge/);
  ui.run('navigate({ view: "summary", issue: "" })');
  assert.doesNotMatch(html(), /現在の版/);
  for (const [value, expected] of [["2026-10-31T14:24:33", "2026/10/31 14:24"], ["2026-01-02T03:04:05", "2026/01/02 03:04"], ["invalid", "—"]]) {
    ui.run(`state.snapshot.last_good.meta.updated = ${JSON.stringify(value)}; render()`);
    assert.ok(ui.element("#project-meta").textContent.includes(`正本の更新日 ${expected}`));
  }
  ui.run('state.snapshot.last_good.rows.versions = [{ id: "V1", title: "Version", order: 1 }]; navigate({ view: "board", group: "none" })');
  assert.equal(ui.element("#version-filter").hidden, false);
  assert.match(html(), /class="version-badge">版なし</, "Unversioned tasks still have a badge in versioned projects");
  for (const [view, title] of [["projects", "プロジェクト"], ["for-you", "あなた向け"]]) {
    ui.run(`navigate({ view: ${JSON.stringify(view)} })`);
    assert.equal(ui.run("document.title"), `${title} · live-board`);
  }
  console.log("PASS presentation: full escaped board title and detail title; version controls/badges/columns/details, page titles and metadata verified");
}

async function compactDirectory() {
  const ui = await app("#projects");
  const html = () => ui.element("#content").innerHTML;
  const time = html().match(/<time datetime="([^"]+)"[^>]*>([^<]+)<\/time>/);
  assert.equal(time[2], ui.run(`relativeTime(${JSON.stringify(time[1])})`));
  assert.match(time[0], /title="[^"]+"/);
  assert.match(html(), /class="project-name-meta note">second · wbs-cards<\/span>/);
  assert.match(html(), /class="status-counts directory-counts"/);
  for (const label of ["未着手 0", "進行中 0", "待ち 1", "済み 7"]) assert.ok(html().includes(`aria-label="${label}"`));
  ui.summaries[1].error = { message: "broken <source&>", at: "2026-10-31T01:00:00Z" };
  await ui.events.project({ data: '"second"' });
  assert.match(html(), /class="project-error" title="[^"]*broken &lt;source&amp;&gt;[^"]*最後に読めた時刻:[^"]*最終確認[^"]*">読み込み失敗<\/span>/);
  assert.doesNotMatch(html(), /<p class="project-error"/);
  console.log("PASS compact directory: relative update with absolute tooltip, inline labeled counts, compact errors retain escaped details and last-good times");
}

async function visibleLabels() {
  const task = { id: "A", title: "試験", label: "v0.2.1", status: "doing", phase: "P1", version: "" };
  const snapshot = { last_good: { meta: {}, rows: { tasks: [task], phases: [], versions: [] } }, error: null };
  const ui = await app("#board?project=first", { first: snapshot });
  for (const render of ["boardCard(task)", "taskList([task])", "renderList()", 'renderForYou()']) {
    for (const label of ["v0.2.1", '<img src=x onerror="alert(1)">&\'', "長いラベル".repeat(100), "", undefined]) {
      ui.run('state.homeTab = "doing"');
      ui.run(`state.snapshot.last_good.rows.tasks[0].label = ${JSON.stringify(label)}`);
      const html = ui.run(`(() => { const task = state.snapshot.last_good.rows.tasks[0]; return ${render}; })()`);
      if (label) {
        const escaped = label.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
        assert.ok(html.includes(`<span class="label-badge">${escaped}</span>`), render);
        assert.doesNotMatch(html, /<img/);
      } else {
        assert.doesNotMatch(html, /class="label-badge"/, render);
      }
    }
  }
  console.log("PASS visible labels: board, task rows, table and home render escaped badges, omit empty labels and retain long labels");
}

async function descriptionProgress() {
  const task = { id: "A", title: "試験", status: "doing", phase: "P1", version: "", description_changed_at: new Date(Date.now() - 125 * 60_000).toISOString(), description_baseline: true };
  const snapshot = { last_good: { loaded_at: "2026-11-04T00:00:00Z", meta: {}, rows: { tasks: [task], phases: [{ id: "P1", title: "段階", order: 1 }], versions: [] } }, error: null,
    progress: { items: { A: { count: 44, total: 90, label: "結果ファイル", last_changed_at: new Date(Date.now() - 3 * 60_000).toISOString() } }, errors: [] }, progress_revision: 1 };
  const ui = await app("#board?project=first&issue=A", { first: snapshot });
  for (const view of ["board", "list", "backlog", "summary"]) {
    ui.run(`navigate({ view: ${JSON.stringify(view)} })`);
    assert.match(ui.element("#content").innerHTML, /記述の更新 2時間5分以上前/);
    assert.match(ui.element("#content").innerHTML.replace(/<[^>]*>/g, ""), /進捗 44 \/ 90（結果ファイル）・最終更新 3分前/);
    assert.match(ui.element("#issue-panel").innerHTML, /記述の更新 2時間5分以上前/);
  }
  assert.equal(ui.element("#description-time").textContent, "進行中カードの記述 最古 2時間5分以上前");
  ui.run('state.snapshot.last_good.rows.tasks[0].description_changed_at = null; render(); renderFreshness()');
  assert.doesNotMatch(ui.element("#content").innerHTML, /記述の更新|data-description-changed/);
  assert.doesNotMatch(ui.element("#issue-panel").innerHTML, /記述の更新|data-description-changed/);
  assert.equal(ui.element("#description-time").textContent, "進行中カードの記述 更新時刻不明");
  ui.run('state.snapshot.last_good.rows.tasks[0].description_changed_at = new Date(Date.now() - 125 * 60_000).toISOString(); render(); renderFreshness()');
  assert.match(ui.element("#file-time").textContent, /^正本ファイルの更新 /);
  ui.run('state.connection = "接続中"; state.snapshot.checked_at = new Date().toISOString(); renderFreshness()');
  assert.equal(ui.element("#freshness").className, "freshness healthy", "Description age alone must not warn or change color");
  assert.match(ui.element("#freshness-message").textContent, /正本ファイルの変更を監視中/);
  ui.run('state.snapshot.history_warning = "記述履歴を保存できません"; renderFreshness()');
  assert.match(ui.element("#error-banner").textContent, /記述履歴を保存できません/);
  ui.run('state.snapshot.history_warning = null');
  ui.run('navigate({ view: "for-you", homeTab: "doing" })');
  assert.match(ui.element("#content").innerHTML, /記述の更新 2時間5分以上前/);
  assert.match(ui.element("#content").innerHTML, /進捗 44 \/ 90/);
  ui.run('navigate({ view: "board", projectId: "first", issue: "A" })');
  ui.snapshots.first.progress.items.A.count = 45;
  ui.snapshots.first.progress_revision++;
  await ui.events.project({ data: '"first"' });
  assert.match(ui.element("#content").innerHTML, /進捗 45 \/ 90/);
  ui.run('state.snapshot.progress = { items: {}, errors: [{ card: "A", message: "<読込失敗>" }] }; render()');
  assert.match(ui.element("#content").innerHTML, /進捗を取得できません: &lt;読込失敗&gt;/);
  ui.run('state.snapshot.last_good.rows.tasks[0].status = "todo"; render(); renderFreshness()');
  assert.doesNotMatch(ui.element("#content").innerHTML, /記述の更新/);
  assert.match(ui.element("#issue-panel").innerHTML, /記述の更新/);
  assert.equal(ui.element("#description-time").textContent, "進行中カードなし");
  assert.equal(ui.run('elapsedAge(new Date(Date.now() - 2 * 86400_000).toISOString())'), "2日");
  assert.equal(ui.run('elapsedAge(new Date(Date.now() - 23 * 60_000).toISOString())'), "23分");
  ui.run('state.snapshot.progress = { items: { A: { count: 0 } }, errors: [] }; render()');
  assert.match(ui.element("#content").innerHTML, /進捗 0<\/div>/);
  assert.doesNotMatch(ui.element("#content").innerHTML, /最終更新/);
  const ticking = { dataset: { descriptionChanged: new Date(Date.now() - 24 * 60_000).toISOString(), descriptionBaseline: "false" } };
  const progressTick = { dataset: { progressChanged: new Date(Date.now() - 4 * 60_000).toISOString() } };
  const live = await app("#board?project=first", {}, { querySelectorAll: selector => selector === "[data-description-changed]" ? [ticking] : selector === "[data-progress-changed]" ? [progressTick] : [] });
  live.intervals.find(item => item.delay === 1000).callback();
  assert.equal(ticking.textContent, "記述の更新 24分前");
  assert.equal(progressTick.textContent, "4分前");
  console.log("PASS description/progress: view coverage, baseline and live age, oldest doing, progress-only refresh, safe errors and zero count");
}

async function main() {
  const selection = process.argv[2] || "all";
  assert.ok(["all", "filters", "owner", "issue-navigation", "clipboard-verbatim", "issue-copy-performance", "issue-copy", "shortcuts", "projects", "directory", "wbs", "unknown-statuses", "layout", "presentation", "compact-directory", "navigation", "search-click", "cross-project-errors", "directory-times", "project-dates", "pr", "activity"].includes(selection), `Unknown test selection: ${selection}`);
  if (selection === "layout") await layout();
  if (selection === "all") await codeReload();
  if (selection === "all") await stickyOffsets();
  if (selection === "all") await visibleLabels();
  if (selection === "all") await descriptionProgress();
  if (selection === "all" || selection === "activity") await activity();
  if (selection === "all" || selection === "unknown-statuses") await unknownStatuses();
  if (selection === "all" || selection === "compact-directory") await compactDirectory();
  if (selection === "all" || selection === "presentation") await presentation();
  if (selection === "all" || selection === "search-click") await searchClicks();
  if (selection === "all" || selection === "cross-project-errors") await crossProjectErrors();
  if (selection === "all" || selection === "directory-times") await directoryTimes();
  if (selection === "all" || selection === "project-dates") await projectDates();
  if (selection === "all" || selection === "navigation") await navigation();
  if (selection === "all" || selection === "filters") await filters();
  if (selection === "all" || selection === "owner") await ownerFilters();
  if (selection === "all" || selection === "issue-navigation") await issueNavigation();
  if (selection === "all" || selection === "clipboard-verbatim") await clipboardVerbatim();
  if (selection === "all" || selection === "issue-copy-performance") await issueCopyPerformance();
  if (selection === "all" || selection === "issue-copy") await issueCopy();
  if (selection === "all" || selection === "shortcuts") await shortcuts();
  if (selection === "all" || selection === "projects") await projects();
  if (selection === "all" || selection === "directory") await directory();
  if (selection === "all" || selection === "wbs") await wbs();
  if (selection === "all" || selection === "wbs") await styleWarnings();
  if (selection === "all" || selection === "pr") await pullRequests();
  console.log(`PASS app tests (${selection})`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
