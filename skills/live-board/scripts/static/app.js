"use strict";

const labels = { todo: "未着手", doing: "進行中", blocked: "待ち", done: "済み" };
const sourceLabels = { "p-run": labels.doing, "p-hold": labels.todo, "p-wait": labels.blocked, "p-owner": "自分待ち", "p-sora": "自分待ち" };
const statusLabel = (status, table = labels) => Object.hasOwn(table, status) ? table[status] : `不明（${status}）`;
const statuses = Object.keys(labels);
const viewNames = { "for-you": "あなた向け", projects: "プロジェクト", summary: "概要", timeline: "タイムライン", backlog: "バックログ", board: "ボード", list: "一覧", releases: "リリース" };
const views = {
  "for-you": renderForYou,
  projects: renderProjects,
  summary: renderSummary,
  timeline: renderTimeline,
  backlog: renderBacklog,
  board: renderBoard,
  list: renderList,
  releases: renderReleases,
};
const sortKeys = ["id", "title", "status", "phase", "version", "deps", "blocked_by"];
const routeDefaults = { view: "summary", projectId: "", q: "", status: "", phase: "", version: "", ready: false, owner: false, group: "none", issue: "", sort: "id", direction: "asc", type: "", page: 1, homeTab: "waiting" };
const viewer = readViewer();
const state = {
  ...parseHash(),
  projects: [], projectSummaries: null, summaryError: "", summaryRequestNumber: 0, catalogRequestNumber: 0,
  snapshot: null, fetchError: "", routeNotice: "", connection: "接続を準備中",
  starting: false, sidebarCollapsed: false, sidebarOpen: false, disclosures: new Map(), detailOpener: null, copyFeedback: null, shortcutsOpener: null,
  starred: viewer.starred, recents: viewer.recents, visitedProject: "", pendingIssueVisit: false,
  projectSnapshots: new Map(), projectRequests: new Map(), projectErrors: new Map(),
  searchQuery: "", searchOpen: false, searchIndex: 0, searchItems: [],
};
const globalView = (view = state.view) => view === "projects" || view === "for-you";
const $ = (selector) => document.querySelector(selector);
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const count = (value) => new Intl.NumberFormat("ja-JP").format(value);
const empty = (message = "該当する課題はありません。") => `<p class="empty">${escape(message)}</p>`;
const lozenge = (status) => `<span class="lozenge s-${escape(status)}">${escape(statusLabel(status))}</span>`;
const versionBadge = (version) => rows().versions.length ? `<span class="version-badge">${escape(version || "版なし")}</span>` : "";
const labelBadge = (task) => task.label ? `<span class="label-badge">${escape(task.label)}</span>` : "";
const blockMark = (task) => task.blocked_by ? '<span class="block-mark">ブロック中</span>' : "";
const ownerStatus = (status) => status === "p-owner" || status === "p-sora";
const ownerWaiting = (task) => ownerStatus(task.raw_status) || task.ball === true;
const ownerMark = (task) => ownerWaiting(task) ? '<span class="block-mark">自分待ち</span>' : "";
const splitKeys = (keys) => String(keys || "").split(/,\s*/).filter(Boolean);
const panel = (title, body, className = "") => `<section class="panel ${className}"><h2>${escape(title)}</h2>${body}</section>`;

function readViewer() {
  try {
    const value = JSON.parse(localStorage.getItem("live-board.viewer"));
    return {
      starred: [...new Set((Array.isArray(value?.starred) ? value.starred : []).filter((id) => typeof id === "string"))],
      recents: (Array.isArray(value?.recents) ? value.recents : []).filter((item) => item && typeof item.projectId === "string" && typeof item.issue === "string"),
    };
  } catch {
    return { starred: [], recents: [] };
  }
}

function saveViewer() {
  try {
    localStorage.setItem("live-board.viewer", JSON.stringify({ starred: state.starred, recents: state.recents }));
  } catch { /* Viewer conveniences also work in memory when storage is unavailable. */ }
}

function recordRecent(projectId, issue = "") {
  state.recents = [{ projectId, issue }, ...state.recents.filter((item) => item.projectId !== projectId || item.issue !== issue)];
  saveViewer();
}

function toggleStar(projectId) {
  if (!state.projects.some((project) => project.id === projectId)) return;
  state.starred = state.starred.includes(projectId) ? state.starred.filter((id) => id !== projectId) : [...state.starred, projectId];
  saveViewer();
  render();
}

function projectHash(projectId, issue = "") {
  return hashFor({ ...routeDefaults, view: "board", projectId, issue });
}

function starButton(projectId) {
  const starred = state.starred.includes(projectId);
  const name = state.projects.find((project) => project.id === projectId)?.name || projectId;
  return `<button type="button" class="star-button ${starred ? "is-starred" : ""}" data-star-project="${escape(projectId)}" aria-pressed="${starred}" aria-label="${escape(name)}${starred ? "のスターを外す" : "にスターを付ける"}" title="${starred ? "スターを外す" : "スターを付ける"}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/></svg></button>`;
}

function projectIcon(project) {
  return `<span class="project-icon" aria-hidden="true">${escape(Array.from(project.name)[0] || "P")}</span>`;
}

function parseHash(hash = location.hash) {
  const [requestedView, query = ""] = hash.replace(/^#/, "").split("?");
  const view = Object.hasOwn(views, requestedView) ? requestedView : routeDefaults.view;
  const params = new URLSearchParams(query);
  if (view === "projects") return {
    ...routeDefaults, view, q: params.get("q") || "",
    type: params.get("type") || "", page: Math.max(1, Number.parseInt(params.get("page"), 10) || 1),
    sort: params.get("sort") === "name" ? "name" : "updated",
    direction: ["asc", "desc"].includes(params.get("direction")) ? params.get("direction") : params.get("sort") === "name" ? "asc" : "desc",
  };
  if (view === "for-you") return { ...routeDefaults, view, homeTab: ["waiting", "doing", "starred", "recent"].includes(params.get("tab")) ? params.get("tab") : "waiting" };
  return {
    ...routeDefaults,
    view,
    projectId: params.get("project") || "",
    q: params.get("q") || "", status: params.get("status") || "",
    phase: params.get("phase") || "", version: params.get("version") || "", ready: params.get("ready") === "1", owner: params.get("owner") === "1" || params.get("sora") === "1",
    group: ["none", "phase", "version"].includes(params.get("group")) ? params.get("group") : "none",
    issue: params.get("issue") || params.get("task") || "",
    sort: sortKeys.includes(params.get("sort")) ? params.get("sort") : "id",
    direction: params.get("direction") === "desc" ? "desc" : "asc",
  };
}

function landingRoute(hash, projectCount) {
  return parseHash(!hash || hash === "#" ? projectCount > 1 ? "#for-you" : "#summary" : hash);
}

function hashFor(patch = {}) {
  const route = { ...state, ...patch };
  const params = new URLSearchParams();
  const defaults = route.view === "projects" ? { ...routeDefaults, sort: "updated", direction: "desc" } : routeDefaults;
  for (const [key, fallback] of Object.entries(defaults)) {
    if (route.view === "projects" && !["q", "sort", "direction", "type", "page"].includes(key)) continue;
    if (route.view === "for-you" && key !== "homeTab") continue;
    if (!globalView(route.view) && ["type", "page", "homeTab"].includes(key)) continue;
    if (key === "view" || route[key] === fallback) continue;
    params.set(key === "projectId" ? "project" : key === "homeTab" ? "tab" : key, key === "ready" || key === "owner" ? "1" : route[key]);
  }
  return `#${route.view}${params.size ? `?${params}` : ""}`;
}

function applyRoute(route, replace = false) {
  const first = state.projects[0];
  let notice = "";
  if (!globalView(route.view) && first && !state.projects.some((project) => project.id === route.projectId)) {
    if (route.projectId) {
      notice = `プロジェクト「${route.projectId}」が見つからないため、先頭の「${first.name}」に戻りました。課題の選択を解除しました。`;
      route = { ...route, issue: "" };
    }
    route = { ...route, projectId: first.id };
  }
  if (first && route.projectId === state.projectId && route.issue && state.snapshot?.last_good
    && !rows().tasks.some((task) => task.id === route.issue)) {
    notice = `課題「${route.issue}」が見つからないため、先頭のプロジェクト「${first.name}」に戻りました。課題の選択を解除しました。`;
    route = { ...route, projectId: first.id, issue: "" };
  }
  const projectChanged = route.projectId !== state.projectId;
  const issueChanged = route.issue !== state.issue;
  if (projectChanged) {
    state.snapshot = null;
    state.fetchError = "";
    state.disclosures.clear();
    state.detailOpener = null;
  }
  Object.assign(state, route);
  if (!globalView() && state.projectId) {
    if (state.visitedProject !== state.projectId) recordRecent(state.projectId);
    state.pendingIssueVisit = Boolean(state.issue) && (state.pendingIssueVisit || issueChanged || state.visitedProject !== state.projectId);
    state.visitedProject = state.projectId;
  } else {
    state.visitedProject = "";
    state.pendingIssueVisit = false;
  }
  state.routeNotice = notice;
  const hash = hashFor();
  if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
  render();
  renderFreshness();
  return projectChanged;
}

function navigate(patch, replace = false) {
  const previousView = state.view;
  const changed = applyRoute(parseHash(hashFor(patch)), replace);
  closeSidebar();
  if (globalView()) {
    if (previousView !== state.view) refreshOverview();
  } else if (changed || !state.snapshot) refresh();
}

function issueLink(task, className = "issue-link") {
  return `<a class="${className}" href="${escape(hashFor({ issue: task.id }))}" data-issue="${escape(task.id)}"><span class="issue-key">${escape(task.id)}</span><span class="issue-title">${escape(task.title)}</span></a>`;
}

function dependencyLinks(keys) {
  return splitKeys(keys).map((id) => `<a href="${escape(hashFor({ issue: id }))}" data-issue="${escape(id)}">${escape(id)}</a>`).join(", ") || "—";
}

function rows() {
  return state.snapshot?.last_good?.rows || { tasks: [], phases: [], versions: [] };
}

function ordered(items) {
  return [...items].sort((a, b) => Number(a.order) - Number(b.order));
}

function phaseTitle(id) {
  const phase = rows().phases.find((item) => item.id === id);
  return phase && phase.title !== phase.id ? `${phase.id} · ${phase.title}` : id;
}

function matchesFilters(task) {
  const needle = state.q.trim().toLocaleLowerCase("ja");
  return (!state.status || task.status === state.status)
    && (!state.phase || task.phase === state.phase)
    && (!state.version || task.version === (state.version === "__none__" ? "" : state.version))
    && (!state.ready || task.ready === "yes")
    && (!state.owner || ownerWaiting(task))
    && (!needle || `${task.id} ${task.title} ${task.done_when} ${task.phase} ${task.version}`.toLocaleLowerCase("ja").includes(needle));
}

function filteredTasks() {
  return rows().tasks.filter(matchesFilters);
}

function hasFilters() {
  return Boolean(state.q || state.status || state.phase || state.version || state.ready || state.owner);
}

function hiddenDone() {
  return hasFilters() ? 0 : state.snapshot?.last_good?.done_hidden || 0;
}

function progress(tasks, label, hidden = 0) {
  return progressCounts(tasks.filter((task) => task.status === "done").length + hidden, tasks.length + hidden, label);
}

function progressCounts(done, total, label, compact = false) {
  const percentage = total ? Math.round(done / total * 100) : 0;
  return `<div class="progress${compact ? " compact-progress" : ""}" title="${done} / ${total} 済み"><progress max="100" value="${percentage}" aria-label="${escape(label)}の完了率">${percentage}%</progress><span>${compact ? "" : `${done} / ${total} 済み `}<strong>${percentage}%</strong></span></div>`;
}

function statusCounts(tasks, hidden = 0) {
  return countLozenges(Object.fromEntries(statuses.map((status) => [status, tasks.filter((task) => task.status === status).length + (status === "done" ? hidden : 0)])));
}

function countLozenges(counts, compact = false) {
  return `<span class="status-counts${compact ? " directory-counts" : ""}">${statuses.map((status) => `<span class="lozenge s-${status}" title="${labels[status]} ${count(counts[status])}" aria-label="${labels[status]} ${count(counts[status])}">${compact ? "" : `${labels[status]} `}${count(counts[status])}</span>`).join("")}</span>`;
}

function taskList(tasks) {
  return tasks.length ? `<ul class="task-list">${tasks.map((task) => `<li class="issue-row ${task.id === state.issue ? "selected" : ""}"><div>${issueLink(task)}${labelBadge(task)}${descriptionLine(task)}${progressLine(task)}${activityLine(task)}${prDetails(task)}</div><span class="row-state">${blockMark(task)}${ownerMark(task)}${lozenge(task.status)}</span><span class="row-phase">${escape(phaseTitle(task.phase))}</span></li>`).join("")}</ul>` : empty();
}

function activityAge(heartbeat) {
  const elapsed = Math.max(0, Date.now() - Date.parse(heartbeat));
  const minutes = count(Math.floor(elapsed / 60_000));
  const stale = elapsed > 600_000;
  return { stale, text: stale ? `止まっている可能性（最終 ${minutes}分前）` : `${minutes}分前` };
}

function elapsedAge(value) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60_000));
  if (!Number.isFinite(minutes)) return "不明";
  if (minutes < 60) return `${minutes}分`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}時間${minutes % 60}分`;
  return `${Math.floor(minutes / 1440)}日`;
}

function descriptionText(value, baseline) {
  return `記述の更新 ${elapsedAge(value)}${baseline ? "以上" : ""}前`;
}

function descriptionLine(task, detail = false) {
  if ((!detail && task.status !== "doing") || !task.description_changed_at) return "";
  return `<div class="description-age" data-description-changed="${escape(task.description_changed_at)}" data-description-baseline="${Boolean(task.description_baseline)}">${descriptionText(task.description_changed_at, task.description_baseline)}</div>`;
}

function progressLine(task, snapshot = state.snapshot) {
  const progress = snapshot?.progress;
  const error = progress?.errors?.find(item => item.card === task.id);
  if (error) return `<div class="progress-line progress-error">進捗を取得できません: ${escape(error.message)}</div>`;
  const item = progress?.items?.[task.id];
  if (!item) return "";
  return `<div class="progress-line">進捗 ${escape(item.count)}${item.total != null ? ` / ${escape(item.total)}` : ""}${item.label ? `（${escape(item.label)}）` : ""}${item.count > 0 && item.last_changed_at ? `・最終更新 <span data-progress-changed="${escape(item.last_changed_at)}">${elapsedAge(item.last_changed_at)}前</span>` : ""}</div>`;
}

function activityLine(task) {
  const activity = state.snapshot?.activity?.items.find(item => item.card === task.id);
  if (!activity) return "";
  const age = activityAge(activity.heartbeat_at);
  return `<div class="activity-line${age.stale ? " activity-stale" : ""}" data-activity-heartbeat="${escape(activity.heartbeat_at)}" title="最終通知 ${escape(formatTime(activity.heartbeat_at, true))}">いま: ${escape(activity.step)}${activity.detail ? `（${escape(activity.detail)}）` : ""}・<span class="activity-age">${escape(age.text)}</span></div>`;
}

function prFlags(task) {
  return (task.pr_flags || []).map(flag => `<span class="pr-flag" data-pr-flag="${escape(flag.kind)}">${escape(flag.message)}</span>`).join("");
}

function prAge(value) {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  return Number.isFinite(seconds) ? `前回の取得から ${count(seconds)} 秒` : "前回の取得時刻は未確認";
}

function prStateLabel(pr) {
  const states = { OPEN: "公開中", MERGED: "マージ済み", CLOSED: "終了" };
  const data = pr.data;
  const label = data ? data.is_draft && data.state === "OPEN" ? "下書き" : Object.hasOwn(states, data.state) ? states[data.state] : "状態未確認" : "状態未確認";
  return `${pr.status !== "ok" && data ? "前回: " : ""}${label}`;
}

function prDetails(task, detailed = false) {
  if (!task.prs?.length) return "";
  const ciLabels = { success: "CI 成功", failure: "CI 失敗", pending: "CI 実行待ち・実行中", none: "CI なし" };
  const entries = task.prs.map(pr => {
    const data = pr.data;
    const stale = pr.status !== "ok";
    const badge = `PR #${pr.number} · ${prStateLabel(pr)}`;
    const link = pr.url ? `<a class="pr-badge" href="${escape(pr.url)}" target="_blank" rel="noreferrer" title="${escape(data?.title || badge)}">${escape(badge)}</a>` : `<span class="pr-badge">${escape(badge)}</span>`;
    const ci = data ? `<span class="pr-ci">${escape(Object.hasOwn(ciLabels, data.ci) ? ciLabels[data.ci] : "CI 未確認")}${stale ? "（前回）" : ""}</span>` : "";
    const age = stale && data ? `<span class="pr-age" data-pr-fetched="${escape(pr.fetched_at)}" title="${escape(formatTime(pr.fetched_at, true))}">${escape(prAge(pr.fetched_at))}</span>` : "";
    const message = pr.error?.message || (stale ? "PR の状態を確認中です" : "");
    return `<div class="pr-entry ${stale ? "pr-stale" : ""}">${link}${ci}${detailed && data ? `<span class="pr-title">${escape(data.title)}</span>${data.merged_at ? `<span class="note">マージ ${escape(formatTime(data.merged_at, true))}</span>` : ""}` : ""}${message ? `<span class="pr-unavailable">${escape(message)}</span>` : ""}${age}</div>`;
  }).join("");
  return `<div class="pr-meta">${entries}${prFlags(task)}</div>`;
}

function projectPRSummary(project) {
  const summary = project.pr_summary;
  if (!summary?.linked_count) return "";
  const unknown = summary.unknown_count;
  const mismatch = summary.mismatch_count;
  const text = `${unknown ? "確認済み: PR の食い違い" : "PR の食い違い"} ${count(mismatch)} 件 · CI 失敗 ${count(summary.ci_failure_count)} 件${unknown ? ` · PR 未確認 ${count(unknown)} 件` : ""}`;
  return `<span class="project-pr-summary ${unknown || mismatch || summary.ci_failure_count ? "pr-attention" : "note"}" title="${escape((summary.errors || []).join(" / "))}">${escape(text)}</span>`;
}

function disclosure(key, heading, body, className = "", openByDefault = true) {
  const open = state.disclosures.get(key) ?? openByDefault;
  return `<details class="disclosure ${className}" data-collapse="${escape(key)}" ${open ? "open" : ""}><summary>${heading}</summary><div class="disclosure-body">${body}</div></details>`;
}

function currentVersion() {
  const versions = ordered(rows().versions);
  return versions.find((version) => rows().tasks.some((task) => task.version === version.id && task.status === "doing"))
    || versions.find((version) => version.status !== "done");
}

function displayedMtime(data) {
  if (!data) return null;
  return data.error ? data.last_good_mtime ?? data.last_good?.source_mtime ?? null : data.source_mtime;
}

function directoryProjects() {
  const needle = state.q.trim().toLocaleLowerCase("ja");
  return (state.projectSummaries || []).filter((project) => (!state.type || project.adapter === state.type)
    && `${project.name} ${project.id}`.toLocaleLowerCase("ja").includes(needle)).sort((a, b) => {
    const sessionOrder = compareSessionProjects(a, b);
    if (sessionOrder) return sessionOrder;
    const byName = a.name.localeCompare(b.name, "ja", { numeric: true });
    const order = state.sort === "name" ? byName : (displayedMtime(a) ?? -Infinity) - (displayedMtime(b) ?? -Infinity);
    return (state.direction === "asc" ? 1 : -1) * order || byName;
  });
}

function compareSessionProjects(a, b) {
  const sessionA = a.adapter === "claude-sessions", sessionB = b.adapter === "claude-sessions";
  if (sessionA !== sessionB) return sessionA ? -1 : 1;
  if (!sessionA) return 0;
  return Number(Boolean(b.session_active)) - Number(Boolean(a.session_active))
    || (displayedMtime(b) ?? 0) - (displayedMtime(a) ?? 0) || a.id.localeCompare(b.id);
}

function allIssues() {
  return state.projects.flatMap((project) => (state.projectSnapshots.get(project.id)?.last_good?.rows.tasks || []).map((task) => ({ project, task })));
}

function isOwnerWaiting({ project, task }) {
  const adapter = state.projectSummaries?.find((item) => item.id === project.id)?.adapter;
  return ownerWaiting(task) || ownerStatus(task.status) || (adapter === "winsmux-backlog" && task.status === "blocked");
}

function recentItems() {
  return state.recents.flatMap((item) => {
    const project = state.projects.find((project) => project.id === item.projectId);
    if (!project) return [];
    const good = state.projectSnapshots.get(project.id)?.last_good;
    const task = item.issue ? good?.rows.tasks.find((task) => task.id === item.issue) : null;
    if (item.issue && good && !task) return [];
    return [{ ...item, project, task }];
  });
}

function waitingCount(project) {
  return project.adapter === "winsmux-backlog" ? project.counts?.blocked ?? null : project.owner_wait;
}

function homeIssues() {
  const issues = allIssues();
  if (state.homeTab === "recent") return recentItems().filter((item) => item.task).map(({ project, task }) => ({ project, task }));
  if (state.homeTab === "starred") return issues.filter(({ project }) => state.starred.includes(project.id));
  if (state.homeTab === "doing") return issues.filter(({ task }) => task.status === "doing");
  return issues.filter(isOwnerWaiting);
}

function projectReadState(projectId) {
  const snapshot = state.projectSnapshots.get(projectId);
  const summary = state.projectSummaries?.find((project) => project.id === projectId);
  const error = snapshot?.error || summary?.error;
  return {
    message: state.projectErrors.get(projectId) || error?.message || "",
    failedAt: error?.at,
    loadedAt: snapshot?.last_good?.loaded_at ?? summary?.loaded_at,
  };
}

function projectReadBadge(projectId) {
  const { message, loadedAt } = projectReadState(projectId);
  if (!message) return "";
  return `<span class="project-error project-read-state" title="${escape(message)}">読み込み失敗・${loadedAt ? `最終正常 ${escape(formatTime(loadedAt))}` : "未取得"}</span>`;
}

function renderForYou() {
  const starred = (state.projectSummaries || []).filter((project) => state.starred.includes(project.id));
  const cards = starred.map((project) => `<article class="starred-project-card"><div class="card-project-heading"><a href="${escape(projectHash(project.id))}">${projectIcon(project)}<strong>${escape(project.name)}</strong></a>${starButton(project.id)}</div>${projectReadBadge(project.id)}${project.counts ? progressCounts(project.counts.done, project.total, project.name) : '<p class="note">未取得</p>'}<p class="note">自分待ち ${waitingCount(project) ?? "未取得"}</p>${projectPRSummary(project)}${projectLoadState(project)}</article>`).join("");
  const tabs = [["waiting", "自分待ち"], ["doing", "進行中"], ["starred", "スター付き"], ["recent", "最近見た"]];
  const navigation = `<nav class="home-tabs project-tabs" aria-label="あなた向けの課題">${tabs.map(([id, title]) => `<a href="${escape(hashFor({ homeTab: id }))}" ${state.homeTab === id ? 'aria-current="page"' : ""}>${title}</a>`).join("")}</nav>`;
  const list = homeIssues().map(({ project, task }) => `<li><a class="cross-issue-row" href="${escape(projectHash(project.id, task.id))}" data-cross-issue="${escape(task.id)}" data-project="${escape(project.id)}"><span class="cross-project-name">${escape(project.name)}${projectReadBadge(project.id)}</span><span class="issue-key">${escape(task.id)}</span><div class="issue-title">${escape(task.title)}${labelBadge(task)}${prFlags(task)}${descriptionLine(task)}${progressLine(task, state.projectSnapshots.get(project.id))}</div>${lozenge(task.status)}</a></li>`).join("");
  const errors = state.projects.flatMap((project) => {
    const snapshot = state.projectSnapshots.get(project.id);
    const { message, loadedAt } = projectReadState(project.id);
    const warnings = snapshot?.last_good?.warnings || [];
    if (!message && !warnings.length) return [];
    return [`<p class="project-error">${escape(project.name)}: ${escape(message || warnings.join(" / "))}${loadedAt ? `（最後に読めた時刻: ${escape(formatTime(loadedAt, true))}）` : "（未取得）"}</p>`];
  }).join("");
  return `<section class="starred-projects"><div class="section-heading"><h2>スター付きプロジェクト</h2><a href="#projects">すべてのプロジェクトを表示</a></div><div class="project-cards">${cards || '<p class="note">プロジェクトにスターを付けると、ここに表示されます</p>'}</div></section>${navigation}${errors}<p class="note home-count">${count(homeIssues().length)} 件の課題</p>${list ? `<ul class="cross-issue-list">${list}</ul>` : empty("該当する課題はありません。")}`;
}

function renderSidebar() {
  const projectLink = (project) => `<a href="${escape(projectHash(project.id))}" ${!globalView() && state.projectId === project.id ? 'aria-current="page"' : ""}>${projectIcon(project)}<span>${escape(project.name)}${projectReadBadge(project.id)}</span></a>`;
  const starred = state.starred.map((id) => state.projects.find((project) => project.id === id)).filter(Boolean);
  const recent = recentItems();
  $("#nav-recent").innerHTML = recent.map((item) => `<a href="${escape(projectHash(item.project.id, item.issue))}">${item.issue ? `<span class="nav-issue-key">${escape(item.issue)}</span><span>${escape(item.task?.title || item.issue)}<small>${escape(item.project.name)}</small>${projectReadBadge(item.project.id)}</span>` : `${projectIcon(item.project)}<span>${escape(item.project.name)}${projectReadBadge(item.project.id)}</span>`}</a>`).join("") || '<p class="note">まだ履歴はありません。</p>';
  $("#nav-starred").innerHTML = starred.map(projectLink).join("") || '<p class="note">スター付きはありません。</p>';
  const sessions = (state.projectSummaries || []).filter(project => project.adapter === "claude-sessions").sort(compareSessionProjects);
  const ids = [...new Set([...sessions.map(project => project.id), ...state.starred, ...recent.map((item) => item.project.id)])];
  $("#nav-projects").innerHTML = ids.map((id) => state.projects.find((project) => project.id === id)).filter(Boolean).map(projectLink).join("") + '<a class="all-projects-link" href="#projects">すべてのプロジェクトを表示</a>';
  if (state.view === "for-you") $("#for-you-link").setAttribute("aria-current", "page");
  else $("#for-you-link").removeAttribute("aria-current");
}

function renderSearch() {
  const needle = state.searchQuery.trim().toLocaleLowerCase("ja");
  const projects = needle ? state.projects.filter((project) => `${project.name} ${project.id}`.toLocaleLowerCase("ja").includes(needle)) : [];
  const issues = needle ? allIssues().filter(({ project, task }) => `${project.name} ${project.id} ${task.id} ${task.title}`.toLocaleLowerCase("ja").includes(needle)) : [];
  state.searchItems = [
    ...projects.map((project) => ({ projectId: project.id, issue: "", title: project.name, subtitle: project.id })),
    ...issues.map(({ project, task }) => ({ projectId: project.id, issue: task.id, title: task.title, subtitle: `${project.name} · ${task.id}` })),
  ];
  state.searchIndex = Math.min(state.searchIndex, Math.max(0, state.searchItems.length - 1));
  const result = (item, index) => `<a id="search-option-${index}" role="option" aria-selected="${index === state.searchIndex}" href="${escape(projectHash(item.projectId, item.issue))}" data-search-index="${index}"><span>${escape(item.title)}</span><small>${escape(item.subtitle)}</small>${projectReadBadge(item.projectId)}</a>`;
  const group = (title, items, offset) => items.length ? `<div role="group" aria-label="${title}"><h2>${title}</h2>${items.map((_, index) => result(state.searchItems[index + offset], index + offset)).join("")}</div>` : "";
  $("#search-results").innerHTML = group("プロジェクト", projects, 0) + group("課題", issues, projects.length) || empty("検索結果はありません。");
  if (state.summaryError || state.projects.some((project) => projectReadState(project.id).message)) $("#search-results").innerHTML += '<p class="project-error search-notice">一部の課題を取得できていません。最後に読めた内容から検索しています。</p>';
  $("#search-results").hidden = !state.searchOpen || !needle;
  $("#global-search").setAttribute("aria-expanded", String(!$("#search-results").hidden));
  if (!$("#search-results").hidden && state.searchItems.length) $("#global-search").setAttribute("aria-activedescendant", `search-option-${state.searchIndex}`);
  else $("#global-search").removeAttribute("aria-activedescendant");
}

function closeSearch() {
  state.searchOpen = false;
  // Hiding/removing a focused option fires focusout synchronously. Do not rebuild it here.
  $("#search-results").hidden = true;
  $("#global-search").setAttribute("aria-expanded", "false");
  $("#global-search").removeAttribute("aria-activedescendant");
}

function openSearchItem(index) {
  const item = state.searchItems[index];
  if (!item) return;
  closeSearch();
  navigate(parseHash(projectHash(item.projectId, item.issue)));
}

function relativeTime(value) {
  if (value == null) return "—";
  const seconds = (new Date(typeof value === "number" ? value * 1000 : value).getTime() - Date.now()) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  const units = [["year", 31_536_000], ["month", 2_592_000], ["day", 86_400], ["hour", 3600], ["minute", 60], ["second", 1]];
  const [unit, size] = units.find(([, size]) => Math.abs(seconds) >= size) || units.at(-1);
  return new Intl.RelativeTimeFormat("ja", { numeric: "auto" }).format(Math.round(seconds / size), unit);
}

function timeCell(value) {
  if (value == null) return "—";
  const iso = new Date(typeof value === "number" ? value * 1000 : value).toISOString();
  return `<time datetime="${escape(iso)}" title="${escape(formatTime(value, true))}" data-relative-time="${escape(iso)}">${escape(relativeTime(value))}</time>`;
}

function projectLoadState(project) {
  const messages = [];
  const { message } = projectReadState(project.id);
  if (message) {
    messages.push(`読み込み失敗${project.error ? `（${formatTime(project.error.at, true)}）` : ""}: ${message}`);
    messages.push(project.loaded_at ? `最後に読めた時刻: ${formatTime(project.loaded_at, true)}。その時点の件数です。` : "まだ正常に読み込めた内容はありません。");
  }
  for (const warning of project.warnings || []) messages.push(`検証警告（${formatTime(project.loaded_at, true)}）: ${warning}`);
  const className = messages.length ? "project-error" : project.counts ? "lozenge s-done" : "note";
  messages.push(`最終確認 ${formatTime(project.checked_at, true)}`);
  return `<span class="${className}" title="${escape(messages.join("\n"))}">${message ? "読み込み失敗" : project.counts ? "正常" : "未取得"}</span>`;
}

function renderProjects() {
  if (!state.projectSummaries) return empty("プロジェクト一覧を読み込んでいます。上の読込状態を確認してください。");
  const projects = directoryProjects();
  const sortHeading = (key, title, className = "") => `<th scope="col" class="${className}" aria-sort="${state.sort === key ? state.direction === "asc" ? "ascending" : "descending" : "none"}"><button type="button" data-sort="${key}">${title}<span aria-hidden="true">${state.sort === key ? state.direction === "asc" ? "↑" : "↓" : "↕"}</span></button></th>`;
  const headings = `<th scope="col"><span class="sr-only">スター</span></th>${sortHeading("name", "名前")}<th scope="col" class="project-key">キー</th><th scope="col" class="project-type">種類</th><th scope="col" class="project-progress">進捗</th><th scope="col" class="project-counts">状態別件数</th><th scope="col" class="project-waiting">自分待ち</th>${sortHeading("updated", "更新", "project-updated")}<th scope="col" class="project-state">状態</th>`;
  const body = projects.slice((state.page - 1) * 25, state.page * 25).map((project) => {
    const href = projectHash(project.id);
    const progress = project.counts ? progressCounts(project.counts.done, project.total, project.name, true) : "未取得";
    const waiting = waitingCount(project);
    const nameTitle = `${project.name} · ${project.id} · ${project.adapter}${project.current_phase ? ` · 現在の段階 ${project.current_phase}` : ""}`;
    return `<tr data-project="${escape(project.id)}"><td>${starButton(project.id)}</td><th scope="row"><a class="project-name-link" href="${escape(href)}" title="${escape(nameTitle)}">${projectIcon(project)}<span class="project-name-text"><span class="project-name">${escape(project.name)}</span><span class="project-name-meta note">${escape(project.id)} · ${escape(project.adapter)}</span></span></a>${projectPRSummary(project)}</th><td class="project-key" title="${escape(project.id)}">${escape(project.id)}</td><td class="project-type" title="${escape(project.adapter)}">${escape(project.adapter)}</td><td class="project-progress">${progress}</td><td class="project-counts">${project.counts ? countLozenges(project.counts, true) : "未取得"}</td><td class="project-waiting">${waiting == null ? "未取得" : `<span class="${waiting ? "lozenge s-blocked" : "note"}">${count(waiting)}</span>`}</td><td class="project-updated">${timeCell(displayedMtime(project))}</td><td class="project-state">${projectLoadState(project)}</td></tr>`;
  }).join("");
  const pages = Math.max(1, Math.ceil(projects.length / 25));
  const pagination = `<nav class="pagination" aria-label="プロジェクト一覧のページ">${Array.from({ length: pages }, (_, index) => `<button type="button" data-page="${index + 1}" aria-label="${index + 1} ページ目" ${state.page === index + 1 ? 'aria-current="page"' : ""}>${index + 1}</button>`).join("")}</nav>`;
  return `<div class="table-box project-directory"><table class="project-table"><caption class="sr-only">設定済みの全プロジェクト。行を選ぶとボードを開きます。</caption><thead><tr>${headings}</tr></thead><tbody>${body || '<tr><td colspan="9">検索条件に一致するプロジェクトはありません。</td></tr>'}</tbody></table></div>${pagination}`;
}

function renderSummary() {
  const tasks = filteredTasks();
  const hidden = hiddenDone();
  const breakdown = `<div class="status-overview">${statuses.map((status) => `<a class="status-stat s-${status}" href="${escape(hashFor({ view: "list", status }))}"><strong>${count(tasks.filter((task) => task.status === status).length + (status === "done" ? hidden : 0))}</strong>${lozenge(status)}</a>`).join("")}</div>${progress(tasks, "全体", hidden)}`;
  const groups = [
    ["着手できる", tasks.filter((task) => task.ready === "yes")],
    ["進行中", tasks.filter((task) => task.status === "doing")],
    ["待っているもの", tasks.filter((task) => task.status === "blocked" || (task.status !== "done" && task.blocked_by))],
  ];
  const next = `<div class="next-grid">${groups.map(([title, items]) => `<section><h3>${title}<span class="count">${items.length}</span></h3>${taskList(items.slice(0, 3))}${items.length > 3 ? `<p class="note">先頭3件を表示 · 全${items.length}件</p>` : ""}</section>`).join("")}</div><a class="view-all" href="${escape(hashFor({ view: "backlog" }))}">バックログで全件を見る →</a>`;
  const phases = ordered(rows().phases).filter((phase) => !hasFilters() || tasks.some((task) => task.phase === phase.id));
  const phaseBody = phases.map((phase) => `<a class="phase-progress ${phase.is_current === "yes" ? "is-current" : ""}" href="${escape(hashFor({ view: "timeline", phase: phase.id }))}"><span class="phase-name">${escape(phaseTitle(phase.id))}${phase.is_current === "yes" ? '<span class="current-label">現在</span>' : ""}</span>${progress(tasks.filter((task) => task.phase === phase.id), phase.id)}</a>`).join("") || empty();
  const version = currentVersion();
  const versionBody = version ? `${versionBadge(version.id)}<h3>${escape(version.title)}</h3>${lozenge(version.status)}<p>${escape(version.delivers)}</p>${progress(tasks.filter((task) => task.version === version.id), version.id)}<h4>完了条件</h4><p class="prose">${escape(version.done_when)}</p><a href="${escape(hashFor({ view: "releases", version: version.id }))}">リリースを見る →</a>` : empty("現在の版はありません。");
  return projectPRSummary(state.snapshot.last_good) + panel("状態の内訳", breakdown) + panel("次の一手", next) + `<div class="summary-grid">${panel("段階ごとの進み具合", phaseBody)}${rows().versions.length ? panel("現在の版", versionBody, "current-version") : ""}</div>`;
}

function renderTimeline() {
  const tasks = filteredTasks();
  const phases = ordered(rows().phases).filter((phase) => !hasFilters() || tasks.some((task) => task.phase === phase.id));
  return '<p class="view-description">段階の順序と進捗を表示しています。日付の軸はありません。</p>' + (phases.map((phase) => {
    const children = tasks.filter((task) => task.phase === phase.id);
    const heading = `<span class="epic-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 3 9 9-9 9-9-9Z"/></svg></span><span class="section-name">${phase.id !== phase.title ? `<span class="note">${escape(phase.id)}</span>` : ""}<strong>${escape(phase.title)}</strong></span>${phase.is_current === "yes" ? '<span class="current-label">現在の段階</span>' : ""}${lozenge(phase.status)}<span class="count">${children.length}件</span>${progress(children, phase.id)}`;
    return disclosure(`phase:${phase.id}`, heading, taskList(children), `epic ${phase.is_current === "yes" ? "is-current" : ""}`, false);
  }).join("") || empty());
}

function renderBacklog() {
  const tasks = filteredTasks();
  const sections = [...ordered(rows().versions), { id: "", title: "バックログ" }];
  return sections.filter((version) => !hasFilters() || tasks.some((task) => task.version === version.id)).map((version) => {
    const children = tasks.filter((task) => task.version === version.id);
    const hidden = version.id ? 0 : hiddenDone();
    const heading = `<span class="section-name">${version.id ? versionBadge(version.id) : ""}<strong>${escape(version.title)}</strong></span><span class="count">${children.length}件</span>${statusCounts(children, hidden)}${progress(children, version.id || "バックログ", hidden)}`;
    return disclosure(`backlog:${version.id}`, heading, taskList(children), "backlog-section");
  }).join("") || empty();
}

function boardCard(task) {
  return `<article class="board-card ${task.id === state.issue ? "selected" : ""}"><a class="card-link" href="${escape(hashFor({ issue: task.id }))}" data-issue="${escape(task.id)}" title="${escape(task.title)}"><span class="card-title">${escape(task.title)}</span><span class="card-tags">${versionBadge(task.version)}${labelBadge(task)}${blockMark(task)}${ownerMark(task)}</span><span class="card-key"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h12v16H6zM9 12l2 2 4-4"/></svg>${escape(task.id)}</span></a>${descriptionLine(task)}${progressLine(task)}${activityLine(task)}${prDetails(task)}</article>`;
}

function boardColumns(tasks) {
  return `<div class="board-columns">${statuses.map((status) => {
    const children = tasks.filter((task) => task.status === status);
    const hidden = status === "done" && (state.group === "none" || (state.group === "version" && !rows().versions.length)) ? hiddenDone() : 0;
    return `<section class="board-column" aria-label="${labels[status]}"><h3>${lozenge(status)}<span class="count">${children.length + hidden}</span></h3><div class="card-list">${children.map(boardCard).join("") || (hidden ? "" : empty("課題なし"))}${hidden ? `<p class="note">非表示の完了 ${count(hidden)} 件</p>` : ""}</div></section>`;
  }).join("")}</div>`;
}

function renderBoard() {
  const tasks = filteredTasks();
  const group = state.group === "version" && !rows().versions.length ? "none" : state.group;
  const control = `<div class="board-toolbar"><p class="note">課題を選ぶと詳細を表示</p><label>グループ<select id="board-group" data-group><option value="none" ${group === "none" ? "selected" : ""}>なし</option><option value="phase" ${group === "phase" ? "selected" : ""}>段階</option>${rows().versions.length ? `<option value="version" ${group === "version" ? "selected" : ""}>版</option>` : ""}</select></label></div>`;
  if (group === "none") return control + boardColumns(tasks);
  const groups = state.group === "phase" ? ordered(rows().phases) : [...ordered(rows().versions), { id: "", title: "版なし" }];
  const lanes = groups.filter((group) => tasks.some((task) => task[state.group] === group.id)).map((group) => {
    const children = tasks.filter((task) => task[state.group] === group.id);
    return disclosure(`board:${state.group}:${group.id}`, `<strong>${escape(group.id)} ${escape(group.title)}</strong><span class="count">${children.length}件</span>`, boardColumns(children), "swimlane");
  }).join("");
  return control + (lanes || empty());
}

function renderList() {
  const columns = [["id", "課題キー"], ["title", "題"], ["status", "状態"], ["phase", "段階"], ...(rows().versions.length ? [["version", "版"]] : []), ["deps", "依存"], ["blocked_by", "ブロック元"]];
  const tasks = [...filteredTasks()].sort((a, b) => {
    const key = state.sort;
    const order = key === "status" ? statuses.indexOf(a.status) - statuses.indexOf(b.status)
      : String(a[key]).localeCompare(String(b[key]), "ja", { numeric: true });
    return (state.direction === "asc" ? 1 : -1) * order || a.id.localeCompare(b.id, "ja", { numeric: true });
  });
  const heading = columns.map(([key, title]) => `<th scope="col" aria-sort="${state.sort === key ? state.direction === "asc" ? "ascending" : "descending" : "none"}"><button type="button" data-sort="${key}">${title}<span aria-hidden="true">${state.sort === key ? state.direction === "asc" ? "↑" : "↓" : "↕"}</span></button></th>`).join("");
  const body = tasks.map((task) => `<tr class="${task.id === state.issue ? "selected" : ""}"><td data-label="課題キー"><a class="issue-key" href="${escape(hashFor({ issue: task.id }))}" data-issue="${escape(task.id)}">${escape(task.id)}</a></td><td data-label="題">${issueLink(task, "table-title")}${labelBadge(task)}${blockMark(task)}${descriptionLine(task)}${progressLine(task)}${activityLine(task)}${prDetails(task)}</td><td data-label="状態">${lozenge(task.status)}</td><td data-label="段階">${escape(phaseTitle(task.phase))}</td>${rows().versions.length ? `<td data-label="版">${versionBadge(task.version)}</td>` : ""}<td data-label="依存">${dependencyLinks(task.deps)}</td><td data-label="ブロック元">${dependencyLinks(task.blocked_by)}</td></tr>`).join("");
  return `<div class="table-box"><table class="issue-table"><caption class="sr-only">絞り込みに一致する全課題。列見出しで並べ替えられます。</caption><thead><tr>${heading}</tr></thead><tbody>${body || `<tr><td colspan="${columns.length}">該当する課題はありません。</td></tr>`}</tbody></table></div>`;
}

function renderReleases() {
  if (!rows().versions.length) return empty("このプロジェクトに版はありません。");
  const tasks = filteredTasks();
  return ordered(rows().versions).filter((version) => !hasFilters() || tasks.some((task) => task.version === version.id)).map((version) => {
    const children = tasks.filter((task) => task.version === version.id);
    const heading = `<span class="section-name">${versionBadge(version.id)}<strong>${escape(version.title)}</strong></span>${lozenge(version.status)}<span class="count">${children.length}課題</span>${progress(children, version.id)}`;
    const body = `<dl class="release-description"><div><dt>提供するもの</dt><dd>${escape(version.delivers)}</dd></div><div><dt>完了条件</dt><dd>${escape(version.done_when)}</dd></div></dl>${taskList(children)}`;
    return disclosure(`release:${version.id}`, heading, body, "release-section", false);
  }).join("") || empty("該当する版はありません。");
}

function options(items, selected, noVersion = false) {
  const missing = selected && !items.some((item) => item.id === selected) && !(noVersion && selected === "__none__");
  const missingLabel = Object.hasOwn(labels, selected) ? labels[selected] : selected;
  return '<option value="">すべて</option>' + items.map((item) => `<option value="${escape(item.id)}" ${selected === item.id ? "selected" : ""}>${escape(item.label ?? `${item.id} · ${item.title}`)}</option>`).join("")
    + (noVersion ? `<option value="__none__" ${selected === "__none__" ? "selected" : ""}>版なし</option>` : "")
    + (missing ? `<option value="${escape(selected)}" selected>${escape(missingLabel)}（存在しません）</option>` : "");
}

function renderShell() {
  const directory = state.view === "projects";
  const global = globalView();
  const name = global ? viewNames[state.view] : state.projects.find((project) => project.id === state.projectId)?.name || "進捗盤";
  $("#project-name").textContent = $("#breadcrumb-project").textContent = name;
  $("#project-star").innerHTML = global || !state.projectId ? "" : starButton(state.projectId);
  $("#route-notice").hidden = !state.routeNotice;
  $("#route-notice").textContent = state.routeNotice;
  document.title = `${viewNames[state.view]}${global ? "" : ` · ${name}`} · live-board`;
  $("#project-breadcrumb").hidden = $("#project-tabs").hidden = global;
  $("#filters").hidden = global;
  $("#project-tools").hidden = !directory;
  const good = state.snapshot?.last_good;
  const styleWarnings = global ? [] : good?.style_warnings || [];
  const disclosure = $("#style-warnings");
  disclosure.hidden = styleWarnings.length === 0;
  if (disclosure.dataset.project !== state.projectId || disclosure.hidden) disclosure.open = false;
  disclosure.dataset.project = state.projectId;
  $("#style-warnings-summary").textContent = `書き方の警告 ${count(styleWarnings.length)} 件`;
  $("#style-warnings-list").innerHTML = styleWarnings.map(warning => `<li>${escape(warning)}</li>`).join("");
  const hidden = good?.done_hidden || 0;
  $("#project-meta").textContent = global ? `${count(state.projects.length)} プロジェクト · 正本の変更を自動で反映します。` : good
    ? `${good.meta.product} · 正本の更新日 ${formatProjectUpdated(good.meta.updated)}${sourceDateText(good)}${good.meta.current_phase ? ` · 現在の段階 ${good.meta.current_phase}` : ""}${hidden ? ` · 非表示の完了 ${count(hidden)} 件は全体集計に含みます` : ""}` : "まだ正常に読み込めた内容はありません。";
  $("#project-meta").classList.toggle("source-date-mismatch", !global && good?.source_date?.mismatch === true);
  document.querySelectorAll("[data-view]").forEach((link) => {
    link.href = hashFor(global ? { ...routeDefaults, view: link.dataset.view, projectId: state.projects[0]?.id || "" } : { view: link.dataset.view });
    if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  renderSidebar();
  renderSearch();
  if (directory) {
    if ($("#project-search").value !== state.q) $("#project-search").value = state.q;
    const adapters = [...new Set((state.projectSummaries || []).map((project) => project.adapter))].sort();
    $("#project-type").innerHTML = '<option value="">種類で絞り込み</option>' + options(adapters.map((id) => ({ id, label: id })), state.type).replace('<option value="">すべて</option>', "");
    $("#project-type").value = state.type;
    const size = directoryProjects().length;
    const page = Math.min(state.page, Math.max(1, Math.ceil(size / 25)));
    if (state.projectSummaries && page !== state.page) {
      state.page = page;
      history.replaceState(null, "", hashFor());
    }
    $("#filter-note").hidden = !state.q && !state.type;
    $("#result-count").textContent = state.projectSummaries ? `${count(size)} 件のプロジェクトが見つかりました` : "プロジェクトを読込中";
    return;
  }
  if (global) {
    $("#result-count").textContent = "";
    $("#filter-note").hidden = true;
    return;
  }
  if ($("#search").value !== state.q) $("#search").value = state.q;
  $("#filter-status").innerHTML = options(statuses.map((id) => ({
    id, label: labels[id] + (state.status === id && !rows().tasks.some((task) => task.status === id) ? "（存在しません）" : ""),
  })), state.status);
  $("#filter-phase").innerHTML = options(ordered(rows().phases), state.phase);
  $("#filter-version").innerHTML = options(ordered(rows().versions), state.version, true);
  $("#version-filter").hidden = !rows().versions.length;
  $("#filter-ready").checked = state.ready;
  $("#filter-owner").checked = state.owner;
  $("#clear-filters").disabled = !hasFilters();
  $("#filter-note").hidden = !hasFilters();
  $("#result-count").textContent = good ? `${count(filteredTasks().length)} / ${count(rows().tasks.length)} 課題` : "課題を読込中";
}

function render() {
  const focusedSort = document.activeElement?.dataset.sort;
  const focusedGroup = document.activeElement?.hasAttribute("data-group");
  const focusedStar = document.activeElement?.dataset.starProject;
  const focusedPage = document.activeElement?.dataset.page;
  renderShell();
  const content = $("#content");
  content.setAttribute("aria-busy", (globalView() ? state.projectSummaries : state.snapshot) ? "false" : "true");
  content.innerHTML = globalView() || state.snapshot?.last_good ? views[state.view]() : empty("表示できる内容がありません。上の読込状態を確認してください。");
  if (focusedSort) content.querySelector(`[data-sort="${CSS.escape(focusedSort)}"]`)?.focus({ preventScroll: true });
  else if (focusedGroup) content.querySelector("[data-group]")?.focus({ preventScroll: true });
  else if (focusedStar) $(`[data-star-project="${CSS.escape(focusedStar)}"]`)?.focus({ preventScroll: true });
  else if (focusedPage) content.querySelector(`[data-page="${CSS.escape(focusedPage)}"]`)?.focus({ preventScroll: true });
  renderIssuePanel();
  observeStickyHeaders();
}

function updateStickyOffsets() {
  const header = $("#global-header").getBoundingClientRect();
  document.documentElement.style.setProperty("--header-height", `${header.height}px`);
  for (const summary of document.querySelectorAll(".swimlane > summary")) {
    summary.parentElement.style.setProperty("--swimlane-header-height", `${summary.getBoundingClientRect().height}px`);
  }
  // Horizontal table scrollers trap CSS sticky positioning.
  for (const table of document.querySelectorAll(".table-box table")) {
    const box = table.getBoundingClientRect();
    const height = table.tHead.getBoundingClientRect().height;
    const offset = Math.max(0, Math.min(header.bottom - box.top, box.height - height));
    table.style.setProperty("--table-header-offset", `${offset}px`);
  }
}

const stickyHeaderObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateStickyOffsets);
function observeStickyHeaders() {
  stickyHeaderObserver?.disconnect();
  for (const element of [$("#global-header"), ...document.querySelectorAll(".swimlane > summary, .table-box table")]) {
    stickyHeaderObserver?.observe(element);
  }
  updateStickyOffsets();
}
window.addEventListener("scroll", updateStickyOffsets, { passive: true });
window.addEventListener("resize", updateStickyOffsets);

function sourceDateText(good) {
  if (good.source_mtime == null) return "";
  const dates = good.source_date;
  if (dates?.file_local_date === null) return " · ファイル 日付不明";
  const mismatch = dates?.mismatch === true
    ? `（記載 ${dates.declared_local_date.replaceAll("-", "/")}・ファイル ${dates.file_local_date.replaceAll("-", "/")}）` : "";
  return ` · ファイル ${formatProjectUpdated(good.source_mtime * 1000)}${mismatch}`;
}

function formatProjectUpdated(value) {
  const date = new Date(value);
  if (value == null || Number.isNaN(date.getTime())) return "—";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value.replaceAll("-", "/");
  return date.toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function formatTime(value, full = false) {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("ja-JP", full
    ? { hour12: false } : { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function renderFreshness() {
  const directory = globalView();
  const summaries = state.projectSummaries || [];
  const checked = summaries.map((project) => project.checked_at).filter(Boolean).sort();
  const snapshot = directory ? { checked_at: checked[0], error: summaries.find((project) => project.error)?.error } : state.snapshot;
  const projectFailures = state.projects.flatMap((project) => {
    const { message, failedAt, loadedAt } = projectReadState(project.id);
    return message ? [`${project.name}: 読み込み失敗${failedAt ? `（${formatTime(failedAt, true)}）` : ""}: ${message}（${loadedAt ? `最終正常 ${formatTime(loadedAt, true)}` : "未取得"}）`] : [];
  });
  const activityNotices = state.projects.flatMap(project => {
    const activity = state.projectSnapshots.get(project.id)?.activity || summaries.find(item => item.id === project.id)?.activity;
    return [
      ...(activity?.errors || []).map(error => ({ failed: true, message: `${project.name}: 実行状況の読み込み失敗（${error.file}）: ${error.message}` })),
      ...(activity?.unmatched || []).map(item => ({ failed: false, message: `${project.name}: 対応する課題のない実行状況（${item.file} / ${item.card}）: ${item.step}${item.detail ? `（${item.detail}）` : ""}` })),
    ];
  });
  const activityFailed = activityNotices.some(notice => notice.failed);
  const fetchError = state.summaryError || (!directory ? state.fetchError : "");
  const validationWarnings = directory ? [] : [...(snapshot?.last_good?.warnings || []), ...(snapshot?.history_warning ? [snapshot.history_warning] : [])];
  const modified = directory ? summaries.map(displayedMtime).filter((value) => value != null).sort((a, b) => b - a)[0] : displayedMtime(snapshot);
  $("#file-time").textContent = `正本ファイルの更新 ${formatTime(modified)}${directory ? ` · ${count(state.projects.length)} プロジェクト` : ""}`;
  $("#file-time").title = formatTime(modified, true);
  const doing = directory ? [] : (snapshot?.last_good?.rows.tasks || []).filter(task => task.status === "doing");
  const oldest = doing.filter(task => task.description_changed_at).sort((a, b) => Date.parse(a.description_changed_at) - Date.parse(b.description_changed_at)
    || Number(b.description_baseline) - Number(a.description_baseline))[0];
  $("#description-time").hidden = directory;
  $("#description-time").textContent = oldest ? `進行中カードの記述 最古 ${elapsedAge(oldest.description_changed_at)}${oldest.description_baseline ? "以上" : ""}前`
    : doing.length ? "進行中カードの記述 更新時刻不明" : "進行中カードなし";
  $("#check-time").textContent = `最終確認 ${formatTime(snapshot?.checked_at)}`;
  $("#check-time").title = formatTime(snapshot?.checked_at, true);
  $("#connection").textContent = state.connection;
  const age = snapshot?.checked_at ? Date.now() - Date.parse(snapshot.checked_at) : Infinity;
  const old = !Number.isFinite(age) || age > 60_000;
  const sourceError = snapshot?.error || projectFailures.length > 0;
  const warning = Boolean(sourceError || fetchError || activityNotices.length || validationWarnings.length || state.connection !== "接続中" || old);
  $("#freshness").className = `freshness ${sourceError || fetchError || activityFailed ? "failed" : warning ? "warning" : "healthy"}`;
  const notices = [];
  if (sourceError) notices.push("読み込み失敗・最後に読めた内容を表示");
  if (fetchError) notices.push("データの取得に失敗");
  if (activityFailed) notices.push("実行状況の読み込み失敗");
  if (activityNotices.some(notice => !notice.failed)) notices.push("対応する課題のない実行状況あり");
  if (validationWarnings.length) notices.push("検証警告あり");
  if (state.connection !== "接続中") notices.push("自動更新の接続を確認してください");
  if (old) notices.push(snapshot?.checked_at ? "最終確認から60秒以上経過" : "確認時刻がまだありません");
  $("#freshness-message").textContent = notices.join(" / ") || "正本ファイルの変更を監視中（記述の新しさは別表示）";
  const messages = [...projectFailures];
  if (fetchError) messages.push(fetchError);
  if (messages.length) messages.push(directory && state.projectSummaries
    ? `最後に取得できた集計を表示しています。最終確認: ${formatTime(snapshot.checked_at, true)}。`
    : snapshot?.last_good ? `最後に読めた時刻: ${formatTime(snapshot.last_good.loaded_at, true)}。その時点の内容を表示しています。`
      : "まだ正常に読み込めた内容はありません。");
  messages.push(...activityNotices.map(notice => notice.message));
  for (const warning of validationWarnings) messages.push(`検証警告（${formatTime(snapshot.last_good.loaded_at, true)}）: ${warning}`);
  $("#error-banner").hidden = messages.length === 0;
  $("#error-banner").textContent = messages.join(" ");
  document.querySelectorAll("[data-pr-fetched]").forEach(element => { element.textContent = prAge(element.dataset.prFetched); });
  document.querySelectorAll("[data-relative-time]").forEach((element) => { element.textContent = relativeTime(element.dataset.relativeTime); });
  document.querySelectorAll("[data-description-changed]").forEach(element => {
    element.textContent = descriptionText(element.dataset.descriptionChanged, element.dataset.descriptionBaseline === "true");
  });
  document.querySelectorAll("[data-progress-changed]").forEach(element => {
    element.textContent = `${elapsedAge(element.dataset.progressChanged)}前`;
  });
  document.querySelectorAll("[data-activity-heartbeat]").forEach(element => {
    const age = activityAge(element.dataset.activityHeartbeat);
    element.classList.toggle("activity-stale", age.stale);
    element.querySelector(".activity-age").textContent = age.text;
  });
}

async function readJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function checkCode() {
  const baseline = await codeBaseline;
  if (!baseline || codeReloading) return;
  try {
    const { fingerprint } = await readJson("/healthz");
    if (typeof fingerprint !== "string" || !fingerprint) return;
    codeReloadPending = fingerprint !== baseline;
    if (!codeReloadPending || codeReloading || codeComposing || document.activeElement?.matches("input, textarea, select")) return;
    const key = `live-board.reloaded.${fingerprint}`;
    try {
      if (sessionStorage.getItem(key)) return;
      sessionStorage.setItem(key, "1");
    } catch { /* Storage may be unavailable in an embedded page. */ }
    codeReloading = true;
    location.reload();
  } catch { /* The mobile copy has no health endpoint. */ }
}

async function refreshProject(id) {
  const request = (state.projectRequests.get(id) || 0) + 1;
  state.projectRequests.set(id, request);
  try {
    const snapshot = await readJson(`/api/projects/${encodeURIComponent(id)}`);
    if (request !== state.projectRequests.get(id) || !state.projects.some(project => project.id === id)) return;
    state.projectSnapshots.set(id, snapshot);
    state.projectErrors.delete(id);
    if (!globalView() && id === state.projectId) {
      const changed = state.snapshot?.last_good?.loaded_at !== snapshot.last_good?.loaded_at
        || state.snapshot?.pr_revision !== snapshot.pr_revision
        || state.snapshot?.activity_revision !== snapshot.activity_revision
        || state.snapshot?.progress_revision !== snapshot.progress_revision
        || state.snapshot?.history_warning !== snapshot.history_warning || !state.snapshot;
      state.snapshot = snapshot;
      state.fetchError = "";
      if (snapshot.last_good && state.issue && !snapshot.last_good.rows.tasks.some((task) => task.id === state.issue)) {
        if (applyRoute(parseHash(hashFor()), true)) await refresh();
      } else if (changed || !snapshot.last_good) render();
    } else if (globalView()) render();
  } catch (error) {
    if (request !== state.projectRequests.get(id) || !state.projects.some(project => project.id === id)) return;
    const message = `データを取得できません（${error.message}）。接続が戻ると再取得します。`;
    state.projectErrors.set(id, message);
    if (!globalView() && id === state.projectId) state.fetchError = message;
    if (globalView()) render();
  }
  renderSidebar();
  renderSearch();
  renderFreshness();
}

async function refresh() {
  if (globalView()) return refreshOverview();
  if (state.projectId) await refreshProject(state.projectId);
}

async function refreshSummary() {
  const request = ++state.summaryRequestNumber;
  try {
    const summaries = await readJson("/api/summary");
    if (request !== state.summaryRequestNumber) return;
    state.projectSummaries = summaries;
    state.summaryError = "";
    if (globalView()) render();
  } catch (error) {
    if (request !== state.summaryRequestNumber) return;
    state.summaryError = `プロジェクトの集計を取得できません（${error.message}）。接続が戻ると再取得します。`;
  }
  renderSidebar();
  renderSearch();
  renderFreshness();
}

async function refreshOverview(refreshList = true) {
  if (refreshList) await refreshCatalog();
  await Promise.all([refreshSummary(), ...state.projects.map((project) => refreshProject(project.id))]);
}

async function refreshCatalog() {
  const request = ++state.catalogRequestNumber;
  try {
    const projects = await readJson("/api/projects");
    if (request !== state.catalogRequestNumber) return;
    state.projects = projects;
    const ids = new Set(projects.map(project => project.id));
    if (state.projectSummaries) state.projectSummaries = state.projectSummaries.filter(project => ids.has(project.id));
    for (const cache of [state.projectSnapshots, state.projectErrors]) {
      for (const id of cache.keys()) if (!ids.has(id)) cache.delete(id);
    }
    // Invalidate pending requests for removed projects without reusing a number
    // if the same folder reappears before an older request finishes.
    for (const id of state.projectRequests.keys()) {
      if (!ids.has(id)) state.projectRequests.set(id, state.projectRequests.get(id) + 1);
    }
    if (!globalView() && !ids.has(state.projectId)) {
      state.snapshot = null;
      applyRoute(projects.length ? parseHash() : { ...routeDefaults, view: "projects" }, true);
    }
  } catch (error) {
    if (request === state.catalogRequestNumber) {
      state.fetchError = `プロジェクト一覧を取得できません（${error.message}）。再接続時に読み直します。`;
      renderFreshness();
    }
  }
}

async function start() {
  if (state.starting) return;
  state.starting = true;
  try {
    state.projects = await readJson("/api/projects");
    state.fetchError = "";
    applyRoute(landingRoute(location.hash, state.projects.length), true);
    await refreshOverview(false);
  } catch (error) {
    state.fetchError = `プロジェクト一覧を取得できません（${error.message}）。再接続時に読み直します。`;
    state.summaryError = state.fetchError;
    renderFreshness();
  } finally {
    state.starting = false;
  }
}

function isRendered(element) {
  if (!element?.getClientRects().length || element.closest("[hidden]")) return false;
  if (["hidden", "collapse"].includes(getComputedStyle(element).visibility)) return false;
  for (let details = element.closest("details:not([open])"); details; details = details.parentElement?.closest("details:not([open])")) {
    if (!details.querySelector(":scope > summary")?.contains(element)) return false;
  }
  return true;
}

function issueNeighbors() {
  const links = $("#content").querySelectorAll("a.card-link[data-issue], a.issue-link[data-issue], a.issue-key[data-issue]");
  const ids = [...new Set(Array.from(links).filter(isRendered).map((link) => link.dataset.issue))];
  const index = ids.indexOf(state.issue);
  return { previous: index > 0 ? ids[index - 1] : "", next: index >= 0 ? ids[index + 1] || "" : "" };
}

function moveIssue(step) {
  const issue = issueNeighbors()[step < 0 ? "previous" : "next"];
  if (issue) navigate({ issue });
}

function clipboardField(value) {
  return String(value ?? "").replace(/\r\n?/g, "\n");
}

function issueCopyText(task) {
  // Copy viewer fields only, never project configuration, notes or fetch errors.
  const name = state.projects.find((project) => project.id === state.projectId)?.name || "進捗盤";
  const lines = [`${clipboardField(name)} ${clipboardField(task.id)}: ${clipboardField(task.title)}`, `状態: ${clipboardField(statusLabel(task.status))}`];
  if (task.phase) lines.push(`段階: ${clipboardField(phaseTitle(task.phase))}`);
  if (task.version) lines.push(`版: ${clipboardField(task.version)}`);
  lines.push(`完了条件: ${clipboardField(task.done_when) || "—"}`);
  if (task.deps) lines.push(`依存先: ${clipboardField(splitKeys(task.deps).join(", "))}`);
  const prs = task.prs || [];
  const missing = (task.pr || []).filter((number) => !prs.some((pr) => pr.number === number)).map((number) => ({ number }));
  for (const pr of [...prs, ...missing]) {
    if (!Number.isSafeInteger(pr.number) || pr.number <= 0) continue;
    const match = String(pr.url || pr.data?.url || "").match(/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/([1-9]\d*)(?=$|[?#])/i);
    const url = match && Number(match[1]) === pr.number ? match[0] : "";
    lines.push(`PR #${pr.number}: ${clipboardField(prStateLabel(pr))}${url ? ` ${url}` : ""}`);
  }
  return lines.join("\n");
}

function finishIssueCopy(feedback, fallback) {
  if (state.copyFeedback !== feedback) return;
  feedback.fallback = fallback;
  feedback.message = fallback ? "自動コピーできません。選択されたテキストを Ctrl+C でコピーしてください。" : "コピーしました";
  $("#issue-copy-status").textContent = feedback.message;
  $("#issue-copy-fallback").hidden = !fallback;
  const textarea = $("#issue-copy-text");
  textarea.value = feedback.text;
  if (fallback) { textarea.focus(); textarea.select(); }
}

function renderIssuePanel() {
  const panel = $("#issue-panel");
  const previousIssue = panel.dataset.issue || "";
  const changed = previousIssue !== state.issue || Boolean(state.issue) && panel.dataset.project !== state.projectId;
  const modal = Boolean(state.issue) && window.matchMedia("(max-width: 1199px)").matches;
  const focusedIssue = document.activeElement?.closest("#issue-panel [data-issue]")?.dataset.issue;
  const focusedClose = document.activeElement?.hasAttribute("data-close-issue");
  const focusedStep = document.activeElement?.dataset.issueStep;
  const focusedCopy = document.activeElement?.dataset.issueCopy;
  const focusedText = document.activeElement?.id === "issue-copy-text" ? document.activeElement : null;
  const selection = focusedText && [focusedText.selectionStart, focusedText.selectionEnd];
  if (changed) state.copyFeedback = null;
  panel.hidden = !state.issue;
  panel.dataset.issue = state.issue;
  panel.dataset.project = state.projectId;
  panel.setAttribute("aria-modal", String(modal));
  $("#detail-backdrop").hidden = !modal;
  $("#workspace").inert = modal;
  $("#global-header").inert = modal;
  document.body.classList.toggle("issue-open", Boolean(state.issue));
  document.body.classList.toggle("issue-modal", modal);
  if (!state.issue) {
    if (changed) {
      const opener = state.detailOpener || previousIssue;
      const link = $("#content").querySelector(`[data-issue="${CSS.escape(opener)}"]`);
      (link || $("#content")).focus({ preventScroll: true });
      state.detailOpener = null;
    }
    return;
  }
  const task = rows().tasks.find((item) => item.id === state.issue);
  if (!task) state.copyFeedback = null;
  if (task && state.pendingIssueVisit) {
    state.pendingIssueVisit = false;
    recordRecent(state.projectId, task.id);
    renderSidebar();
  }
  const version = task && rows().versions.find((item) => item.id === task.version);
  const details = task ? `<div class="detail-status">${lozenge(task.status)}${blockMark(task)}${ownerMark(task)}</div>
    ${descriptionLine(task, true)}${progressLine(task)}
    ${activityLine(task)}
    ${prDetails(task, true)}
    <dl class="detail-fields"><div><dt>段階</dt><dd>${escape(phaseTitle(task.phase))}</dd></div>${rows().versions.length ? `<div><dt>版</dt><dd>${versionBadge(task.version)}${version ? ` ${escape(version.title)}` : ""}</dd></div>` : ""}</dl>
    ${task.raw_status != null ? `<dl class="detail-fields"><div><dt>元の状態</dt><dd>${escape(statusLabel(task.raw_status, sourceLabels))}</dd></div><div><dt>ラベル</dt><dd>${escape(task.label || "—")}</dd></div></dl><section class="detail-section"><h3>補足</h3><p class="prose">${escape(task.note) || "—"}</p></section>` : ""}
    <section class="detail-section"><h3>完了条件</h3><p class="prose">${escape(task.done_when) || "—"}</p></section>
    <section class="detail-section"><h3>依存先</h3>${detailDependencies(task.deps, "依存先はありません。")}</section>
    <section class="detail-section"><h3>ブロック元</h3><p class="note">未完了の依存先</p>${detailDependencies(task.blocked_by, "ブロック元はありません。")}</section>`
    : empty(state.snapshot?.last_good ? "課題が見つかりません。上の × または Esc で閉じられます。" : "データを取得すると、選択した課題が表示されます。");
  const neighbors = issueNeighbors();
  const feedback = state.copyFeedback;
  const copy = task ? `<section class="issue-copy" aria-label="Claude に渡す"><div class="issue-copy-actions"><button type="button" class="text-button" data-issue-copy="key">キーをコピー</button><button type="button" class="text-button" data-issue-copy="handoff">Claude に渡す内容をコピー</button></div><p id="issue-copy-status" class="note copy-status" role="status" aria-live="polite" aria-atomic="true">${escape(feedback?.message)}</p><div id="issue-copy-fallback" ${feedback?.fallback ? "" : "hidden"}><label for="issue-copy-text">コピーする内容</label><textarea id="issue-copy-text" rows="6" readonly aria-describedby="issue-copy-status">${escape(feedback?.text)}</textarea></div></section>` : "";
  panel.innerHTML = `<div class="detail-toolbar"><span class="issue-key">${escape(state.issue)}</span><button type="button" class="icon-button" data-close-issue aria-label="課題の詳細を閉じる（Esc）"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></div><h2 id="issue-heading">${escape(task?.title || "課題の詳細")}</h2>
    <nav class="issue-navigation" aria-label="課題の前後移動"><button type="button" class="text-button" data-issue-step="-1" aria-keyshortcuts="k" ${neighbors.previous ? "" : "disabled"}>← 前の課題</button><button type="button" class="text-button" data-issue-step="1" aria-keyshortcuts="j" ${neighbors.next ? "" : "disabled"}>次の課題 →</button></nav>${copy}${details}`;
  if (changed) panel.scrollTop = 0;
  if (!changed && focusedText && feedback?.fallback) {
    const textarea = $("#issue-copy-text");
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(...selection);
  } else if (!changed && focusedCopy) panel.querySelector(`[data-issue-copy="${CSS.escape(focusedCopy)}"]`)?.focus({ preventScroll: true });
  else if (focusedStep) {
    const button = panel.querySelector(`[data-issue-step="${CSS.escape(focusedStep)}"]`);
    (button?.disabled ? panel.querySelector("[data-close-issue]") : button)?.focus({ preventScroll: true });
  } else if (changed || focusedClose) panel.querySelector("[data-close-issue]").focus({ preventScroll: true });
  else if (focusedIssue) panel.querySelector(`[data-issue="${CSS.escape(focusedIssue)}"]`)?.focus({ preventScroll: true });
}

function trapFocus(container, event) {
  const controls = Array.from(container.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]'))
    .filter((control) => control.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (first && event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (last && !event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function openShortcuts() {
  const dialog = $("#shortcuts-dialog");
  if (dialog.open) return;
  const opener = document.activeElement;
  const selector = opener?.id ? `#${CSS.escape(opener.id)}`
    : opener?.dataset.issueCopy ? `[data-issue-copy="${CSS.escape(opener.dataset.issueCopy)}"]`
      : opener?.dataset.issueStep ? `[data-issue-step="${CSS.escape(opener.dataset.issueStep)}"]`
        : opener?.getAttribute("href") != null ? `[href="${CSS.escape(opener.getAttribute("href"))}"]` : "";
  state.shortcutsOpener = { element: opener, selector, containerId: opener?.parentElement?.closest("[id]")?.id };
  closeSearch();
  dialog.showModal();
  $("#shortcuts-close").focus();
}

function closeShortcuts() {
  const dialog = $("#shortcuts-dialog");
  if (!dialog.open) return;
  dialog.close();
  const opener = state.shortcutsOpener;
  state.shortcutsOpener = null;
  const container = opener?.containerId ? $(`#${CSS.escape(opener.containerId)}`) : document.body;
  const replacement = opener?.element?.id ? $(`#${CSS.escape(opener.element.id)}`) : opener?.selector ? container?.querySelector(opener.selector) : null;
  const targets = [opener?.element, replacement, state.sidebarOpen ? $("#sidebar-toggle") : null,
    !$("#issue-panel").hidden ? $("#issue-panel [data-close-issue]") : null, $("#content")];
  for (const target of targets) {
    if (!target?.isConnected || target === document.body || target.disabled || target.matches?.(":disabled")
      || !isRendered(target) || target.closest("[inert]") || dialog.contains(target)) continue;
    target.focus({ preventScroll: true });
    if (document.activeElement === target) return;
  }
}

function detailDependencies(keys, emptyMessage) {
  const ids = splitKeys(keys);
  if (!ids.length) return `<p class="note">${emptyMessage}</p>`;
  return `<ul class="dependency-list">${ids.map((id) => {
    const dependency = rows().tasks.find((task) => task.id === id);
    return `<li>${issueLink(dependency || { id, title: "課題が見つかりません" })}${dependency ? lozenge(dependency.status) : ""}</li>`;
  }).join("")}</ul>`;
}

function renderSidebarVisibility() {
  const mobile = window.matchMedia("(max-width: 599px)").matches;
  const open = mobile && state.sidebarOpen;
  $("#workspace").classList.toggle("sidebar-collapsed", !mobile && state.sidebarCollapsed);
  $("#workspace").classList.toggle("sidebar-open", open);
  $("#sidebar-backdrop").hidden = !open;
  $("#sidebar").inert = mobile && !open;
  if (open) {
    $("#sidebar").setAttribute("role", "dialog");
    $("#sidebar").setAttribute("aria-modal", "true");
  } else {
    $("#sidebar").removeAttribute("role");
    $("#sidebar").removeAttribute("aria-modal");
  }
  $("#main-area").inert = $("#top-bar").inert = open;
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
  $("#sidebar-toggle").setAttribute("aria-expanded", String(mobile ? open : !state.sidebarCollapsed));
  $("#sidebar-toggle").setAttribute("aria-label", mobile ? "ナビゲーションを閉じる" : state.sidebarCollapsed ? "サイドバーを広げる" : "サイドバーを折りたたむ");
}

function toggleSidebar() {
  if (window.matchMedia("(max-width: 599px)").matches) {
    if (state.issue) navigate({ issue: "" });
    state.sidebarOpen = !state.sidebarOpen;
    renderSidebarVisibility();
    $(state.sidebarOpen ? "#sidebar-toggle" : "#menu-toggle").focus();
  } else {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    renderSidebarVisibility();
  }
}

function closeSidebar() {
  if (!state.sidebarOpen) return;
  state.sidebarOpen = false;
  renderSidebarVisibility();
  $("#menu-toggle").focus();
}

$("#filters").addEventListener("submit", (event) => event.preventDefault());
$("#filters").addEventListener("change", (event) => {
  const key = event.target.dataset.filter;
  if (key) navigate({ [key]: key === "ready" || key === "owner" ? event.target.checked : event.target.value });
});
$("#search").addEventListener("input", (event) => {
  if (!event.isComposing) navigate({ q: event.target.value }, true);
});
$("#search").addEventListener("compositionend", (event) => navigate({ q: event.target.value }, true));
$("#clear-filters").addEventListener("click", () => navigate({ q: "", status: "", phase: "", version: "", ready: false, owner: false }));
$("#project-tools").addEventListener("submit", (event) => event.preventDefault());
$("#project-search").addEventListener("input", (event) => {
  if (!event.isComposing) navigate({ q: event.target.value, page: 1 }, true);
});
$("#project-search").addEventListener("compositionend", (event) => navigate({ q: event.target.value, page: 1 }, true));
$("#project-type").addEventListener("change", (event) => navigate({ type: event.target.value, page: 1 }));
$("#global-search-form").addEventListener("submit", (event) => event.preventDefault());
$("#global-search-form").addEventListener("focusout", (event) => {
  if (!$("#global-search-form").contains(event.relatedTarget)) closeSearch();
});
$("#global-search").addEventListener("input", (event) => {
  if (event.isComposing) return;
  state.searchQuery = event.target.value;
  state.searchOpen = true;
  state.searchIndex = 0;
  renderSearch();
});
$("#global-search").addEventListener("compositionend", (event) => {
  state.searchQuery = event.target.value;
  state.searchOpen = true;
  state.searchIndex = 0;
  renderSearch();
});
$("#global-search").addEventListener("focus", () => { state.searchOpen = true; renderSearch(); });
$("#global-search").addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if (event.key === "Enter") {
    event.preventDefault();
    if (state.searchOpen) openSearchItem(state.searchIndex);
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    state.searchOpen = true;
    state.searchIndex = Math.max(0, Math.min(state.searchItems.length - 1, state.searchIndex + (event.key === "ArrowDown" ? 1 : -1)));
    renderSearch();
    $(`#search-option-${state.searchIndex}`)?.scrollIntoView?.({ block: "nearest" });
  }
});
$("#content").addEventListener("change", (event) => {
  if (event.target.hasAttribute("data-group")) navigate({ group: event.target.value });
});
$("#content").addEventListener("toggle", (event) => {
  const key = event.target.dataset.collapse;
  if (!key || !event.target.isConnected) return;
  state.disclosures.set(key, event.target.open);
  if (state.issue) renderIssuePanel();
}, true);
$("#content").addEventListener("click", (event) => {
  if (event.target.closest("[data-star-project]")) return;
  const button = event.target.closest("[data-sort]");
  if (button) navigate({ sort: button.dataset.sort, direction: state.sort === button.dataset.sort ? state.direction === "asc" ? "desc" : "asc" : button.dataset.sort === "updated" ? "desc" : "asc", page: 1 });
  const page = event.target.closest("[data-page]");
  if (page) navigate({ page: Number(page.dataset.page) });
  const row = event.target.closest("tr[data-project]");
  if (row && !event.target.closest("a, button") && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    navigate({ ...routeDefaults, view: "board", projectId: row.dataset.project });
  }
});
document.addEventListener("click", (event) => {
  const star = event.target.closest("[data-star-project]");
  if (star) { toggleStar(star.dataset.starProject); return; }
  if (event.target.id === "sidebar-backdrop") closeSidebar();
  if (!event.target.closest("#global-search-form")) {
    closeSearch();
  }
  if (event.target.closest(".skip-link")) {
    event.preventDefault(); $("#content").focus();
  }
  const link = event.target.closest("a[data-issue]");
  const cross = event.target.closest("a[data-cross-issue]");
  const search = event.target.closest("[data-search-index]");
  if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
    if (cross) { event.preventDefault(); navigate(parseHash(projectHash(cross.dataset.project, cross.dataset.crossIssue))); }
    else if (search) { event.preventDefault(); openSearchItem(Number(search.dataset.searchIndex)); }
    else if (link) {
      event.preventDefault();
      if (!state.issue) state.detailOpener = link.dataset.issue;
      navigate({ issue: link.dataset.issue });
    }
  }
  if (event.target.closest("#global-nav a")) closeSidebar();
  const copy = event.target.closest("[data-issue-copy]");
  if (copy) {
    const task = rows().tasks.find((item) => item.id === state.issue);
    if (!task) return;
    const text = copy.dataset.issueCopy === "key" ? clipboardField(task.id) : issueCopyText(task);
    const feedback = { text, message: "", fallback: false };
    state.copyFeedback = feedback;
    $("#issue-copy-status").textContent = "";
    $("#issue-copy-fallback").hidden = true;
    try {
      if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) finishIssueCopy(feedback, true);
      else navigator.clipboard.writeText(text).then(() => finishIssueCopy(feedback, false), () => finishIssueCopy(feedback, true));
    } catch { finishIssueCopy(feedback, true); }
    return;
  }
  const step = event.target.closest("[data-issue-step]");
  if (step && !step.disabled) moveIssue(Number(step.dataset.issueStep));
  if (event.target.closest("[data-close-issue]") || event.target.id === "detail-backdrop") navigate({ issue: "" });
});
document.addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if ($("#shortcuts-dialog").open) {
    if (event.key === "Escape") { event.preventDefault(); closeShortcuts(); }
    else if (event.key === "Tab") trapFocus($("#shortcuts-dialog"), event);
    return;
  }
  const editing = event.target.closest("input, select, textarea, [contenteditable]");
  if (event.key === "?" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault(); openShortcuts(); return;
  }
  if (event.ctrlKey && !event.altKey && !event.metaKey && (event.key === "[" || event.code === "BracketLeft")) {
    event.preventDefault(); toggleSidebar(); return;
  }
  if (event.key === "Escape") {
    if (state.searchOpen) { event.preventDefault(); closeSearch(); }
    else if (state.sidebarOpen) { event.preventDefault(); closeSidebar(); }
    else if (state.issue) { event.preventDefault(); navigate({ issue: "" }); }
  }
  if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    closeSidebar();
    if (state.issue) navigate({ issue: "" });
    $("#global-search").focus();
  }
  if (state.issue && !editing && !event.ctrlKey && !event.metaKey && !event.altKey && ["j", "k"].includes(event.key.toLowerCase())) {
    event.preventDefault(); moveIssue(event.key.toLowerCase() === "j" ? 1 : -1);
  }
  if (event.key === "Tab" && !event.target.closest("#global-search")) closeSearch();
  if (event.key === "Tab" && state.sidebarOpen) {
    const controls = $("#sidebar").querySelectorAll("a[href], button, summary");
    const visible = Array.from(controls).filter((control) => control.getClientRects().length);
    const first = visible[0], last = visible.at(-1);
    if (first && event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (last && !event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  if (event.key === "Tab" && document.body.classList.contains("issue-modal")) {
    trapFocus($("#issue-panel"), event);
  }
});
$("#shortcuts-close").addEventListener("click", closeShortcuts);
$("#shortcuts-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeShortcuts(); });
$("#sidebar-toggle").addEventListener("click", toggleSidebar);
$("#menu-toggle").addEventListener("click", toggleSidebar);
$("#global-nav").addEventListener("click", (event) => {
  if (event.target.closest("summary") && state.sidebarCollapsed) { state.sidebarCollapsed = false; renderSidebarVisibility(); }
});
window.addEventListener("hashchange", () => navigate(landingRoute(location.hash, state.projects.length), true));
window.matchMedia("(max-width: 1199px)").addEventListener("change", renderIssuePanel);
window.matchMedia("(max-width: 599px)").addEventListener("change", () => { state.sidebarOpen = false; renderSidebarVisibility(); });
const recheck = () => state.projects.length ? refreshOverview() : start();
const codeBaseline = readJson("/healthz").then((data) => typeof data?.fingerprint === "string" ? data.fingerprint : "").catch(() => "");
let codeReloadPending = false, codeComposing = false, codeReloading = false;
window.addEventListener("focus", () => { checkCode(); recheck(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { checkCode(); recheck(); } });
document.addEventListener("compositionstart", () => { codeComposing = true; });
document.addEventListener("compositionend", () => { codeComposing = false; });
document.addEventListener("blur", () => { if (codeReloadPending) Promise.resolve().then(checkCode); }, true);
setInterval(renderFreshness, 1000);
const events = new EventSource("/events");
events.onopen = () => { state.connection = "接続中"; renderFreshness(); checkCode(); recheck(); };
events.onerror = () => { state.connection = "切断・再接続中"; renderFreshness(); };
events.addEventListener("project", async (event) => {
  const id = JSON.parse(event.data);
  if (state.projects.some((project) => project.id === id)) await Promise.all([refreshSummary(), refreshProject(id)]);
});
events.addEventListener("projects", refreshOverview);
events.addEventListener("heartbeat", recheck);
render();
renderSidebarVisibility();
start();
