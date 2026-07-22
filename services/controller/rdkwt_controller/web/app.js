"use strict";

const ACTIVE_STATUSES = new Set([
  "QUEUED", "PROVISIONING", "RUNNING", "INSPECTING", "CHECKING",
  "PREPROCESSING", "COMPILING", "VERIFYING", "COLLECTING", "CANCELLING",
  "CONNECTING", "UPLOADING", "CLEANING",
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
  devices: [],
  boardRuns: [],
  currentBoardRun: null,
  boardEventSource: null,
  boardUploadLimit: 0,
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
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || `请求失败（${response.status}）`);
    if (payload && typeof payload === "object") Object.assign(error, payload);
    throw error;
  }
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
  if (["FAILED", "INTERRUPTED", "BLOCKED", "ERROR"].includes(status)) return "bad";
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

function calibrationVersion(versionId) {
  return projectCalibrations().find(({version}) => version.id === versionId)?.version || null;
}

function calibrationSourceLabel(sourceType) {
  return {npy: "直接 NPY", npy_multi: "多输入 NPY ZIP", images: "图片"}[sourceType] || sourceType;
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
    const sourceLabel = calibrationSourceLabel(version.source_type);
    const tensorMetadata = version.source_type === "npy" && version.validation_report?.shape
      ? ` · [${version.validation_report.shape.join(", ")}] ${version.validation_report.dtype}`
      : version.source_type === "npy_multi" && version.validation_report?.inputs
        ? ` · ${version.validation_report.inputs.length} inputs`
        : "";
    copy.append(
      el("strong", "", calibrationSet.name),
      el("small", "", `${sourceLabel} · ${version.sample_count} 份${tensorMetadata} · ${version.status === "READY" ? "清单已冻结" : "可继续上传"}${warningCount ? ` · ${warningCount} 警告` : ""}`),
    );
    row.append(copy, el("span", `asset-tag${version.status === "DRAFT" ? " draft" : ""}`, version.status));
    list.append(row);
    const option = new Option(`${calibrationSet.name} · ${sourceLabel} · ${version.sample_count} 份 · ${version.status}`, version.id);
    option.disabled = version.status !== "DRAFT";
    versionSelect.add(option);
  }
  if ([...versionSelect.options].some((option) => option.value === selected)) versionSelect.value = selected;
  if (!projectCalibrations().length) list.append(el("div", "empty-state", "新建草稿后可批量上传图片或直接 NPY"));
  syncCalibrationUploadMode();
  populateWizardCalibrations();
}

function syncCalibrationUploadMode() {
  const selected = calibrationVersion($("#calibration-version-select").value);
  const sourceType = selected?.source_type || "images";
  const npy = sourceType === "npy";
  const multi = sourceType === "npy_multi";
  const sampleInput = $("#sample-files");
  const uploadButton = $("#sample-upload-form button[type=submit]");
  sampleInput.accept = npy ? ".npy" : ".jpg,.jpeg,.png,.bmp";
  sampleInput.disabled = multi;
  sampleInput.required = !multi;
  uploadButton.disabled = multi;
  $("#sample-archive").accept = ".zip";
  $("#sample-drop-title").textContent = multi
    ? "请使用 ZIP 导入多输入 NPY"
    : npy ? "选择直接 NPY 文件" : "选择 JPEG / PNG / BMP";
  $("#sample-file-label").textContent = !selected
    ? "请先选择一个 DRAFT 校准版本"
    : multi
      ? "ZIP 必须严格使用 <input_name>/<sample>.npy，且各输入样本名完全对齐"
      : `可一次选择多份${npy ? " NPY" : "图片"}，按选择顺序登记`;
}

async function loadRuns() {
  state.runs = await api("/api/v1/runs");
  renderRunSummary();
  renderProjectRuns();
  renderAllRuns();
  populateComparisonRuns();
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

function populateComparisonRuns() {
  const select = $("#comparison-runs");
  if (!select) return;
  const selected = new Set([...select.selectedOptions].map((option) => option.value));
  select.replaceChildren();
  state.runs
    .filter((run) => run.kind === "CONVERSION" && run.status === "SUCCEEDED")
    .forEach((run) => {
      const project = state.projects.find((item) => item.id === run.project_id);
      const option = new Option(
        `${run.profile_id} · 模型 ${run.model_version_id.slice(0, 8)} · ${run.runner_mode || "cpu"} · ${run.id.slice(0, 8)} · ${project?.name || "未知项目"}`,
        run.id,
      );
      option.selected = selected.has(run.id);
      select.add(option);
    });
  updateComparisonButton();
}

function updateComparisonButton() {
  const count = $("#comparison-runs")?.selectedOptions.length || 0;
  $("#compare-runs").disabled = count < 2 || count > 4;
}

function comparisonValue(field, value) {
  if (value === null || value === undefined) return "—";
  if (["hbm_size_bytes", "ddr_bytes_per_run", "l2m_bytes_per_run"].includes(field)) return formatBytes(value);
  if (["total_duration_ms", "hbruntime_duration_ms"].includes(field)) return formatDuration(value);
  if (field === "cache_hit") return value ? "命中" : "未命中";
  return String(value);
}

function renderComparison(comparison) {
  const labels = {
    profile_id: "目标平台", runner_mode: "Runner", calibration_version_id: "校准版本",
    calibration_source_type: "校准源", calibration_algorithm: "校准算法", sample_limit: "样本数",
    compile_mode: "编译模式", core_num: "Core", optimize_level: "优化级别", max_l2m_size: "L2M",
    hbm_size_bytes: "HBM 大小", fps: "静态 FPS", latency_us: "静态延迟 (μs)",
    ddr_bytes_per_run: "DDR / run", l2m_bytes_per_run: "L2M / run",
    minimum_quantized_cosine: "最低量化 Cosine", minimum_verifier_cosine: "最低验证 Cosine",
    hbruntime_duration_ms: "HBRuntime 耗时", total_duration_ms: "总阶段耗时", cache_hit: "缓存",
  };
  const root = $("#comparison-result");
  root.replaceChildren();
  $("#comparison-subtitle").textContent = `模型 ${comparison.model_version_id} · ${comparison.rows.length} 个成功任务`;
  const table = el("table", "comparison-table");
  const head = el("thead");
  const headRow = el("tr");
  headRow.append(el("th", "", "指标"));
  comparison.rows.forEach((row) => headRow.append(el("th", "", `${row.profile_id} · ${row.run_id.slice(0, 8)}`)));
  head.append(headRow);
  const body = el("tbody");
  Object.keys(labels).forEach((field) => {
    const row = el("tr", Object.hasOwn(comparison.differences, field) ? "different" : "");
    row.append(el("th", "", labels[field]));
    comparison.rows.forEach((item) => row.append(el("td", "", comparisonValue(field, item[field]))));
    body.append(row);
  });
  table.append(head, body);
  root.append(table, el("p", "comparison-note", "高亮行表示任务间存在差异；数值验证指标用于回归判断，不代表最终业务数据集精度。"));
}

function showView(view) {
  state.currentView = view;
  $("#workspace-view").classList.toggle("hidden", view !== "workspace");
  $("#runs-view").classList.toggle("hidden", view !== "runs");
  $("#devices-view").classList.toggle("hidden", view !== "devices");
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
  syncRunnerMode();
}

function checkLabel(id) {
  return {
    "docker-engine": "Docker Engine", "runner-image": "Runner 镜像",
    "storage-state": "状态存储", "storage-assets": "资产存储", "storage-runs": "任务存储",
    "storage-cache": "编译缓存", "gpu-runner": "GPU Runner（可选）",
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
  const inputCount = selectedModelVersion()?.inspection?.inputs?.length || 0;
  for (const {calibrationSet, version} of projectCalibrations()) {
    const sourceMatches = inputCount > 1 ? version.source_type === "npy_multi" : version.source_type !== "npy_multi";
    if (sourceMatches && version.status === "READY" && version.sample_count >= 20) {
      select.add(new Option(`${calibrationSet.name} · ${calibrationSourceLabel(version.source_type)} · ${version.sample_count} 份`, version.id));
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
  syncRunnerMode();
  saveWizardDraft();
}

function syncRunnerMode() {
  const select = $("#runner-mode");
  if (!select) return;
  const gpu = state.preflight?.details?.gpu || {};
  const gpuOption = select.querySelector('option[value="gpu"]');
  gpuOption.disabled = !gpu.available;
  if (!gpu.available && select.value === "gpu") select.value = "cpu";
  $("#runner-mode-note").textContent = gpu.available
    ? `GPU Runner 已就绪：${gpu.image?.immutable_id || gpu.image?.reference || "固定镜像"}`
    : `当前仅使用 CPU。${gpu.message || "GPU Runner 未启用，不影响转换。"}`;
}

const WIZARD_FIELDS = [
  "wizard-model", "wizard-profile", "input-name", "target-shape", "train-layout", "train-type", "runtime-type",
  "input-mean", "input-scale", "input-std", "wizard-calibration", "sample-limit", "calibration-algorithm",
  "recipe-id", "resize-short", "recipe-mean", "recipe-std", "output-prefix", "compile-mode", "balance-factor",
  "core-num", "l2m-mode", "l2m-custom", "optimize-level", "jobs", "runner-mode", "cache-mode",
  "verification-mode", "compare-digits",
];

function draftKey() {
  return `rdkwt:m2-draft:${state.currentProject?.id || "none"}`;
}

function saveWizardDraft() {
  if (!state.currentProject) return;
  const fields = {};
  WIZARD_FIELDS.forEach((id) => { fields[id] = $(`#${id}`).value; });
  const additionalInputs = [...$$("#additional-input-configs .input-config-card")].map((card) => ({
    name: card.querySelector(".multi-input-name").value,
    target_shape: card.querySelector(".multi-target-shape").value,
    train_layout: card.querySelector(".multi-train-layout").value,
    train_type: card.querySelector(".multi-train-type").value,
    runtime_type: card.querySelector(".multi-runtime-type").value,
    mean: card.querySelector(".multi-input-mean").value,
    scale: card.querySelector(".multi-input-scale").value,
    std: card.querySelector(".multi-input-std").value,
  }));
  try {
    localStorage.setItem(draftKey(), JSON.stringify({version: 2, saved_at: new Date().toISOString(), fields, additional_inputs: additionalInputs}));
    $("#wizard-draft-status").textContent = `草稿已保存 · ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  } catch (_error) {
    $("#wizard-draft-status").textContent = "浏览器未允许保存草稿";
  }
}

function loadWizardDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(draftKey())); } catch (_error) { draft = null; }
  if ([1, 2].includes(draft?.version)) {
    WIZARD_FIELDS.forEach((id) => {
      if (draft.fields[id] !== undefined) $(`#${id}`).value = draft.fields[id];
    });
    $("#wizard-draft-status").textContent = `已恢复 ${formatDate(draft.saved_at)} 的草稿`;
  }
  const readyModel = projectModels().find(({version}) => version.compatibility_status === "READY");
  if (!$("#wizard-model").value && readyModel) $("#wizard-model").value = readyModel.version.id;
  const profileId = state.profiles.some((profile) => profile.profile_id === $("#wizard-profile").value)
    ? $("#wizard-profile").value : state.profiles[0]?.profile_id;
  selectProfile(profileId, false);
  const canKeepDraftInput = [1, 2].includes(draft?.version)
    && draft.fields["wizard-model"] === $("#wizard-model").value
    && Boolean($("#input-name").value && $("#target-shape").value);
  updateWizardModel(!canKeepDraftInput, canKeepDraftInput ? draft.additional_inputs : null);
  populateWizardCalibrations();
  const readyCalibration = projectCalibrations().find(({version}) => {
    const inputs = selectedModelVersion()?.inspection?.inputs?.length || 0;
    return version.status === "READY" && version.sample_count >= 20
      && (inputs > 1 ? version.source_type === "npy_multi" : version.source_type !== "npy_multi");
  });
  const draftCalibration = draft?.fields?.["wizard-calibration"];
  if (draftCalibration && [...$("#wizard-calibration").options].some((option) => option.value === draftCalibration)) {
    $("#wizard-calibration").value = draftCalibration;
  } else if (!$("#wizard-calibration").value && readyCalibration) {
    $("#wizard-calibration").value = readyCalibration.version.id;
  }
  updateCalibrationSelection(false);
  toggleCompileMode();
  toggleL2mCustom();
  syncRunnerMode();
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

const TRAIN_TYPE_OPTIONS = [
  ["rgb", "RGB"], ["bgr", "BGR"], ["gray", "Gray"], ["yuv444", "YUV444"], ["featuremap", "Featuremap"],
];
const RUNTIME_TYPE_OPTIONS = [
  ["nv12", "NV12"], ["rgb", "RGB"], ["bgr", "BGR"], ["yuv444", "YUV444"], ["gray", "Gray"], ["featuremap", "Featuremap"],
];

function inputDefaults(input) {
  const shape = input.shape || [];
  const rankFour = shape.length === 4;
  const channels = rankFour && Number.isInteger(shape[1]) ? shape[1] : null;
  const trainType = channels === 3 ? "rgb" : channels === 1 ? "gray" : "featuremap";
  const evenImage = rankFour && Number.isInteger(shape[2]) && Number.isInteger(shape[3])
    && shape[2] % 2 === 0 && shape[3] % 2 === 0;
  return {
    name: input.name,
    target_shape: shape.map((item) => Number.isInteger(item) && item > 0 ? item : "").join(","),
    train_layout: "NCHW",
    train_type: trainType,
    runtime_type: trainType === "rgb" && evenImage ? "nv12" : trainType === "gray" ? "gray" : "featuremap",
    mean: "", scale: "", std: "",
  };
}

function selectControl(className, options, value) {
  const select = el("select", className);
  options.forEach(([optionValue, label]) => select.add(new Option(label, optionValue)));
  select.value = value;
  return select;
}

function labeledControl(label, control) {
  const wrapper = el("label", "", label);
  wrapper.append(control);
  return wrapper;
}

function renderAdditionalInputs(inputs, draftInputs = null) {
  const root = $("#additional-input-configs");
  root.replaceChildren();
  inputs.slice(1).forEach((input, offset) => {
    const index = offset + 1;
    const saved = draftInputs?.[offset];
    const defaults = inputDefaults(input);
    const values = saved?.name === input.name ? {...defaults, ...saved} : defaults;
    const card = el("div", "input-config-card");
    card.dataset.inputIndex = String(index);
    card.append(el("h4", "", `Input ${index + 1} · ${input.name}`));
    const grid = el("div", "form-grid three");
    const name = el("input", "multi-input-name"); name.readOnly = true; name.value = input.name;
    const shape = el("input", "multi-target-shape"); shape.required = true; shape.placeholder = "1,16"; shape.value = values.target_shape;
    const mean = el("input", "multi-input-mean"); mean.placeholder = "可选"; mean.value = values.mean || "";
    const scale = el("input", "multi-input-scale"); scale.placeholder = "可选"; scale.value = values.scale || "";
    const std = el("input", "multi-input-std"); std.placeholder = "可选"; std.value = values.std || "";
    grid.append(
      labeledControl("输入节点", name),
      labeledControl("目标 Shape", shape),
      labeledControl("训练布局", selectControl("multi-train-layout", [["NCHW", "NCHW"], ["NHWC", "NHWC"]], values.train_layout)),
      labeledControl("训练输入", selectControl("multi-train-type", TRAIN_TYPE_OPTIONS, values.train_type)),
      labeledControl("Runtime 输入", selectControl("multi-runtime-type", RUNTIME_TYPE_OPTIONS, values.runtime_type)),
      labeledControl("Mean", mean), labeledControl("Scale", scale), labeledControl("Std（可选）", std),
    );
    card.append(grid);
    root.append(card);
  });
}

function updateWizardModel(overwrite = true, draftInputs = null) {
  const version = selectedModelVersion();
  const root = $("#wizard-model-info");
  root.replaceChildren();
  if (!version?.inspection) {
    root.className = "inspection-card empty-state";
    root.textContent = version ? "该模型尚未完成检查" : "请选择模型";
    $("#additional-input-configs").replaceChildren();
    populateWizardCalibrations();
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
    const defaults = inputDefaults(input);
    $("#input-name").value = defaults.name;
    $("#target-shape").value = defaults.target_shape;
    $("#train-layout").value = defaults.train_layout;
    $("#train-type").value = defaults.train_type;
    $("#runtime-type").value = defaults.runtime_type;
    $("#input-mean").value = defaults.mean;
    $("#input-scale").value = defaults.scale;
    $("#input-std").value = defaults.std;
  }
  renderAdditionalInputs(inspection.inputs || [], draftInputs);
  populateWizardCalibrations();
  saveWizardDraft();
}

function parseNumbers(value, name, allowEmpty = true) {
  if (!value.trim() && allowEmpty) return [];
  const values = value.split(",").map((item) => Number(item.trim()));
  if (!values.length || values.some((item) => !Number.isFinite(item))) throw new Error(`${name} 必须是逗号分隔的数字`);
  return values;
}

function parseShape(control = $("#target-shape"), name = "目标 Shape") {
  const values = control.value.split(",").map((item) => Number(item.trim()));
  if (values.length < 1 || values.length > 4 || values.some((item) => !Number.isInteger(item) || item < 1)) {
    throw new Error(`${name} 必须包含 1～4 个正整数`);
  }
  return values;
}

function inputGeometry(input = configuredInputs()[0]) {
  const shape = input.target_shape;
  if (shape.length !== 4) throw new Error(`${input.name} 只有 Rank 4 输入可使用图片/NV12 几何配置`);
  const nchw = input.train_layout === "NCHW";
  return {shape, channels: nchw ? shape[1] : shape[3], height: nchw ? shape[2] : shape[1], width: nchw ? shape[3] : shape[2]};
}

function inputFromControls(controls, index) {
  return {
    name: controls.name.value,
    target_shape: parseShape(controls.shape, `Input ${index + 1} 目标 Shape`),
    train_type: controls.trainType.value,
    train_layout: controls.trainLayout.value,
    runtime_type: controls.runtimeType.value,
    normalization: {
      mean: parseNumbers(controls.mean.value, `Input ${index + 1} Mean`),
      scale: parseNumbers(controls.scale.value, `Input ${index + 1} Scale`),
      std: parseNumbers(controls.std.value, `Input ${index + 1} Std`),
    },
  };
}

function configuredInputs() {
  const first = inputFromControls({
    name: $("#input-name"), shape: $("#target-shape"), trainType: $("#train-type"),
    trainLayout: $("#train-layout"), runtimeType: $("#runtime-type"),
    mean: $("#input-mean"), scale: $("#input-scale"), std: $("#input-std"),
  }, 0);
  const rest = [...$$("#additional-input-configs .input-config-card")].map((card, offset) => inputFromControls({
    name: card.querySelector(".multi-input-name"), shape: card.querySelector(".multi-target-shape"),
    trainType: card.querySelector(".multi-train-type"), trainLayout: card.querySelector(".multi-train-layout"),
    runtimeType: card.querySelector(".multi-runtime-type"), mean: card.querySelector(".multi-input-mean"),
    scale: card.querySelector(".multi-input-scale"), std: card.querySelector(".multi-input-std"),
  }, offset + 1));
  return [first, ...rest];
}

function validateInputConfiguration(input) {
  const shape = input.target_shape;
  if (shape[0] !== 1) throw new Error(`${input.name} 当前仅支持 batch=1`);
  let channels = null; let height = null; let width = null;
  if (shape.length === 4) {
    const nchw = input.train_layout === "NCHW";
    channels = nchw ? shape[1] : shape[3];
    height = nchw ? shape[2] : shape[1];
    width = nchw ? shape[3] : shape[2];
  }
  if (input.train_type !== "featuremap") {
    if (shape.length !== 4) throw new Error(`${input.name} 的 ${input.train_type} 输入要求 Rank 4`);
    const expectedChannels = input.train_type === "gray" ? 1 : 3;
    if (channels !== expectedChannels) throw new Error(`${input.name} 的 ${input.train_type} 要求 channels=${expectedChannels}`);
  }
  if (input.runtime_type === "nv12") {
    if (shape.length !== 4 || channels !== 3 || height % 2 || width % 2) {
      throw new Error(`${input.name} 的 NV12 要求 Rank 4、channels=3 且宽高为偶数`);
    }
  }
  Object.entries(input.normalization).forEach(([field, values]) => {
    const label = {mean: "Mean", scale: "Scale", std: "Std"}[field];
    const allowed = channels === null ? [0, 1] : [0, 1, channels];
    if (!allowed.includes(values.length)) throw new Error(`${input.name} ${label} 数量必须为 ${allowed.join("、")}`);
  });
}

function validateWizardStep(step) {
  if (step === 1 && selectedModelVersion()?.compatibility_status !== "READY") throw new Error("请选择已通过检查的模型");
  if (step === 2) {
    if (!$("#wizard-profile").value) throw new Error("请选择目标平台");
    if ($("#runner-mode").value === "gpu" && $("#runner-mode option[value=gpu]").disabled) {
      throw new Error("当前主机没有可用的 GPU Runner，请使用 CPU");
    }
  }
  if (step === 3) {
    const inputs = configuredInputs();
    const inspected = selectedModelVersion()?.inspection?.inputs || [];
    if (inputs.length !== inspected.length) throw new Error("输入配置数量必须与 ONNX 模型一致");
    inputs.forEach(validateInputConfiguration);
  }
  if (step === 4) {
    const selected = calibrationVersion($("#wizard-calibration").value);
    const limit = Number($("#sample-limit").value);
    if (!selected || selected.status !== "READY") throw new Error("请选择已定稿校准版本");
    if (!Number.isInteger(limit) || limit < 20 || limit > selected.sample_count) throw new Error(`使用样本数必须在 20～${selected.sample_count} 之间`);
    const inputs = configuredInputs();
    if (selected.source_type === "npy") {
      if (inputs.length !== 1) throw new Error("直接 NPY 只支持单输入模型");
      const expectedShape = inputs[0].target_shape.slice(1);
      const actualShape = selected.validation_report?.shape;
      if (JSON.stringify(actualShape) !== JSON.stringify(expectedShape)) {
        throw new Error(`直接 NPY Shape [${actualShape?.join(", ") || "未知"}] 必须匹配模型去除 batch 后的 [${expectedShape.join(", ")}]`);
      }
      if (!selected.validation_report?.dtype) throw new Error("直接 NPY 校准报告缺少 dtype");
    } else if (selected.source_type === "npy_multi") {
      if (inputs.length < 2) throw new Error("多输入 NPY ZIP 至少需要两个模型输入");
      const reports = new Map((selected.validation_report?.inputs || []).map((item) => [item.name, item]));
      inputs.forEach((input) => {
        const report = reports.get(input.name);
        const expected = input.target_shape.slice(1);
        if (!report || JSON.stringify(report.shape) !== JSON.stringify(expected) || !report.dtype) {
          throw new Error(`${input.name} 的 NPY 必须为 batch-free Shape [${expected.join(", ")}] 且包含 dtype 报告`);
        }
      });
    } else {
      if (inputs.length !== 1) throw new Error("图片校准只支持单输入模型");
      if (!["rgb", "bgr", "gray"].includes(inputs[0].train_type)) {
        throw new Error("图片校准的训练输入必须是 RGB、BGR 或 Gray");
      }
      const {channels} = inputGeometry(inputs[0]);
      if (parseNumbers($("#recipe-mean").value, "Recipe Mean", false).length !== channels) throw new Error(`Recipe Mean 必须包含 ${channels} 项`);
      const std = parseNumbers($("#recipe-std").value, "Recipe Std", false);
      if (std.length !== channels || std.some((item) => item === 0)) throw new Error(`Recipe Std 必须包含 ${channels} 个非零值`);
    }
  }
  if (step === 5) {
    if (!$("#output-prefix").checkValidity()) throw new Error("输出前缀格式不合法");
    if ($("#compile-mode").value === "balance" && !$("#balance-factor").checkValidity()) throw new Error("balance 模式需要 0～100 的 Balance factor");
    if (!$("#compare-digits").checkValidity()) throw new Error("比较小数位必须在 1～12 之间");
  }
  if (step === 6 && !$("#confirm-snapshot").checked) throw new Error("请确认冻结配置后再提交");
}

function conversionPayload() {
  const l2m = $("#l2m-mode").value;
  const selectedCalibration = calibrationVersion($("#wizard-calibration").value);
  const calibration = {algorithm: $("#calibration-algorithm").value};
  if (selectedCalibration?.source_type === "images") {
    calibration.recipe = {
      id: $("#recipe-id").value,
      resize_short: Number($("#resize-short").value),
      mean: parseNumbers($("#recipe-mean").value, "Recipe Mean", false),
      std: parseNumbers($("#recipe-std").value, "Recipe Std", false),
    };
  }
  const inputs = configuredInputs();
  const payload = {
    profile_id: $("#wizard-profile").value,
    model_version_id: $("#wizard-model").value,
    calibration_version_id: $("#wizard-calibration").value,
    output_prefix: $("#output-prefix").value,
    calibration,
    runner_mode: $("#runner-mode").value,
    cache_mode: $("#cache-mode").value,
    verification: {mode: $("#verification-mode").value, compare_digits: Number($("#compare-digits").value)},
    core_num: Number($("#core-num").value),
    max_l2m_size: l2m === "auto" ? "auto" : l2m === "custom" ? Number($("#l2m-custom").value) : 0,
    compile_mode: $("#compile-mode").value,
    balance_factor: $("#compile-mode").value === "balance" ? Number($("#balance-factor").value) : null,
    optimize_level: $("#optimize-level").value,
    sample_limit: Number($("#sample-limit").value),
    jobs: Number($("#jobs").value),
  };
  if (inputs.length === 1) payload.input = inputs[0];
  else payload.inputs = inputs;
  return payload;
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
  const selected = calibrationVersion($("#wizard-calibration").value);
  const direct = ["npy", "npy_multi"].includes(selected?.source_type);
  if (selected) $("#sample-limit").value = Math.min(Math.max(20, Number($("#sample-limit").value) || 20), selected.sample_count);
  $("#image-recipe-fields").classList.toggle("hidden", direct);
  $("#calibration-preview").classList.toggle("npy-mode", direct);
  $("#calibration-step-copy").textContent = direct
    ? "直接 NPY 不执行图片 Recipe；提交前会逐输入核对冻结的 Shape、dtype、样本对齐、哈希和有限数值统计。"
    : "选择冻结的校准版本，并检查第一份样本的中心裁剪结果和归一化统计。";
  renderCalibrationPreview().catch((error) => {
    $("#preview-stats").replaceChildren(el("span", "", `预览失败：${error.message}`));
  });
  if (save) saveWizardDraft();
}

async function renderCalibrationPreview() {
  const versionId = $("#wizard-calibration").value;
  const selected = calibrationVersion(versionId);
  const image = $("#preview-original");
  const canvas = $("#preview-processed");
  const statsRoot = $("#preview-stats");
  if (!versionId) {
    image.removeAttribute("src");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    statsRoot.replaceChildren(el("span", "", "选择校准版本后生成预览"));
    return;
  }
  if (["npy", "npy_multi"].includes(selected?.source_type)) {
    image.removeAttribute("src");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    const report = selected.validation_report || {};
    const formatStatistic = (value) => Number.isFinite(Number(value)) ? Number(value).toPrecision(6) : "—";
    statsRoot.replaceChildren();
    const reports = selected.source_type === "npy_multi"
      ? (report.inputs || [])
      : [{name: "Input 1", shape: report.shape, dtype: report.dtype, first_sample_statistics: report.first_sample_statistics}];
    statsRoot.append(keyValue("数据路径", selected.source_type === "npy_multi" ? "多输入 NPY ZIP（样本名已对齐）" : "直接 NPY（无图片 Recipe）"));
    reports.forEach((inputReport) => {
      const statistics = inputReport.first_sample_statistics || {};
      const block = el("div", "npy-input-statistics");
      block.append(
        el("strong", "", inputReport.name || "Input"),
        keyValue("Shape / dtype", `[${(inputReport.shape || []).join(", ")}] · ${inputReport.dtype || "—"}`),
        keyValue("第一份 min / max", `${formatStatistic(statistics.minimum)} / ${formatStatistic(statistics.maximum)}`),
        keyValue("第一份 mean / std", `${formatStatistic(statistics.mean)} / ${formatStatistic(statistics.standard_deviation)}`),
      );
      statsRoot.append(block);
    });
    return;
  }
  const {channels, height, width, shape} = inputGeometry(configuredInputs()[0]);
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
  const verification = summary.verification || {};
  const verifierMinimum = verification.hb_verifier?.minimum_cosine;
  const cache = run.cache || summary.cache || {};
  const hbm = summary.hbm;
  const outputCosine = quant.output_cosines?.[0];
  const cards = el("div", "summary-cards");
  cards.append(
    summaryCard("目标平台", run.profile_id, run.request?.configuration?.target_profile?.profile?.march || ""),
    summaryCard("HBM", hbm ? formatBytes(hbm.size_bytes) : "—", hbm?.relative_path || "尚未生成"),
    summaryCard("总阶段耗时", formatDuration(summary.total_duration_ms), `${summary.steps?.length || 0} 个已记录阶段`),
    summaryCard("静态性能", perf.fps !== undefined && perf.fps !== null ? `${perf.fps} FPS` : "—", perf.latency_us !== undefined && perf.latency_us !== null ? `${perf.latency_us} μs` : "无静态报告"),
    summaryCard("数值验证", verifierMinimum ? String(verifierMinimum.cosine_similarity) : verification.enabled === false ? "已关闭" : "—", verifierMinimum?.tensor_name || "HBRuntime + hb_verifier"),
  );
  root.append(cards);
  const grid = el("div", "detail-grid");
  const execution = el("div", "detail-card"); execution.append(el("h3", "", "执行快照"));
  execution.append(
    keyValue("状态 / 阶段", `${run.status} / ${attempt.stage}`), keyValue("Attempt", `${attempt.number}（${attempt.recovered ? "重启后恢复" : "正常启动"}）`),
    keyValue("Runner / image", `${run.runner_mode || "cpu"} / ${run.runner_image?.immutable_id || "—"}`), keyValue("应用 / 合约", `${run.app_version} / ${run.contract_version}`),
    keyValue("编译缓存", cache.key ? `${cache.hit ? "HIT" : "MISS"} / ${cache.key.slice(0, 16)}…` : "关闭"),
    keyValue("容器退出码", attempt.exit_code === null ? "—" : String(attempt.exit_code)), keyValue("Profile SHA", run.profile_sha256),
  );
  const metrics = el("div", "detail-card"); metrics.append(el("h3", "", "质量与资源"));
  metrics.append(
    keyValue("输出 Quantized Cosine", outputCosine ? `${outputCosine.name}: ${outputCosine.quantized_cosine}` : "—"),
    keyValue("最低节点 Cosine", quant.minimum_node ? `${quant.minimum_node.name}: ${quant.minimum_node.quantized_cosine}` : "—"),
    keyValue("最低内存估计", perf.minimum_memory_bytes ? formatBytes(perf.minimum_memory_bytes) : "—"),
    keyValue("DDR / run", perf.ddr_bytes_per_run ? formatBytes(perf.ddr_bytes_per_run) : "—"),
    keyValue("hb_verifier 最低 Cosine", verifierMinimum ? `${verifierMinimum.tensor_name}: ${verifierMinimum.cosine_similarity}` : "—"),
    keyValue("HBRuntime 推理耗时", verification.hbruntime ? formatDuration(verification.hbruntime.duration_ms) : "—"),
    keyValue("警告 / 建议产物", `${summary.warning_count || 0} / ${summary.advice_artifact_count || 0}`),
  );
  metrics.append(el("p", "cosine-note", "HBRuntime 与 hb_verifier 是真实数值冒烟和模型阶段回归验证，但仍不等同于完整业务数据集精度。"));
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

function deviceById(deviceId) {
  return state.devices.find((device) => device.id === deviceId) || null;
}

async function loadBoardData() {
  [state.devices, state.boardRuns] = await Promise.all([
    api("/api/v1/devices"),
    api("/api/v1/board-runs"),
  ]);
  renderDevices();
  renderBoardRuns();
  populateBoardRunForm();
}

function renderDevices() {
  $("#devices-total").textContent = state.devices.length;
  $("#devices-ready").textContent = state.devices.filter((device) => device.status === "READY").length;
  const root = $("#device-list");
  root.replaceChildren();
  if (!state.devices.length) {
    root.append(el("div", "empty-state", "尚未添加开发板。凭据将保存在本机加密 Secret Store。"));
    return;
  }
  state.devices.forEach((device) => {
    const card = el("article", "device-card");
    const heading = el("div", "device-card-heading");
    const identity = el("div");
    identity.append(
      el("span", "eyebrow", device.platform.toUpperCase()),
      el("h3", "", device.name),
      el("code", "", `${device.user}@${device.host}:${device.port}`),
    );
    heading.append(identity, statusPill(device.status));
    const facts = el("div", "device-facts");
    const probe = device.probe || {};
    const values = [
      ["Host Key", device.host_key_fingerprint || "等待首次确认"],
      ["检测平台", device.detected_platform || "—"],
      ["hrt_model_exec", probe.hrt_model_exec_version || "—"],
      ["/tmp 可用", probe.disk?.free_bytes ? formatBytes(probe.disk.free_bytes) : "—"],
    ];
    values.forEach(([label, value]) => {
      const item = el("div");
      item.append(el("span", "", label), el("code", "", value));
      facts.append(item);
    });
    if (device.last_probe_error) card.append(el("p", "device-error", device.last_probe_error));
    const actions = el("div", "device-actions");
    const probeButton = el("button", "button ghost small", device.status === "READY" ? "重新探测" : "探测并验证");
    probeButton.type = "button";
    probeButton.addEventListener("click", () => probeDevice(device.id));
    const runButton = el("button", "button secondary small", "板端验证");
    runButton.type = "button";
    runButton.disabled = device.status !== "READY";
    runButton.addEventListener("click", () => openBoardRunCreate(device.id));
    const deleteButton = el("button", "button danger small", "删除");
    deleteButton.type = "button";
    deleteButton.addEventListener("click", () => deleteDevice(device));
    actions.append(probeButton, runButton, deleteButton);
    card.append(heading, facts, actions);
    root.append(card);
  });
}

async function probeDevice(deviceId) {
  try {
    const device = await api(`/api/v1/devices/${deviceId}/probe`, {method: "POST"});
    toast(`${device.name} 已通过 SSH / SFTP 与平台探测`);
    await loadBoardData();
  } catch (error) {
    if (error.code === "HOST_KEY_UNTRUSTED" && error.observed_fingerprint) {
      const accepted = window.confirm(
        `首次连接观察到 SSH Host Key：\n\n${error.observed_fingerprint}\n\n请通过可信渠道核对。确认保存并重新探测吗？`,
      );
      if (accepted) {
        try {
          await api(`/api/v1/devices/${deviceId}`, {
            method: "PATCH",
            body: JSON.stringify({host_key_fingerprint: error.observed_fingerprint}),
          });
          await probeDevice(deviceId);
          return;
        } catch (trustError) {
          toast(trustError.message, true);
        }
      }
    } else {
      toast(error.message, true);
    }
    await loadBoardData().catch(() => {});
  }
}

async function deleteDevice(device) {
  if (!window.confirm(`删除开发板“${device.name}”及其本机加密凭据？历史板端任务会保留。`)) return;
  try {
    await api(`/api/v1/devices/${device.id}`, {method: "DELETE"});
    toast("设备与加密凭据已删除");
    await loadBoardData();
  } catch (error) {
    toast(error.message, true);
  }
}

function populateBoardRunForm(preferredDeviceId = $("#board-device")?.value) {
  const deviceSelect = $("#board-device");
  const conversionSelect = $("#board-conversion");
  if (!deviceSelect || !conversionSelect) return;
  const selectedDevice = preferredDeviceId || deviceSelect.value;
  deviceSelect.replaceChildren(new Option("选择已就绪设备", ""));
  state.devices.filter((device) => device.status === "READY").forEach((device) => {
    deviceSelect.add(new Option(`${device.name} · ${device.platform.toUpperCase()}`, device.id));
  });
  if ([...deviceSelect.options].some((option) => option.value === selectedDevice)) {
    deviceSelect.value = selectedDevice;
  }
  const device = deviceById(deviceSelect.value);
  const previousRun = conversionSelect.value;
  conversionSelect.replaceChildren(new Option("选择成功转换", ""));
  state.runs
    .filter((run) => run.kind === "CONVERSION" && run.status === "SUCCEEDED")
    .filter((run) => !device || run.profile_id.startsWith(`${device.platform}-`))
    .forEach((run) => conversionSelect.add(new Option(
      `${run.profile_id} · ${run.id.slice(0, 8)} · ${formatDate(run.created_at)}`,
      run.id,
    )));
  if ([...conversionSelect.options].some((option) => option.value === previousRun)) {
    conversionSelect.value = previousRun;
  }
  const coreOne = $("#board-core").querySelector('option[value="2"]');
  coreOne.disabled = !device || device.platform === "s100";
  if (coreOne.disabled && $("#board-core").value === "2") $("#board-core").value = "0";
}

function openBoardRunCreate(deviceId = null) {
  populateBoardRunForm(deviceId);
  if (deviceId) $("#board-device").value = deviceId;
  populateBoardRunForm(deviceId);
  syncBoardRunFields();
  if (!state.devices.some((device) => device.status === "READY")) {
    return toast("请先完成至少一台开发板的探测", true);
  }
  if (!state.runs.some((run) => run.kind === "CONVERSION" && run.status === "SUCCEEDED")) {
    return toast("请先完成至少一个模型转换，生成 HBM", true);
  }
  $("#board-run-create-dialog").showModal();
}

function syncBoardRunFields() {
  const mode = $("#board-mode").value;
  $("#board-core-field").classList.toggle("hidden", mode === "model_info");
  $("#board-input-field").classList.toggle("hidden", mode !== "infer");
  $("#board-perf-fields").classList.toggle("hidden", mode !== "perf");
  $("#board-input").required = mode === "infer";
  const timeMode = $("#board-duration-mode").value === "time";
  $("#board-frame-field").classList.toggle("hidden", timeMode);
  $("#board-time-field").classList.toggle("hidden", !timeMode);
}

function boardModeLabel(mode) {
  return {model_info: "模型信息", infer: "单次推理", perf: "性能测试"}[mode] || mode;
}

function renderBoardRuns() {
  $("#board-runs-total").textContent = state.boardRuns.length;
  $("#board-runs-active").textContent = state.boardRuns.filter((run) => ACTIVE_STATUSES.has(run.status)).length;
  const list = $("#board-run-list");
  list.replaceChildren();
  if (!state.boardRuns.length) {
    list.append(el("div", "empty-state", "设备探测成功、且已有 HBM 后即可运行 model_info / infer / perf。"));
    return;
  }
  state.boardRuns.forEach((run) => {
    const row = el("button", "run-row board-run-row");
    row.type = "button";
    const device = deviceById(run.device_id);
    const identity = el("div", "run-identity");
    identity.append(
      el("strong", "", `${boardModeLabel(run.mode)} · ${device?.name || "已删除设备"}`),
      el("code", "", run.id),
    );
    row.append(
      identity,
      statusPill(run.status),
      el("span", "", run.phase),
      el("span", "", formatDate(run.created_at)),
      el("span", "run-chevron", "›"),
    );
    row.addEventListener("click", () => openBoardRun(run.id));
    list.append(row);
  });
}

function boardMetricLabel(key) {
  return {
    latency_ms: "单次延迟", latency_avg_ms: "平均延迟", latency_min_ms: "最低延迟",
    latency_max_ms: "最高延迟", fps: "实测 FPS", inference_count: "推理次数",
  }[key] || key.replaceAll("_", " ");
}

function boardMetricValue(key, value) {
  if (key.includes("latency") && typeof value === "number") return `${value.toFixed(3)} ms`;
  if (key === "fps" && typeof value === "number") return value.toFixed(3);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value ?? "—");
}

function renderBoardRunDetail(detail) {
  state.currentBoardRun = detail;
  $("#board-run-title").textContent = `${boardModeLabel(detail.mode)} · ${detail.id.slice(0, 8)}`;
  $("#board-run-subtitle").textContent = `${detail.device?.name || "已删除设备"} · ${detail.device?.platform?.toUpperCase() || "—"} · ${detail.phase}`;
  const root = $("#board-run-detail");
  root.replaceChildren();
  const summary = el("div", "summary-cards");
  const summaryValues = [
    ["状态", detail.status], ["阶段", detail.phase], ["HBM", formatBytes(detail.hbm_size_bytes)],
    ["创建时间", formatDate(detail.created_at)],
  ];
  summaryValues.forEach(([label, value]) => {
    const card = el("div", "summary-card");
    card.append(el("span", "", label), el("strong", "", value));
    summary.append(card);
  });
  root.append(summary);
  if (detail.error) {
    const error = el("div", "error-card");
    error.append(el("h3", "", detail.error.code), el("p", "", detail.error.message));
    root.append(error);
  }
  const metrics = detail.result?.metrics || {};
  if (detail.mode === "model_info" && Array.isArray(metrics.models)) {
    const modelCard = el("section", "detail-card board-result-card");
    modelCard.append(el("h3", "", "HBM 模型结构"));
    if (!metrics.models.length) modelCard.append(el("p", "empty-state", "工具未返回可解析的模型结构；请查看原始日志。"));
    metrics.models.forEach((model) => {
      const item = el("div", "board-model-info");
      item.append(
        el("strong", "", model.name || "未命名模型"),
        el("code", "", `${model.inputs?.length || 0} inputs · ${model.outputs?.length || 0} outputs`),
      );
      modelCard.append(item);
    });
    root.append(modelCard);
  } else if (Object.keys(metrics).length) {
    const cards = el("div", "summary-cards board-metric-cards");
    Object.entries(metrics).forEach(([key, value]) => {
      const card = el("div", "summary-card");
      card.append(el("span", "", boardMetricLabel(key)), el("strong", "", boardMetricValue(key, value)));
      cards.append(card);
    });
    root.append(cards);
  }
  const actions = el("section", "detail-card board-result-card");
  actions.append(el("h3", "", "日志与产物"));
  const links = el("div", "board-artifact-links");
  const logLink = el("a", "button ghost small", "原始日志");
  logLink.href = `/api/v1/board-runs/${detail.id}/logs`;
  links.append(logLink);
  (detail.result?.artifacts || []).forEach((artifact, index) => {
    if (artifact.name === "board.log") return;
    const link = el("a", "button ghost small", `${artifact.name} · ${formatBytes(artifact.size_bytes)}`);
    link.href = `/api/v1/board-runs/${detail.id}/artifacts/${index}`;
    links.append(link);
  });
  actions.append(links);
  root.append(actions);
  $("#cancel-board-run").classList.toggle("hidden", !ACTIVE_STATUSES.has(detail.status));
}

function closeBoardEventStream() {
  state.boardEventSource?.close();
  state.boardEventSource = null;
}

async function openBoardRun(runId) {
  try {
    const detail = await api(`/api/v1/board-runs/${runId}`);
    renderBoardRunDetail(detail);
    if (!$("#board-run-dialog").open) $("#board-run-dialog").showModal();
    closeBoardEventStream();
    if (ACTIVE_STATUSES.has(detail.status)) {
      state.boardEventSource = new EventSource(`/api/v1/board-runs/${runId}/events`);
      state.boardEventSource.addEventListener("status", async () => {
        const refreshed = await api(`/api/v1/board-runs/${runId}`);
        renderBoardRunDetail(refreshed);
        await loadBoardData();
      });
      state.boardEventSource.addEventListener("terminal", async () => {
        closeBoardEventStream();
        const refreshed = await api(`/api/v1/board-runs/${runId}`);
        renderBoardRunDetail(refreshed);
        await loadBoardData();
      });
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function periodicRefresh() {
  if (state.pollBusy) return;
  state.pollBusy = true;
  try {
    await loadRuns();
    await loadBoardData();
    if (state.currentProject && !$("#model-progress").classList.contains("hidden")) return;
    if (state.currentProject) await selectProject(state.currentProject.id, false);
    const smoke = state.preflight?.details?.runner_smoke_test;
    if (smoke && ACTIVE_STATUSES.has(smoke.status)) await refreshPreflight(false);
    if (state.currentRun?.id && $("#run-dialog").open) await refreshRunDetail();
    if (state.currentBoardRun?.id && $("#board-run-dialog").open) {
      const detail = await api(`/api/v1/board-runs/${state.currentBoardRun.id}`);
      renderBoardRunDetail(detail);
    }
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
    const created = await api(`/api/v1/projects/${state.currentProject.id}/calibration-sets`, {method: "POST", body: JSON.stringify({
      name: $("#calibration-name").value,
      description: "",
      source_type: $("#calibration-source-type").value,
    })});
    event.target.reset();
    await selectProject(state.currentProject.id, false);
    $("#calibration-version-select").value = created.versions[0].id;
    syncCalibrationUploadMode();
    toast(`${calibrationSourceLabel(created.versions[0].source_type)}校准集草稿已创建`);
  } catch (error) { toast(error.message, true); }
});

$("#calibration-version-select").addEventListener("change", syncCalibrationUploadMode);
$("#sample-files").addEventListener("change", (event) => {
  if (event.target.files.length) $("#sample-file-label").textContent = `已选择 ${event.target.files.length} 个文件`;
  else syncCalibrationUploadMode();
});
$("#sample-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const versionId = $("#calibration-version-select").value; const files = [...$("#sample-files").files];
  if (!versionId) return toast("请选择一个 DRAFT 校准版本", true);
  if (calibrationVersion(versionId)?.source_type === "npy_multi") return toast("多输入 NPY 必须通过 ZIP 原子导入", true);
  if (!files.length) return toast("请选择至少一份校准样本", true);
  const button = event.submitter; button.disabled = true;
  try {
    for (let index = 0; index < files.length; index += 1) {
      await uploadBinary(`/api/v1/calibration-versions/${versionId}/samples`, files[index], (ratio) => setProgress("#sample-progress", (index + ratio) / files.length));
    }
    event.target.reset();
    await selectProject(state.currentProject.id, false);
    $("#calibration-version-select").value = versionId;
    syncCalibrationUploadMode();
    toast(`${files.length} 份校准样本已登记`);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#sample-archive").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  const versionId = $("#calibration-version-select").value;
  if (!file) return;
  if (!versionId) {
    event.target.value = "";
    return toast("请选择一个 DRAFT 校准版本", true);
  }
  event.target.disabled = true;
  try {
    const result = await uploadBinary(`/api/v1/calibration-versions/${versionId}/archives`, file, (ratio) => setProgress("#sample-progress", ratio));
    await selectProject(state.currentProject.id, false);
    $("#calibration-version-select").value = versionId;
    syncCalibrationUploadMode();
    toast(`ZIP 已原子导入 ${result.imported_count} 份校准样本`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    event.target.disabled = false;
    event.target.value = "";
  }
});

$("#finalize-calibration").addEventListener("click", async () => {
  const versionId = $("#calibration-version-select").value; if (!versionId) return toast("请选择一个 DRAFT 校准版本", true);
  try {
    const current = projectCalibrations().find(({version}) => version.id === versionId)?.version;
    if (!window.confirm(`定稿后不可再添加样本。确认冻结当前 ${current?.sample_count || 0} 份样本？`)) return;
    const result = await api(`/api/v1/calibration-versions/${versionId}/finalize`, {method: "POST"});
    await selectProject(state.currentProject.id, false); toast(result.sample_count < 20 ? "版本已定稿，但少于 20 份，不能用于标准转换" : "校准版本已定稿，Manifest 与源文件已冻结");
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

$("#open-device-dialog").addEventListener("click", () => $("#device-dialog").showModal());
$("#refresh-devices").addEventListener("click", () => loadBoardData().catch((error) => toast(error.message, true)));
$("#device-auth-type").addEventListener("change", () => {
  const privateKey = $("#device-auth-type").value === "private_key";
  $("#device-password-field").classList.toggle("hidden", privateKey);
  $("#device-private-key-field").classList.toggle("hidden", !privateKey);
  $("#device-passphrase-field").classList.toggle("hidden", !privateKey);
  $("#device-password").required = !privateKey;
  $("#device-private-key").required = privateKey;
});
$("#device-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  const privateKey = $("#device-auth-type").value === "private_key";
  const credential = privateKey
    ? {
      private_key: $("#device-private-key").value,
      ...($("#device-passphrase").value ? {passphrase: $("#device-passphrase").value} : {}),
    }
    : {password: $("#device-password").value};
  try {
    const device = await api("/api/v1/devices", {
      method: "POST",
      body: JSON.stringify({
        name: $("#device-name").value,
        platform: $("#device-platform").value,
        host: $("#device-host").value,
        port: Number($("#device-port").value),
        user: $("#device-user").value,
        auth_type: $("#device-auth-type").value,
        credential,
        host_key_fingerprint: $("#device-fingerprint").value.trim() || null,
      }),
    });
    event.target.reset();
    $("#device-port").value = "22";
    $("#device-user").value = "root";
    $("#device-auth-type").dispatchEvent(new Event("change"));
    $("#device-dialog").close();
    await loadBoardData();
    toast("设备已保存；现在进行 Host Key 与运行时探测");
    await probeDevice(device.id);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#open-board-run-dialog").addEventListener("click", () => openBoardRunCreate());
$("#board-device").addEventListener("change", () => populateBoardRunForm($("#board-device").value));
$("#board-mode").addEventListener("change", syncBoardRunFields);
$("#board-duration-mode").addEventListener("change", syncBoardRunFields);
$("#board-run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const mode = $("#board-mode").value;
  button.disabled = true;
  try {
    let run;
    if (mode === "infer") {
      const file = $("#board-input").files[0];
      if (!file) throw new Error("请选择推理输入文件");
      const query = new URLSearchParams({
        device_id: $("#board-device").value,
        conversion_run_id: $("#board-conversion").value,
        core_id: $("#board-core").value,
      });
      run = await uploadBinary(`/api/v1/board-runs/infer?${query}`, file, (ratio) => {
        setProgress("#board-upload-progress", ratio);
      });
    } else {
      const payload = {
        device_id: $("#board-device").value,
        conversion_run_id: $("#board-conversion").value,
        mode,
      };
      if (mode === "perf") {
        payload.core_id = Number($("#board-core").value);
        payload.thread_num = Number($("#board-threads").value);
        if ($("#board-duration-mode").value === "time") {
          payload.perf_time_minutes = Number($("#board-perf-time").value);
        } else {
          payload.frame_count = Number($("#board-frame-count").value);
        }
      }
      run = await api("/api/v1/board-runs", {method: "POST", body: JSON.stringify(payload)});
    }
    event.target.reset();
    $("#board-run-create-dialog").close();
    await loadBoardData();
    toast(`板端${boardModeLabel(mode)} ${run.id.slice(0, 8)} 已入队`);
    await openBoardRun(run.id);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    syncBoardRunFields();
  }
});
$("#cancel-board-run").addEventListener("click", async () => {
  if (!state.currentBoardRun || !window.confirm("取消当前板端任务并关闭 SSH 通道？")) return;
  try {
    await api(`/api/v1/board-runs/${state.currentBoardRun.id}/cancel`, {method: "POST"});
    toast("板端取消请求已发送");
    await openBoardRun(state.currentBoardRun.id);
  } catch (error) {
    toast(error.message, true);
  }
});

$("#wizard-model").addEventListener("change", () => {
  updateWizardModel(true);
  updateCalibrationSelection(true);
});
$("#wizard-calibration").addEventListener("change", () => updateCalibrationSelection(true));
$("#runner-mode").addEventListener("change", syncRunnerMode);
$("#compile-mode").addEventListener("change", toggleCompileMode);
$("#l2m-mode").addEventListener("change", toggleL2mCustom);
["target-shape", "train-layout", "train-type", "resize-short", "recipe-mean", "recipe-std"].forEach((id) => $(`#${id}`).addEventListener("change", () => renderCalibrationPreview().catch(() => {})));
WIZARD_FIELDS.forEach((id) => $(`#${id}`).addEventListener("change", () => { state.wizardPreview = null; saveWizardDraft(); }));
$("#additional-input-configs").addEventListener("change", () => {
  state.wizardPreview = null;
  saveWizardDraft();
});
$("#comparison-runs").addEventListener("change", updateComparisonButton);
$("#compare-runs").addEventListener("click", async () => {
  const runIds = [...$("#comparison-runs").selectedOptions].map((option) => option.value);
  try {
    const comparison = await api("/api/v1/run-comparisons", {method: "POST", body: JSON.stringify({run_ids: runIds})});
    renderComparison(comparison);
    $("#comparison-dialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
});
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
  if (dialog.id === "board-run-dialog") closeBoardEventStream();
  dialog.close();
}));
$("#run-dialog").addEventListener("close", closeRunStreams);
$("#board-run-dialog").addEventListener("close", closeBoardEventStream);

async function initialize() {
  try {
    const session = await api("/api/v1/session");
    state.csrf = session.csrf_token;
    state.boardUploadLimit = session.board_max_upload_bytes;
    $("#upload-limit").textContent = formatBytes(session.max_upload_bytes);
    const [profiles, preflight, projects, runs, devices, boardRuns] = await Promise.all([
      api("/api/v1/profiles"), api("/api/v1/system/preflight"), api("/api/v1/projects"), api("/api/v1/runs"),
      api("/api/v1/devices"), api("/api/v1/board-runs"),
    ]);
    state.profiles = profiles; state.preflight = preflight; state.projects = projects; state.runs = runs;
    state.devices = devices; state.boardRuns = boardRuns;
    renderProfileCards(); renderPreflight(); renderProjectList(); renderRunSummary(); renderAllRuns(); populateComparisonRuns();
    renderDevices(); renderBoardRuns(); populateBoardRunForm();
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
