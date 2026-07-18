"use strict";

const ACTIVE_STATUSES = new Set([
  "QUEUED", "PROVISIONING", "RUNNING", "INSPECTING", "CHECKING",
  "PREPROCESSING", "COMPILING", "VERIFYING", "COLLECTING", "CANCELLING",
]);
const RETRYABLE_STATUSES = new Set(["FAILED", "CANCELLED", "INTERRUPTED"]);
const TERMINAL_STATUSES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
const state = {
  csrf: "",
  projects: [],
  currentProject: null,
  projectDeletionPreview: null,
  profiles: [],
  runs: [],
  preflight: null,
  currentView: "workspace",
  wizardStep: 1,
  wizardPreview: null,
  currentRun: null,
  runEventSource: null,
  logEventSource: null,
  logSequence: 0,
  logsPaused: false,
  smokeStarted: false,
  pollBusy: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = "toast"; }, 3800);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && typeof options.body === "string") headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-RDKWT-CSRF", state.csrf);
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败（${response.status}）`);
  return payload;
}

function uploadBinary(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    request.responseType = "json";
    request.setRequestHeader("X-RDKWT-CSRF", state.csrf);
    const filenameBytes = new TextEncoder().encode(file.name);
    let filenameBinary = "";
    filenameBytes.forEach((byte) => { filenameBinary += String.fromCharCode(byte); });
    request.setRequestHeader("X-Filename-B64", btoa(filenameBinary));
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve(request.response);
      else reject(new Error(request.response?.detail || `上传失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error("上传连接失败"));
    request.send(file);
  });
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(value));
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "—";
  if (milliseconds < 1000) return `${milliseconds} ms`;
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(1)} s`;
  return `${(milliseconds / 60000).toFixed(1)} min`;
}

function statusClass(status) {
  if (status === "SUCCEEDED" || status === "READY" || status === "PASS") return "good";
  if (["FAILED", "INTERRUPTED", "BLOCKED"].includes(status)) return "bad";
  if (ACTIVE_STATUSES.has(status)) return "active";
  return "neutral";
}

function statusPill(status) {
  const node = el("span", `status-pill ${statusClass(status)}`);
  node.append(el("span"), document.createTextNode(status));
  return node;
}

function setProgress(selector, ratio) {
  const node = $(selector);
  node.classList.remove("hidden");
  node.firstElementChild.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
  if (ratio >= 1) setTimeout(() => node.classList.add("hidden"), 700);
}

function runKindLabel(kind) {
  return {CONVERSION: "模型转换", MODEL_INSPECTION: "模型检查", PROBE: "Runner 预检"}[kind] || kind;
}

async function loadProjects(selectId = state.currentProject?.id) {
  state.projects = await api("/api/v1/projects");
  renderProjectList();
  if (selectId && state.projects.some((project) => project.id === selectId)) {
    await selectProject(selectId, false);
  }
}

function renderProjectList() {
  const list = $("#project-list");
  list.replaceChildren();
  $("#project-empty").classList.toggle("hidden", state.projects.length > 0);
  for (const project of state.projects) {
    const button = el("button", `project-item${state.currentProject?.id === project.id ? " active" : ""}`);
    button.type = "button";
    button.append(
      el("strong", "", project.name),
      el("span", "", `${project.model_count} 模型 · ${project.run_count || 0} 任务`),
    );
    button.addEventListener("click", () => selectProject(project.id));
    list.append(button);
  }
}

async function selectProject(projectId, switchView = true) {
  const [project, preview] = await Promise.all([
    api(`/api/v1/projects/${projectId}`),
    api(`/api/v1/projects/${projectId}/deletion-preview`),
  ]);
  state.currentProject = project;
  state.projectDeletionPreview = preview;
  if (switchView) showView("workspace");
  $("#welcome").classList.add("hidden");
  $("#project-workspace").classList.remove("hidden");
  renderProjectList();
  renderCurrentProject();
}

function projectModels() {
  return (state.currentProject?.models || []).flatMap((model) =>
    model.versions.map((version) => ({model, version})),
  );
}

function projectCalibrations() {
  return (state.currentProject?.calibration_sets || []).flatMap((calibrationSet) =>
    calibrationSet.versions.map((version) => ({calibrationSet, version})),
  );
}

function renderCurrentProject() {
  const project = state.currentProject;
  if (!project) return;
  const models = projectModels();
  const calibrations = projectCalibrations();
  const projectRuns = state.runs.filter((run) => run.project_id === project.id);
  const diskBytes = state.projectDeletionPreview?.disk_usage_bytes ?? project.disk_usage_bytes;
  $("#current-project-name").textContent = project.name;
  $("#current-project-description").textContent = project.description || "暂无备注";
  $("#metric-models").textContent = models.length;
  $("#metric-model-ready").textContent = `${models.filter(({version}) => version.compatibility_status === "READY").length} 可转换`;
  $("#metric-calibrations").textContent = project.calibration_set_count;
  $("#metric-calibration-ready").textContent = `${calibrations.filter(({version}) => version.status === "READY").length} 已冻结`;
  $("#metric-runs").textContent = projectRuns.length;
  $("#metric-run-success").textContent = `${projectRuns.filter((run) => run.status === "SUCCEEDED").length} 成功`;
  $("#metric-storage").textContent = formatBytes(diskBytes);
  renderModels();
  renderCalibrations();
  renderProjectRuns();
}

function renderModels() {
  const list = $("#model-list");
  list.replaceChildren();
  for (const {model, version} of projectModels()) {
    const row = el("div", "asset-row");
    const copy = el("div");
    const inspection = version.inspection;
    const meta = inspection
      ? `IR ${inspection.ir_version ?? "—"} · opset ${inspection.opsets?.find((item) => item.domain === "ai.onnx")?.version ?? "—"} · ${formatBytes(version.asset.size_bytes)}`
      : `${formatBytes(version.asset.size_bytes)} · ${version.asset.sha256.slice(0, 12)}…`;
    copy.append(el("strong", "", model.name), el("small", "", meta));
    const actions = el("div", "asset-row-actions");
    const label = {
      READY: "READY", INSPECTING: "检查中", BLOCKED: "BLOCKED", PENDING_INSPECTION: "待检查",
    }[version.compatibility_status] || version.compatibility_status;
    const tagClass = version.compatibility_status === "READY" ? "" : version.compatibility_status === "BLOCKED" ? " blocked" : " pending";
    actions.append(el("span", `asset-tag${tagClass}`, label));
    if (version.compatibility_status !== "INSPECTING") {
      const inspect = el("button", "asset-action", version.inspection ? "重新检查" : "开始检查");
      inspect.type = "button";
      inspect.addEventListener("click", () => inspectModel(version.id));
      actions.append(inspect);
    }
    row.append(copy, actions);
    list.append(row);
  }
  if (!projectModels().length) list.append(el("div", "empty-state", "尚未上传 ONNX 模型"));
  populateWizardModels();
}

async function inspectModel(versionId) {
  try {
    const submission = await api(`/api/v1/model-versions/${versionId}/inspect`, {method: "POST"});
    toast(`模型检查 ${submission.run_id.slice(0, 8)} 已入队`);
    await Promise.all([loadRuns(), selectProject(state.currentProject.id, false)]);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderCalibrations() {
  const list = $("#calibration-list");
  const versionSelect = $("#calibration-version-select");
  const selected = versionSelect.value;
  list.replaceChildren();
  versionSelect.replaceChildren(new Option("选择校准版本", ""));
  for (const {calibrationSet, version} of projectCalibrations()) {
    const row = el("div", "asset-row");
    const copy = el("div");
    const warningCount = version.validation_report?.warnings?.length || 0;
    copy.append(
      el("strong", "", calibrationSet.name),
      el("small", "", `${version.sample_count} 个样本 · ${version.status === "READY" ? "清单已冻结" : "可继续上传"}${warningCount ? ` · ${warningCount} 警告` : ""}`),
    );
    row.append(copy, el("span", `asset-tag${version.status === "DRAFT" ? " draft" : ""}`, version.status));
    list.append(row);
    const option = new Option(`${calibrationSet.name} · ${version.sample_count} 张 · ${version.status}`, version.id);
    option.disabled = version.status !== "DRAFT";
    versionSelect.add(option);
  }
  if ([...versionSelect.options].some((option) => option.value === selected)) versionSelect.value = selected;
  if (!projectCalibrations().length) list.append(el("div", "empty-state", "新建草稿后可批量上传校准图片"));
  populateWizardCalibrations();
}

async function loadRuns() {
  state.runs = await api("/api/v1/runs");
  renderRunSummary();
  renderProjectRuns();
  renderAllRuns();
  const queued = state.runs.filter((run) => run.status === "QUEUED").length;
  const active = state.runs.filter((run) => ACTIVE_STATUSES.has(run.status) && run.status !== "QUEUED").length;
  $("#queue-status").textContent = `队列 ${queued} · 运行 ${active}`;
}

function createRunRow(run) {
  const row = el("button", "run-row");
  row.type = "button";
  const identity = el("div", "run-identity");
  const queue = run.queue_position ? ` · 队列 #${run.queue_position}` : "";
  identity.append(el("strong", "", `${runKindLabel(run.kind)} · ${run.profile_id}`), el("code", "", `${run.id}${queue}`));
  const latest = run.attempts[run.attempts.length - 1];
  row.append(
    identity,
    statusPill(run.status),
    el("span", "", latest?.stage || run.status),
    el("span", "", formatDate(run.created_at)),
    el("span", "run-chevron", "›"),
  );
  row.addEventListener("click", () => openRun(run.id));
  return row;
}

function renderProjectRuns() {
  const list = $("#project-run-list");
  if (!list || !state.currentProject) return;
  list.replaceChildren();
  const runs = state.runs.filter((run) => run.project_id === state.currentProject.id).slice(0, 12);
  if (!runs.length) return list.append(el("div", "empty-state", "暂无任务；完成模型检查与校准集定稿后即可创建转换。"));
  runs.forEach((run) => list.append(createRunRow(run)));
}

function renderRunSummary() {
  $("#runs-queued").textContent = state.runs.filter((run) => run.status === "QUEUED").length;
  $("#runs-active").textContent = state.runs.filter((run) => ACTIVE_STATUSES.has(run.status) && run.status !== "QUEUED").length;
  $("#runs-succeeded").textContent = state.runs.filter((run) => run.status === "SUCCEEDED").length;
  $("#runs-attention").textContent = state.runs.filter((run) => ["FAILED", "INTERRUPTED"].includes(run.status)).length;
}

function renderAllRuns() {
  const list = $("#all-run-list");
  if (!list) return;
  const filter = $("#run-filter").value;
  const query = $("#run-search").value.trim().toLowerCase();
  const projectNames = new Map(state.projects.map((project) => [project.id, project.name]));
  const runs = state.runs.filter((run) => {
    const statusMatches = filter === "all" || (filter === "active" ? ACTIVE_STATUSES.has(run.status) : run.status === filter);
    const haystack = `${run.id} ${run.profile_id} ${run.kind} ${projectNames.get(run.project_id) || ""}`.toLowerCase();
    return statusMatches && (!query || haystack.includes(query));
  });
  list.replaceChildren();
  if (!runs.length) return list.append(el("div", "empty-state", "没有符合条件的任务"));
  runs.forEach((run) => list.append(createRunRow(run)));
}

function showView(view) {
  state.currentView = view;
  $("#workspace-view").classList.toggle("hidden", view !== "workspace");
  $("#runs-view").classList.toggle("hidden", view !== "runs");
  $$(".topnav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
}

function renderPreflight() {
  const preflight = state.preflight;
  if (!preflight) return;
  const status = $("#system-status");
  status.className = `status-pill status-button ${preflight.available ? "good" : "bad"}`;
  status.replaceChildren(el("span"), document.createTextNode(preflight.available ? "环境可用" : "环境受阻"));
  const passed = preflight.checks.filter((item) => item.status === "PASS").length;
  $("#readiness-score").textContent = preflight.available ? `${passed}/${preflight.checks.length} 就绪` : `${preflight.checks.length - passed} 项受阻`;
  const checks = $("#readiness-checks");
  checks.replaceChildren();
  preflight.checks.slice(0, 5).forEach((item) => {
    const row = el("div", `readiness-item ${item.status.toLowerCase()}`);
    row.append(el("i"), el("span", "", checkLabel(item.id)), el("small", "", item.status));
    checks.append(row);
  });
  renderPreflightDialog();
}

function checkLabel(id) {
  return {
    "docker-engine": "Docker Engine", "runner-image": "Runner 镜像",
    "storage-state": "状态存储", "storage-assets": "资产存储", "storage-runs": "任务存储",
  }[id] || id;
}

function renderPreflightDialog() {
  const root = $("#preflight-details");
  const preflight = state.preflight;
  if (!root || !preflight) return;
  root.replaceChildren();
  preflight.checks.forEach((item) => {
    const row = el("div", `preflight-check ${item.status === "BLOCKED" ? "blocked" : ""}`);
    const copy = el("div");
    copy.append(el("strong", "", checkLabel(item.id)), el("small", "", item.message));
    row.append(el("span", "check-icon", item.status === "PASS" ? "✓" : "!"), copy, el("code", "", item.status));
    root.append(row);
  });
  const smoke = preflight.details.runner_smoke_test;
  const heading = el("div", "preflight-check");
  const smokeCopy = el("div");
  smokeCopy.append(el("strong", "", "Runner Smoke Test"), el("small", "", smoke.status === "NOT_RUN" ? "尚未运行；首次打开会自动执行一次受控工具探测。" : `任务 ${smoke.run_id?.slice(0, 8) || "—"} · ${formatDate(smoke.finished_at)}`));
  heading.append(el("span", "check-icon", smoke.status === "SUCCEEDED" ? "✓" : "•"), smokeCopy, el("code", "", smoke.status));
  root.append(heading);
  const versions = el("div", "version-grid");
  const versionValues = smoke.toolchain_versions || {};
  const image = preflight.details.runner_image || {};
  const values = {
    "Runner image": image.immutable_id || "unavailable",
    "Contract": image.contract_version || "—",
    "OpenExplorer": versionValues.openexplorer || "待探测",
    "HMCT": versionValues.hmct || "待探测",
    "HBDK": versionValues.hbdk || "待探测",
    "hb_compile": versionValues.hb_compile || "待探测",
  };
  Object.entries(values).forEach(([name, value]) => {
    const item = el("div"); item.append(el("span", "", name), el("code", "", value)); versions.append(item);
  });
  root.append(versions);
}

async function refreshPreflight(startSmoke = false) {
  state.preflight = await api("/api/v1/system/preflight");
  renderPreflight();
  const smoke = state.preflight.details.runner_smoke_test;
  if (startSmoke && state.preflight.available && smoke.status === "NOT_RUN" && !state.smokeStarted) {
    state.smokeStarted = true;
    try {
      await runSmokeTest(false);
    } catch (error) {
      toast(`Runner 预检未启动：${error.message}`, true);
    }
  }
}

async function runSmokeTest(showToast = true) {
  const submission = await api("/api/v1/system/preflight/runner-smoke-test", {
    method: "POST", body: JSON.stringify({profile_id: "s100-oe-3.7.0"}),
  });
  if (showToast) toast(`Runner Smoke Test ${submission.run_id.slice(0, 8)} 已入队`);
  await Promise.all([loadRuns(), refreshPreflight(false)]);
}

function showCreateProject() {
  $("#create-project-form").classList.remove("hidden");
  $("#project-name").focus();
}

function populateWizardModels() {
  const select = $("#wizard-model");
  if (!select) return;
  const selected = select.value;
  select.replaceChildren(new Option("选择模型", ""));
  for (const {model, version} of projectModels()) {
    const option = new Option(`${model.name} · ${version.asset.sha256.slice(0, 8)} · ${version.compatibility_status}`, version.id);
    option.disabled = version.compatibility_status !== "READY";
    select.add(option);
  }
  if ([...select.options].some((option) => option.value === selected && !option.disabled)) select.value = selected;
}

function populateWizardCalibrations() {
  const select = $("#wizard-calibration");
  if (!select) return;
  const selected = select.value;
  select.replaceChildren(new Option("选择已定稿版本", ""));
  for (const {calibrationSet, version} of projectCalibrations()) {
    if (version.status === "READY" && version.sample_count >= 20) {
      select.add(new Option(`${calibrationSet.name} · ${version.sample_count} 张`, version.id));
    }
  }
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

function renderProfileCards() {
  const root = $("#profile-cards");
  root.replaceChildren();
  state.profiles.forEach((profile) => {
    const button = el("button", "profile-card");
    button.type = "button";
    button.dataset.profile = profile.profile_id;
    const description = profile.platform === "s100" ? "nash-e · 单 Core · L2M 固定关闭" : "nash-p · 1/2 Core · L2M 可自动分配";
    button.append(el("strong", "", profile.display_name), el("span", "", profile.march), el("small", "", description));
    button.addEventListener("click", () => selectProfile(profile.profile_id, true));
    root.append(button);
  });
}

function selectProfile(profileId, resetInvalid = false) {
  const profile = state.profiles.find((item) => item.profile_id === profileId);
  if (!profile) return;
  const previous = $("#wizard-profile").value;
  $("#wizard-profile").value = profileId;
  $$(".profile-card").forEach((card) => card.classList.toggle("active", card.dataset.profile === profileId));
  const s600 = profile.platform === "s600";
  $("#core-num").querySelector('option[value="2"]').disabled = !s600;
  $("#l2m-mode").disabled = !s600;
  if (!s600) {
    $("#core-num").value = "1";
    $("#l2m-mode").value = "0";
  } else if (resetInvalid && previous !== profileId) {
    $("#core-num").value = "2";
    $("#l2m-mode").value = "auto";
    toast("已按 S600 能力重置 Core 与 L2M");
  }
  toggleL2mCustom();
  const locks = $("#profile-locks");
  locks.replaceChildren();
  const values = {"Profile": profile.profile_id, "march（锁定）": profile.march, "Core 能力": s600 ? "1 / 2" : "1", "L2M 能力": s600 ? "0…24 MiB / auto" : "0（锁定）", "适配器": profile.toolchain_adapter, "Profile SHA": profile.sha256.slice(0, 16)};
  Object.entries(values).forEach(([name, value]) => {
    const item = el("div", "lock-item"); item.append(el("span", "", name), el("strong", "", value)); locks.append(item);
  });
  saveWizardDraft();
}

const WIZARD_FIELDS = [
  "wizard-model", "wizard-profile", "input-name", "target-shape", "train-layout", "train-type", "runtime-type",
  "input-mean", "input-scale", "input-std", "wizard-calibration", "sample-limit", "calibration-algorithm",
  "recipe-id", "resize-short", "recipe-mean", "recipe-std", "output-prefix", "compile-mode", "balance-factor",
  "core-num", "l2m-mode", "l2m-custom", "optimize-level", "jobs",
];

function draftKey() {
  return `rdkwt:m2-draft:${state.currentProject?.id || "none"}`;
}

function saveWizardDraft() {
  if (!state.currentProject) return;
  const fields = {};
  WIZARD_FIELDS.forEach((id) => { fields[id] = $(`#${id}`).value; });
  try {
    localStorage.setItem(draftKey(), JSON.stringify({version: 1, saved_at: new Date().toISOString(), fields}));
    $("#wizard-draft-status").textContent = `草稿已保存 · ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  } catch (_error) {
    $("#wizard-draft-status").textContent = "浏览器未允许保存草稿";
  }
}

function loadWizardDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(draftKey())); } catch (_error) { draft = null; }
  if (draft?.version === 1) {
    WIZARD_FIELDS.forEach((id) => {
      if (draft.fields[id] !== undefined) $(`#${id}`).value = draft.fields[id];
    });
    $("#wizard-draft-status").textContent = `已恢复 ${formatDate(draft.saved_at)} 的草稿`;
  }
  const readyModel = projectModels().find(({version}) => version.compatibility_status === "READY");
  if (!$("#wizard-model").value && readyModel) $("#wizard-model").value = readyModel.version.id;
  const readyCalibration = projectCalibrations().find(({version}) => version.status === "READY" && version.sample_count >= 20);
  if (!$("#wizard-calibration").value && readyCalibration) $("#wizard-calibration").value = readyCalibration.version.id;
  const profileId = state.profiles.some((profile) => profile.profile_id === $("#wizard-profile").value)
    ? $("#wizard-profile").value : state.profiles[0]?.profile_id;
  selectProfile(profileId, false);
  const canKeepDraftInput = draft?.version === 1
    && draft.fields["wizard-model"] === $("#wizard-model").value
    && Boolean($("#input-name").value && $("#target-shape").value);
  updateWizardModel(!canKeepDraftInput);
  updateCalibrationSelection(false);
  toggleCompileMode();
  toggleL2mCustom();
}

function openWizard() {
  if (!state.currentProject) return toast("请先选择项目", true);
  populateWizardModels();
  populateWizardCalibrations();
  renderProfileCards();
  loadWizardDraft();
  setWizardStep(1);
  $("#wizard-dialog").showModal();
}

function setWizardStep(step) {
  state.wizardStep = step;
  $$(".wizard-page").forEach((page) => page.classList.toggle("hidden", Number(page.dataset.page) !== step));
  $$("#wizard-steps li").forEach((item) => {
    const itemStep = Number(item.dataset.step);
    item.classList.toggle("active", itemStep === step);
    item.classList.toggle("done", itemStep < step);
  });
  $("#wizard-back").classList.toggle("hidden", step === 1);
  $("#wizard-next").classList.toggle("hidden", step === 6);
  $("#wizard-submit").classList.toggle("hidden", step !== 6);
  $(".wizard-body").scrollTop = 0;
}

function selectedModelVersion() {
  return projectModels().find(({version}) => version.id === $("#wizard-model").value)?.version || null;
}

function updateWizardModel(overwrite = true) {
  const version = selectedModelVersion();
  const root = $("#wizard-model-info");
  root.replaceChildren();
  if (!version?.inspection) {
    root.className = "inspection-card empty-state";
    root.textContent = version ? "该模型尚未完成检查" : "请选择模型";
    return;
  }
  root.className = "inspection-card";
  const inspection = version.inspection;
  const header = el("div", "inspection-head");
  header.append(el("h4", "", version.original_filename), statusPill(version.compatibility_status));
  root.append(header);
  const opset = inspection.opsets?.find((item) => item.domain === "ai.onnx")?.version ?? "—";
  const meta = el("div", "inspection-meta");
  [["IR", inspection.ir_version], ["opset", opset], ["算子类型", Object.keys(inspection.operators || {}).length], ["文件", formatBytes(inspection.size_bytes)]].forEach(([name, value]) => {
    const item = el("div"); item.append(el("span", "", name), el("strong", "", String(value))); meta.append(item);
  });
  root.append(meta);
  const io = el("div", "inspection-io");
  [...(inspection.inputs || []), ...(inspection.outputs || [])].forEach((item, index) => {
    const block = el("div");
    block.append(el("span", "", index < (inspection.inputs || []).length ? "INPUT" : "OUTPUT"), el("code", "", `${item.name} · [${item.shape.join(", ")}] · ${item.dtype}`));
    io.append(block);
  });
  root.append(io);
  const input = inspection.inputs?.[0];
  if (input && overwrite) {
    $("#input-name").value = input.name;
    $("#target-shape").value = input.shape.map((item) => Number.isInteger(item) ? item : "").join(",");
  }
  saveWizardDraft();
}

function parseNumbers(value, name, allowEmpty = true) {
  if (!value.trim() && allowEmpty) return [];
  const values = value.split(",").map((item) => Number(item.trim()));
  if (!values.length || values.some((item) => !Number.isFinite(item))) throw new Error(`${name} 必须是逗号分隔的数字`);
  return values;
}

function parseShape() {
  const values = $("#target-shape").value.split(",").map((item) => Number(item.trim()));
  if (values.length !== 4 || values.some((item) => !Number.isInteger(item) || item < 1)) throw new Error("目标 Shape 必须包含 4 个正整数");
  return values;
}

function inputGeometry() {
  const shape = parseShape();
  const nchw = $("#train-layout").value === "NCHW";
  return {shape, channels: nchw ? shape[1] : shape[3], height: nchw ? shape[2] : shape[1], width: nchw ? shape[3] : shape[2]};
}

function validateWizardStep(step) {
  if (step === 1 && selectedModelVersion()?.compatibility_status !== "READY") throw new Error("请选择已通过检查的模型");
  if (step === 2 && !$("#wizard-profile").value) throw new Error("请选择目标平台");
  if (step === 3) {
    const {shape, channels, height, width} = inputGeometry();
    const expectedChannels = $("#train-type").value === "gray" ? 1 : 3;
    if (shape[0] !== 1 || channels !== expectedChannels) throw new Error(`当前输入类型要求 batch=1、channels=${expectedChannels}`);
    if ($("#runtime-type").value === "nv12" && (height % 2 || width % 2)) throw new Error("NV12 的目标宽高必须为偶数");
    [["Mean", $("#input-mean").value], ["Scale", $("#input-scale").value], ["Std", $("#input-std").value]].forEach(([name, value]) => {
      const numbers = parseNumbers(value, name);
      if (numbers.length && ![1, channels].includes(numbers.length)) throw new Error(`${name} 数量必须为 1 或 ${channels}`);
    });
  }
  if (step === 4) {
    const selected = projectCalibrations().find(({version}) => version.id === $("#wizard-calibration").value)?.version;
    const limit = Number($("#sample-limit").value);
    if (!selected || selected.status !== "READY") throw new Error("请选择已定稿校准版本");
    if (!Number.isInteger(limit) || limit < 20 || limit > selected.sample_count) throw new Error(`使用样本数必须在 20～${selected.sample_count} 之间`);
    const {channels} = inputGeometry();
    if (parseNumbers($("#recipe-mean").value, "Recipe Mean", false).length !== channels) throw new Error(`Recipe Mean 必须包含 ${channels} 项`);
    const std = parseNumbers($("#recipe-std").value, "Recipe Std", false);
    if (std.length !== channels || std.some((item) => item === 0)) throw new Error(`Recipe Std 必须包含 ${channels} 个非零值`);
  }
  if (step === 5) {
    if (!$("#output-prefix").checkValidity()) throw new Error("输出前缀格式不合法");
    if ($("#compile-mode").value === "balance" && !$("#balance-factor").checkValidity()) throw new Error("balance 模式需要 0～100 的 Balance factor");
  }
  if (step === 6 && !$("#confirm-snapshot").checked) throw new Error("请确认冻结配置后再提交");
}

function conversionPayload() {
  const l2m = $("#l2m-mode").value;
  return {
    profile_id: $("#wizard-profile").value,
    model_version_id: $("#wizard-model").value,
    calibration_version_id: $("#wizard-calibration").value,
    output_prefix: $("#output-prefix").value,
    input: {
      name: $("#input-name").value,
      target_shape: parseShape(),
      train_type: $("#train-type").value,
      train_layout: $("#train-layout").value,
      runtime_type: $("#runtime-type").value,
      normalization: {
        mean: parseNumbers($("#input-mean").value, "Mean"),
        scale: parseNumbers($("#input-scale").value, "Scale"),
        std: parseNumbers($("#input-std").value, "Std"),
      },
    },
    calibration: {
      algorithm: $("#calibration-algorithm").value,
      recipe: {
        id: $("#recipe-id").value,
        resize_short: Number($("#resize-short").value),
        mean: parseNumbers($("#recipe-mean").value, "Recipe Mean", false),
        std: parseNumbers($("#recipe-std").value, "Recipe Std", false),
      },
    },
    core_num: Number($("#core-num").value),
    max_l2m_size: l2m === "auto" ? "auto" : l2m === "custom" ? Number($("#l2m-custom").value) : 0,
    compile_mode: $("#compile-mode").value,
    balance_factor: $("#compile-mode").value === "balance" ? Number($("#balance-factor").value) : null,
    optimize_level: $("#optimize-level").value,
    sample_limit: Number($("#sample-limit").value),
    jobs: Number($("#jobs").value),
  };
}

async function fetchYamlPreview() {
  [1, 2, 3, 4, 5].forEach(validateWizardStep);
  $("#yaml-preview").textContent = "正在验证资源哈希并生成 YAML…";
  const preview = await api("/api/v1/conversion-previews", {method: "POST", body: JSON.stringify(conversionPayload())});
  state.wizardPreview = preview;
  $("#yaml-preview").textContent = preview.yaml;
  const warnings = $("#preview-warnings");
  warnings.replaceChildren();
  (preview.warnings || []).forEach((message) => warnings.append(el("div", "warning-item", message)));
  if (!(preview.warnings || []).length) warnings.append(el("div", "warning-item", "所有 P0 交叉字段和资源完整性检查均已通过。"));
}

function updateCalibrationSelection(save = true) {
  const selected = projectCalibrations().find(({version}) => version.id === $("#wizard-calibration").value)?.version;
  if (selected) $("#sample-limit").value = Math.min(Math.max(20, Number($("#sample-limit").value) || 20), selected.sample_count);
  renderCalibrationPreview().catch((error) => {
    $("#preview-stats").replaceChildren(el("span", "", `预览失败：${error.message}`));
  });
  if (save) saveWizardDraft();
}

async function renderCalibrationPreview() {
  const versionId = $("#wizard-calibration").value;
  const image = $("#preview-original");
  const canvas = $("#preview-processed");
  const statsRoot = $("#preview-stats");
  if (!versionId) {
    image.removeAttribute("src");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    statsRoot.replaceChildren(el("span", "", "选择校准版本后生成预览"));
    return;
  }
  const {channels, height, width, shape} = inputGeometry();
  const resizeShort = Number($("#resize-short").value);
  const means = parseNumbers($("#recipe-mean").value, "Recipe Mean", false);
  const stds = parseNumbers($("#recipe-std").value, "Recipe Std", false);
  if (means.length !== channels || stds.length !== channels || stds.some((value) => value === 0)) return;
  image.src = `/api/v1/calibration-versions/${versionId}/samples/0/content`;
  await new Promise((resolve, reject) => {
    if (image.complete && image.naturalWidth) return resolve();
    image.onload = resolve;
    image.onerror = () => reject(new Error("无法读取第一份校准样本"));
  });
  const scale = resizeShort / Math.min(image.naturalWidth, image.naturalHeight);
  const resizedWidth = Math.max(resizeShort, Math.round(image.naturalWidth * scale));
  const resizedHeight = Math.max(resizeShort, Math.round(image.naturalHeight * scale));
  if (resizedWidth < width || resizedHeight < height) throw new Error("Resize 后图片小于目标裁剪尺寸");
  const temporary = document.createElement("canvas");
  temporary.width = resizedWidth; temporary.height = resizedHeight;
  temporary.getContext("2d").drawImage(image, 0, 0, resizedWidth, resizedHeight);
  canvas.width = width; canvas.height = height;
  const context = canvas.getContext("2d", {willReadFrequently: true});
  context.drawImage(temporary, Math.floor((resizedWidth - width) / 2), Math.floor((resizedHeight - height) / 2), width, height, 0, 0, width, height);
  const pixels = context.getImageData(0, 0, width, height).data;
  const trainType = $("#train-type").value;
  let minimum = Infinity; let maximum = -Infinity; let sum = 0; let count = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    let values = trainType === "gray"
      ? [0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2]]
      : [pixels[index], pixels[index + 1], pixels[index + 2]];
    if (trainType === "bgr") values = values.reverse();
    values.forEach((value, channel) => {
      const normalized = (value / 255 - means[channel]) / stds[channel];
      minimum = Math.min(minimum, normalized); maximum = Math.max(maximum, normalized); sum += normalized; count += 1;
    });
  }
  statsRoot.replaceChildren();
  const stats = {
    "输出 Shape": `[${shape.join(", ")}]`, "dtype": "float32",
    "最小值": minimum.toFixed(5), "最大值": maximum.toFixed(5), "均值": (sum / count).toFixed(5),
    "处理链": `Resize ${resizeShort} → Center crop ${height}×${width}`,
  };
  Object.entries(stats).forEach(([name, value]) => {
    const line = el("div", "stat-line"); line.append(el("span", "", name), el("code", "", value)); statsRoot.append(line);
  });
}

function toggleCompileMode() {
  const balance = $("#compile-mode").value === "balance";
  $("#balance-factor").disabled = !balance;
  $("#balance-factor").required = balance;
}

function toggleL2mCustom() {
  $("#l2m-custom-field").classList.toggle("hidden", $("#l2m-mode").value !== "custom");
}

function closeRunStreams() {
  state.runEventSource?.close(); state.runEventSource = null;
  state.logEventSource?.close(); state.logEventSource = null;
}

async function openRun(runId) {
  closeRunStreams();
  state.currentRun = {id: runId};
  state.logSequence = 0;
  state.logsPaused = false;
  $("#pause-logs").textContent = "暂停";
  $("#log-viewer").replaceChildren();
  showRunTab("overview");
  $("#run-dialog").showModal();
  try {
    await refreshRunDetail();
    connectRunStreams();
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshRunDetail() {
  const runId = state.currentRun?.id;
  if (!runId) return;
  const run = await api(`/api/v1/runs/${runId}`);
  if (state.currentRun?.id !== runId) return;
  state.currentRun = run;
  renderRunDetail();
}

function renderRunDetail() {
  const run = state.currentRun;
  if (!run?.attempts) return;
  const attempt = run.attempts[run.attempts.length - 1];
  $("#run-kind").textContent = runKindLabel(run.kind);
  $("#run-title").textContent = `${run.profile_id} · ${run.id.slice(0, 8)}`;
  $("#run-subtitle").textContent = `${run.id} · Attempt ${attempt.number} · 创建于 ${formatDate(run.created_at)}`;
  const status = $("#run-status"); status.className = `status-pill ${statusClass(run.status)}`; status.textContent = run.status;
  $("#cancel-run").classList.toggle("hidden", !ACTIVE_STATUSES.has(run.status));
  $("#retry-run").classList.toggle("hidden", !RETRYABLE_STATUSES.has(run.status));
  $("#export-run").classList.toggle("hidden", !TERMINAL_STATUSES.has(run.status));
  $("#export-run").href = `/api/v1/runs/${run.id}/export`;
  $("#download-log").href = `/api/v1/runs/${run.id}/attempts/${attempt.number}/logs`;
  renderRunStepper(run, attempt);
  renderRunOverview(run, attempt);
  $("#run-yaml").textContent = run.generated_yaml || "该任务没有生成 YAML。";
  $("#run-request").textContent = JSON.stringify(run.request, null, 2);
  renderArtifacts(run, attempt);
}

function renderRunStepper(run, attempt) {
  const root = $("#run-stepper"); root.replaceChildren();
  const conversionStages = [
    ["QUEUED", "Queue", null], ["PROVISIONING", "Provision", null], ["INSPECTING", "Inspect", "inspect"],
    ["CHECKING", "Check", "check"], ["PREPROCESSING", "Preprocess", "preprocess"], ["COMPILING", "Compile", "compile"],
    ["VERIFYING", "Verify", "verify"], ["COLLECTING", "Collect", "collect"],
  ];
  const inspectionStages = [["QUEUED", "Queue", null], ["PROVISIONING", "Provision", null], ["INSPECTING", "Inspect", "inspect"], ["COLLECTING", "Collect", "collect"]];
  const stages = run.kind === "MODEL_INSPECTION" ? inspectionStages : conversionStages;
  const result = attempt.result;
  const completed = new Set((result?.steps || []).filter((item) => item.status === "succeeded").map((item) => item.step));
  const errorStep = result?.error?.step;
  const currentStatus = run.status;
  const currentIndex = stages.findIndex(([status]) => status === currentStatus);
  stages.forEach(([statusName, label, stepName], index) => {
    const node = el("div", "stage");
    if ((stepName && completed.has(stepName)) || (!stepName && (currentIndex > index || TERMINAL_STATUSES.has(run.status)))) node.classList.add("done");
    if (statusName === currentStatus || (run.status === "RUNNING" && statusName === "PROVISIONING")) node.classList.add("current");
    if (stepName && errorStep === stepName) node.classList.add("failed");
    if (statusName === "VERIFYING" && result && !(result.steps || []).some((item) => item.step === "verify")) node.title = "本任务未启用验证步骤（SKIPPED）";
    node.append(el("i"), el("span", "", label)); root.append(node);
  });
}

function summaryCard(label, value, note = "") {
  const card = el("div", "summary-card"); card.append(el("span", "", label), el("strong", "", value), el("small", "", note)); return card;
}

function keyValue(name, value) {
  const row = el("div", "key-value"); row.append(el("span", "", name), el("code", "", value ?? "—")); return row;
}

function renderRunOverview(run, attempt) {
  const root = $("#run-overview-tab"); root.replaceChildren();
  const summary = run.summary || {};
  const perf = summary.static_performance || {};
  const quant = summary.quantization || {};
  const hbm = summary.hbm;
  const outputCosine = quant.output_cosines?.[0];
  const cards = el("div", "summary-cards");
  cards.append(
    summaryCard("目标平台", run.profile_id, run.request?.configuration?.target_profile?.profile?.march || ""),
    summaryCard("HBM", hbm ? formatBytes(hbm.size_bytes) : "—", hbm?.relative_path || "尚未生成"),
    summaryCard("总阶段耗时", formatDuration(summary.total_duration_ms), `${summary.steps?.length || 0} 个已记录阶段`),
    summaryCard("静态性能", perf.fps !== undefined && perf.fps !== null ? `${perf.fps} FPS` : "—", perf.latency_us !== undefined && perf.latency_us !== null ? `${perf.latency_us} μs` : "无静态报告"),
  );
  root.append(cards);
  const grid = el("div", "detail-grid");
  const execution = el("div", "detail-card"); execution.append(el("h3", "", "执行快照"));
  execution.append(
    keyValue("状态 / 阶段", `${run.status} / ${attempt.stage}`), keyValue("Attempt", `${attempt.number}（${attempt.recovered ? "重启后恢复" : "正常启动"}）`),
    keyValue("Runner image", run.runner_image?.immutable_id), keyValue("应用 / 合约", `${run.app_version} / ${run.contract_version}`),
    keyValue("容器退出码", attempt.exit_code === null ? "—" : String(attempt.exit_code)), keyValue("Profile SHA", run.profile_sha256),
  );
  const metrics = el("div", "detail-card"); metrics.append(el("h3", "", "质量与资源"));
  metrics.append(
    keyValue("输出 Quantized Cosine", outputCosine ? `${outputCosine.name}: ${outputCosine.quantized_cosine}` : "—"),
    keyValue("最低节点 Cosine", quant.minimum_node ? `${quant.minimum_node.name}: ${quant.minimum_node.quantized_cosine}` : "—"),
    keyValue("最低内存估计", perf.minimum_memory_bytes ? formatBytes(perf.minimum_memory_bytes) : "—"),
    keyValue("DDR / run", perf.ddr_bytes_per_run ? formatBytes(perf.ddr_bytes_per_run) : "—"),
    keyValue("警告 / 建议产物", `${summary.warning_count || 0} / ${summary.advice_artifact_count || 0}`),
  );
  metrics.append(el("p", "cosine-note", "Cosine 仅反映量化前后数值相似度，不等同于最终业务精度；完整精度验证属于后续验证流程。"));
  grid.append(execution, metrics); root.append(grid);
  if (run.kind === "MODEL_INSPECTION" && attempt.result?.metrics?.inspect) {
    const inspection = attempt.result.metrics.inspect;
    const card = el("div", "detail-card"); card.style.marginTop = "14px"; card.append(el("h3", "", "ONNX 结构检查"));
    card.append(keyValue("兼容状态", inspection.compatibility_status), keyValue("IR / opset", `${inspection.ir_version} / ${inspection.opsets?.map((item) => `${item.domain}:${item.version}`).join(", ")}`), keyValue("输入", inspection.inputs?.map((item) => `${item.name} [${item.shape.join(",")}] ${item.dtype}`).join("; ")), keyValue("输出", inspection.outputs?.map((item) => `${item.name} [${item.shape.join(",")}] ${item.dtype}`).join("; ")), keyValue("external data", inspection.external_data ? "是（阻断）" : "否"));
    root.append(card);
  }
  if (run.error) {
    const error = el("div", "error-card"); error.append(el("h3", "", "错误诊断"), el("code", "", run.error.code), el("p", "", run.error.message || "任务未完成"), el("p", "", `建议：${run.error.advice || "下载日志后检查输入与环境。"}`)); root.append(error);
  }
}

function renderArtifacts(run, attempt) {
  const root = $("#artifact-list"); root.replaceChildren();
  (run.artifacts || []).forEach((artifact, index) => {
    const row = el("div", "artifact-row");
    const name = artifact.relative_path.split("/").pop();
    const actions = el("div", "artifact-actions");
    const href = `/api/v1/runs/${run.id}/attempts/${attempt.number}/artifacts/${index}`;
    const download = el("a", "button ghost small", artifact.kind === "hbm" ? "导出 HBM" : "下载"); download.href = href;
    actions.append(download);
    if (artifact.mime_type === "text/html") {
      const preview = el("button", "button ghost small", "预览"); preview.type = "button"; preview.addEventListener("click", () => openReport(`${href}?inline=true`)); actions.prepend(preview);
    }
    row.append(el("span", "artifact-kind", artifact.kind), el("strong", "", name), el("small", "", formatBytes(artifact.size_bytes)), actions);
    root.append(row);
  });
  if (!(run.artifacts || []).length) root.append(el("div", "empty-state", "当前 Attempt 尚无可识别产物"));
}

function openReport(url) {
  $("#report-frame").src = url;
  $("#report-preview").classList.remove("hidden");
}

function showRunTab(tab) {
  $$("[data-run-tab]").forEach((button) => button.classList.toggle("active", button.dataset.runTab === tab));
  $$(".run-tab").forEach((section) => section.classList.add("hidden"));
  $(`#run-${tab}-tab`).classList.remove("hidden");
}

function connectRunStreams() {
  const run = state.currentRun;
  if (!run?.attempts) return;
  const attempt = run.attempts[run.attempts.length - 1].number;
  state.runEventSource?.close();
  state.runEventSource = new EventSource(`/api/v1/runs/${run.id}/attempts/${attempt}/events`);
  let refreshTimer = null;
  state.runEventSource.addEventListener("runner", () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refreshRunDetail().catch(() => {}), 180);
  });
  state.runEventSource.addEventListener("terminal", () => {
    state.runEventSource?.close(); state.runEventSource = null;
    refreshRunDetail().then(loadRuns).catch(() => {});
  });
  if (!state.logsPaused) connectLogStream();
}

function connectLogStream() {
  const run = state.currentRun;
  if (!run?.attempts || state.logsPaused) return;
  const attempt = run.attempts[run.attempts.length - 1].number;
  state.logEventSource?.close();
  state.logEventSource = new EventSource(`/api/v1/runs/${run.id}/attempts/${attempt}/log-stream?after=${state.logSequence}`);
  state.logEventSource.addEventListener("log", (event) => {
    const item = JSON.parse(event.data);
    state.logSequence = Math.max(state.logSequence, Number(item.sequence));
    appendLog(item);
  });
  state.logEventSource.addEventListener("terminal", () => {
    state.logEventSource?.close(); state.logEventSource = null;
  });
}

function appendLog(item) {
  const viewer = $("#log-viewer");
  const line = el("div", `log-line ${item.stream || "combined"}`);
  line.dataset.search = String(item.text || "").toLowerCase();
  line.append(el("span", "stream", item.stream || "log"), el("span", "text", item.text || ""));
  viewer.append(line);
  while (viewer.childElementCount > 2500) viewer.firstElementChild.remove();
  applyLogSearch();
  if ($("#auto-scroll").checked) viewer.scrollTop = viewer.scrollHeight;
}

function applyLogSearch() {
  const query = $("#log-search").value.trim().toLowerCase();
  $$("#log-viewer .log-line").forEach((line) => {
    const matched = !query || line.dataset.search.includes(query);
    line.classList.toggle("hidden", !matched);
    line.classList.toggle("matched", Boolean(query) && matched);
  });
}

async function periodicRefresh() {
  if (state.pollBusy) return;
  state.pollBusy = true;
  try {
    await loadRuns();
    if (state.currentProject && !$("#model-progress").classList.contains("hidden")) return;
    if (state.currentProject) await selectProject(state.currentProject.id, false);
    const smoke = state.preflight?.details?.runner_smoke_test;
    if (smoke && ACTIVE_STATUSES.has(smoke.status)) await refreshPreflight(false);
    if (state.currentRun?.id && $("#run-dialog").open) await refreshRunDetail();
  } catch (_error) {
    // A transient polling failure is shown on the next explicit user action.
  } finally {
    state.pollBusy = false;
  }
}

$("#toggle-create-project").addEventListener("click", showCreateProject);
$("#welcome-create").addEventListener("click", showCreateProject);
$("#cancel-create-project").addEventListener("click", () => $("#create-project-form").classList.add("hidden"));
$("#create-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const project = await api("/api/v1/projects", {method: "POST", body: JSON.stringify({name: $("#project-name").value, description: $("#project-description").value})});
    event.target.reset(); event.target.classList.add("hidden"); await loadProjects(project.id); toast("项目已创建");
  } catch (error) { toast(error.message, true); }
});

$("#model-file").addEventListener("change", (event) => { $("#model-file-label").textContent = event.target.files[0]?.name || "上传后由隔离 Runner 解析结构和兼容性"; });
$("#model-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#model-file").files[0]; if (!file || !state.currentProject) return;
  const button = event.submitter; button.disabled = true;
  try {
    const modelName = $("#model-name").value;
    const path = `/api/v1/projects/${state.currentProject.id}/models${modelName ? `?model_name=${encodeURIComponent(modelName)}` : ""}`;
    const result = await uploadBinary(path, file, (ratio) => setProgress("#model-progress", ratio));
    event.target.reset(); $("#model-file-label").textContent = "上传后由隔离 Runner 解析结构和兼容性";
    await selectProject(state.currentProject.id, false);
    toast(result.storage_reused ? "模型已登记并复用相同内容，正在启动检查" : "模型上传完成，正在启动隔离检查");
    await inspectModel(result.id);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#calibration-create-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.currentProject) return;
  try {
    const created = await api(`/api/v1/projects/${state.currentProject.id}/calibration-sets`, {method: "POST", body: JSON.stringify({name: $("#calibration-name").value, description: ""})});
    event.target.reset(); await selectProject(state.currentProject.id, false); $("#calibration-version-select").value = created.versions[0].id; toast("校准集草稿已创建");
  } catch (error) { toast(error.message, true); }
});

$("#sample-files").addEventListener("change", (event) => { $("#sample-file-label").textContent = event.target.files.length ? `已选择 ${event.target.files.length} 个文件` : "可一次选择多张，按选择顺序登记"; });
$("#sample-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const versionId = $("#calibration-version-select").value; const files = [...$("#sample-files").files];
  if (!versionId) return toast("请选择一个 DRAFT 校准版本", true);
  const button = event.submitter; button.disabled = true;
  try {
    for (let index = 0; index < files.length; index += 1) {
      await uploadBinary(`/api/v1/calibration-versions/${versionId}/samples`, files[index], (ratio) => setProgress("#sample-progress", (index + ratio) / files.length));
    }
    event.target.reset(); $("#sample-file-label").textContent = "可一次选择多张，按选择顺序登记";
    await selectProject(state.currentProject.id, false); $("#calibration-version-select").value = versionId; toast(`${files.length} 个校准样本已登记`);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#finalize-calibration").addEventListener("click", async () => {
  const versionId = $("#calibration-version-select").value; if (!versionId) return toast("请选择一个 DRAFT 校准版本", true);
  try {
    const current = projectCalibrations().find(({version}) => version.id === versionId)?.version;
    if (!window.confirm(`定稿后不可再添加样本。确认冻结当前 ${current?.sample_count || 0} 个样本？`)) return;
    const result = await api(`/api/v1/calibration-versions/${versionId}/finalize`, {method: "POST"});
    await selectProject(state.currentProject.id, false); toast(result.sample_count < 20 ? "版本已定稿，但少于 20 张，不能用于标准转换" : "校准版本已定稿，Manifest 与源文件已冻结");
  } catch (error) { toast(error.message, true); }
});

$("#delete-project").addEventListener("click", async () => {
  if (!state.currentProject) return;
  try {
    const preview = await api(`/api/v1/projects/${state.currentProject.id}/deletion-preview`);
    if (!preview.can_delete) return toast(preview.blocked_reason, true);
    const summary = `${preview.model_count} 个模型、${preview.calibration_set_count} 个校准集、${preview.run_count} 个任务及 ${formatBytes(preview.disk_usage_bytes)} 数据`;
    if (!window.confirm(`永久删除“${state.currentProject.name}”及其 ${summary}？此操作不可撤销。`)) return;
    await api(`/api/v1/projects/${state.currentProject.id}`, {method: "DELETE", headers: {"X-Confirm-Project": state.currentProject.id}});
    state.currentProject = null; state.projectDeletionPreview = null; $("#project-workspace").classList.add("hidden"); $("#welcome").classList.remove("hidden"); await Promise.all([loadProjects(), loadRuns()]); toast("项目、任务产物及未共享资产已删除");
  } catch (error) { toast(error.message, true); }
});

$("#open-wizard").addEventListener("click", openWizard);
$("#refresh-project").addEventListener("click", () => selectProject(state.currentProject.id, false).catch((error) => toast(error.message, true)));
$("#refresh-runs").addEventListener("click", () => loadRuns().catch((error) => toast(error.message, true)));
$("#refresh-all-runs").addEventListener("click", () => loadRuns().catch((error) => toast(error.message, true)));
$("#run-filter").addEventListener("change", renderAllRuns);
$("#run-search").addEventListener("input", renderAllRuns);
$$(".topnav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));

$("#wizard-model").addEventListener("change", () => updateWizardModel(true));
$("#wizard-calibration").addEventListener("change", () => updateCalibrationSelection(true));
$("#compile-mode").addEventListener("change", toggleCompileMode);
$("#l2m-mode").addEventListener("change", toggleL2mCustom);
["target-shape", "train-layout", "train-type", "resize-short", "recipe-mean", "recipe-std"].forEach((id) => $(`#${id}`).addEventListener("change", () => renderCalibrationPreview().catch(() => {})));
WIZARD_FIELDS.forEach((id) => $(`#${id}`).addEventListener("change", () => { state.wizardPreview = null; saveWizardDraft(); }));
$("#wizard-next").addEventListener("click", async () => {
  try {
    validateWizardStep(state.wizardStep);
    if (state.wizardStep === 5) await fetchYamlPreview();
    setWizardStep(Math.min(6, state.wizardStep + 1));
  } catch (error) { toast(error.message, true); }
});
$("#wizard-back").addEventListener("click", () => setWizardStep(Math.max(1, state.wizardStep - 1)));
$("#refresh-yaml").addEventListener("click", () => fetchYamlPreview().catch((error) => toast(error.message, true)));
$("#wizard-submit").addEventListener("click", async () => {
  const button = $("#wizard-submit"); button.disabled = true;
  try {
    validateWizardStep(6); await fetchYamlPreview();
    const submission = await api("/api/v1/conversion-runs", {method: "POST", body: JSON.stringify(conversionPayload())});
    try { localStorage.removeItem(draftKey()); } catch (_error) { /* ignored */ }
    $("#wizard-dialog").close(); toast(`转换任务 ${submission.run_id.slice(0, 8)} 已进入持久队列`); await loadRuns(); await openRun(submission.run_id);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$$("[data-run-tab]").forEach((button) => button.addEventListener("click", () => showRunTab(button.dataset.runTab)));
$("#pause-logs").addEventListener("click", () => {
  state.logsPaused = !state.logsPaused;
  $("#pause-logs").textContent = state.logsPaused ? "继续" : "暂停";
  if (state.logsPaused) { state.logEventSource?.close(); state.logEventSource = null; } else connectLogStream();
});
$("#log-search").addEventListener("input", applyLogSearch);
$("#cancel-run").addEventListener("click", async () => {
  if (!state.currentRun || !window.confirm("确认取消当前 Attempt？已经产生的日志和可识别产物会保留。")) return;
  try { await api(`/api/v1/runs/${state.currentRun.id}/cancel`, {method: "POST"}); toast("取消请求已发送"); await refreshRunDetail(); } catch (error) { toast(error.message, true); }
});
$("#retry-run").addEventListener("click", async () => {
  if (!state.currentRun || !window.confirm("使用完全相同的冻结快照创建新 Attempt？")) return;
  try { const result = await api(`/api/v1/runs/${state.currentRun.id}/retry`, {method: "POST"}); toast(`Attempt ${result.attempt} 已入队`); closeRunStreams(); await refreshRunDetail(); connectRunStreams(); } catch (error) { toast(error.message, true); }
});
$("#close-report").addEventListener("click", () => { $("#report-frame").src = "about:blank"; $("#report-preview").classList.add("hidden"); });

$("#system-status").addEventListener("click", () => $("#preflight-dialog").showModal());
$("#welcome-preflight").addEventListener("click", () => $("#preflight-dialog").showModal());
$("#rerun-smoke").addEventListener("click", () => runSmokeTest(true).catch((error) => toast(error.message, true)));
$$("[data-close]").forEach((button) => button.addEventListener("click", () => {
  const dialog = $(`#${button.dataset.close}`);
  if (dialog.id === "run-dialog") closeRunStreams();
  dialog.close();
}));
$("#run-dialog").addEventListener("close", closeRunStreams);

async function initialize() {
  try {
    const session = await api("/api/v1/session");
    state.csrf = session.csrf_token;
    $("#upload-limit").textContent = formatBytes(session.max_upload_bytes);
    const [profiles, preflight, projects, runs] = await Promise.all([
      api("/api/v1/profiles"), api("/api/v1/system/preflight"), api("/api/v1/projects"), api("/api/v1/runs"),
    ]);
    state.profiles = profiles; state.preflight = preflight; state.projects = projects; state.runs = runs;
    renderProfileCards(); renderPreflight(); renderProjectList(); renderRunSummary(); renderAllRuns();
    const queued = runs.filter((run) => run.status === "QUEUED").length;
    const active = runs.filter((run) => ACTIVE_STATUSES.has(run.status) && run.status !== "QUEUED").length;
    $("#queue-status").textContent = `队列 ${queued} · 运行 ${active}`;
    if (projects.length) await selectProject(projects[0].id, false);
    await refreshPreflight(true);
    setInterval(periodicRefresh, 3500);
  } catch (error) {
    toast(error.message, true);
  }
}

initialize();
