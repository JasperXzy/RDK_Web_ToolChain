"use strict";

const state = {
  csrf: "",
  projects: [],
  currentProject: null,
  profiles: [],
  runs: [],
};

const $ = (selector) => document.querySelector(selector);
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
  toast.timer = setTimeout(() => { node.className = "toast"; }, 3600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && typeof options.body === "string") headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-RDKWT-CSRF", state.csrf);
  const response = await fetch(path, {...options, headers});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败（${response.status}）`);
  return payload;
}

function uploadBinary(path, file, extraHeaders, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    request.responseType = "json";
    request.setRequestHeader("X-RDKWT-CSRF", state.csrf);
    const filenameBytes = new TextEncoder().encode(file.name);
    let filenameBinary = "";
    filenameBytes.forEach((byte) => { filenameBinary += String.fromCharCode(byte); });
    request.setRequestHeader("X-Filename-B64", btoa(filenameBinary));
    Object.entries(extraHeaders || {}).forEach(([key, value]) => { if (value) request.setRequestHeader(key, value); });
    request.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve(request.response);
      else reject(new Error(request.response?.detail || `上传失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error("上传连接失败"));
    request.send(file);
  });
}

const formatBytes = (bytes) => {
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
};
const formatDate = (value) => new Intl.DateTimeFormat("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}).format(new Date(value));

function setProgress(selector, ratio) {
  const node = $(selector);
  node.classList.remove("hidden");
  node.firstElementChild.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
  if (ratio >= 1) setTimeout(() => node.classList.add("hidden"), 700);
}

async function loadProjects(selectId = state.currentProject?.id) {
  state.projects = await api("/api/v1/projects");
  renderProjectList();
  if (selectId && state.projects.some((project) => project.id === selectId)) await selectProject(selectId);
}

function renderProjectList() {
  const list = $("#project-list");
  list.replaceChildren();
  $("#project-empty").classList.toggle("hidden", state.projects.length > 0);
  for (const project of state.projects) {
    const button = el("button", `project-item${state.currentProject?.id === project.id ? " active" : ""}`);
    button.type = "button";
    button.append(el("strong", "", project.name), el("span", "", `${project.model_count} 模型 · ${project.calibration_set_count} 校准集`));
    button.addEventListener("click", () => selectProject(project.id));
    list.append(button);
  }
}

async function selectProject(projectId) {
  state.currentProject = await api(`/api/v1/projects/${projectId}`);
  $("#welcome").classList.add("hidden");
  $("#project-workspace").classList.remove("hidden");
  renderProjectList();
  renderCurrentProject();
}

function renderCurrentProject() {
  const project = state.currentProject;
  $("#current-project-name").textContent = project.name;
  $("#current-project-description").textContent = project.description || "暂无备注";
  $("#metric-models").textContent = project.model_count;
  $("#metric-calibrations").textContent = project.calibration_set_count;
  $("#metric-storage").textContent = formatBytes(project.disk_usage_bytes);
  renderModels(project.models || []);
  renderCalibrations(project.calibration_sets || []);
  renderRuns();
}

function renderModels(models) {
  const list = $("#model-list");
  const select = $("#conversion-model");
  list.replaceChildren();
  select.replaceChildren(new Option("选择模型", ""));
  for (const model of models) {
    for (const version of model.versions) {
      const row = el("div", "asset-row");
      const copy = el("div");
      copy.append(el("strong", "", model.name), el("small", "", `${formatBytes(version.asset.size_bytes)} · ${version.asset.sha256.slice(0, 12)}…`));
      row.append(copy, el("span", "asset-tag", "待转换检查"));
      list.append(row);
      select.add(new Option(`${model.name} · ${version.asset.sha256.slice(0, 8)}`, version.id));
    }
  }
  if (!models.length) list.append(el("div", "empty-state", "尚未上传 ONNX 模型"));
}

function renderCalibrations(calibrationSets) {
  const list = $("#calibration-list");
  const versionSelect = $("#calibration-version-select");
  const conversionSelect = $("#conversion-calibration");
  const selected = versionSelect.value;
  list.replaceChildren();
  versionSelect.replaceChildren(new Option("选择校准版本", ""));
  conversionSelect.replaceChildren(new Option("选择已定稿版本", ""));
  for (const calibrationSet of calibrationSets) {
    for (const version of calibrationSet.versions) {
      const row = el("div", "asset-row");
      const copy = el("div");
      copy.append(el("strong", "", calibrationSet.name), el("small", "", `${version.sample_count} 个样本 · ${version.status === "READY" ? "清单已冻结" : "可继续上传"}`));
      row.append(copy, el("span", `asset-tag${version.status === "DRAFT" ? " draft" : ""}`, version.status === "READY" ? "READY" : "DRAFT"));
      list.append(row);
      versionSelect.add(new Option(`${calibrationSet.name} · ${version.sample_count} 张 · ${version.status}`, version.id));
      if (version.status === "READY" && version.sample_count >= 20) conversionSelect.add(new Option(`${calibrationSet.name} · ${version.sample_count} 张`, version.id));
    }
  }
  if ([...versionSelect.options].some((option) => option.value === selected)) versionSelect.value = selected;
  if (!calibrationSets.length) list.append(el("div", "empty-state", "新建草稿后可批量上传校准图片"));
}

function renderProfiles() {
  const select = $("#conversion-profile");
  select.replaceChildren();
  for (const profile of state.profiles) select.add(new Option(`${profile.display_name} · ${profile.march}`, profile.profile_id));
}

async function loadRuns() {
  state.runs = await api("/api/v1/runs");
  renderRuns();
}

function renderRuns() {
  const list = $("#run-list");
  list.replaceChildren();
  const projectRuns = state.runs.filter((run) => run.project_id === state.currentProject?.id).slice(0, 8);
  if (!projectRuns.length) return list.append(el("div", "empty-state", "暂无转换任务"));
  for (const run of projectRuns) {
    const row = el("div", "run-row");
    const identity = el("div");
    identity.append(el("strong", "", run.profile_id), el("code", "", run.id));
    const statusClass = ["SUCCEEDED"].includes(run.status) ? "good" : ["FAILED"].includes(run.status) ? "bad" : "neutral";
    row.append(identity, el("span", `status-pill ${statusClass}`, run.status), el("span", "", `尝试 ${run.attempts.length}`), el("span", "", formatDate(run.created_at)));
    list.append(row);
  }
}

function showCreateProject() {
  $("#create-project-form").classList.remove("hidden");
  $("#project-name").focus();
}

$("#toggle-create-project").addEventListener("click", showCreateProject);
$("#welcome-create").addEventListener("click", showCreateProject);
$("#cancel-create-project").addEventListener("click", () => $("#create-project-form").classList.add("hidden"));
$("#create-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const project = await api("/api/v1/projects", {method: "POST", body: JSON.stringify({name: $("#project-name").value, description: $("#project-description").value})});
    event.target.reset();
    event.target.classList.add("hidden");
    await loadProjects(project.id);
    toast("项目已创建");
  } catch (error) { toast(error.message, true); }
});

$("#model-file").addEventListener("change", (event) => { $("#model-file-label").textContent = event.target.files[0]?.name || "文件保留原名用于展示，内部使用内容哈希"; });
$("#model-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#model-file").files[0];
  if (!file || !state.currentProject) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const modelName = $("#model-name").value;
    const uploadPath = `/api/v1/projects/${state.currentProject.id}/models${modelName ? `?model_name=${encodeURIComponent(modelName)}` : ""}`;
    const result = await uploadBinary(uploadPath, file, {}, (ratio) => setProgress("#model-progress", ratio));
    event.target.reset();
    $("#model-file-label").textContent = "文件保留原名用于展示，内部使用内容哈希";
    await loadProjects(state.currentProject.id);
    toast(result.storage_reused ? "模型已登记，复用了相同内容" : "模型上传完成");
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#calibration-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.currentProject) return;
  try {
    const created = await api(`/api/v1/projects/${state.currentProject.id}/calibration-sets`, {method: "POST", body: JSON.stringify({name: $("#calibration-name").value, description: ""})});
    event.target.reset();
    await loadProjects(state.currentProject.id);
    $("#calibration-version-select").value = created.versions[0].id;
    toast("校准集草稿已创建");
  } catch (error) { toast(error.message, true); }
});

$("#sample-files").addEventListener("change", (event) => { $("#sample-file-label").textContent = event.target.files.length ? `已选择 ${event.target.files.length} 个文件` : "可一次选择多张，按选择顺序登记"; });
$("#sample-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const versionId = $("#calibration-version-select").value;
  const files = [...$("#sample-files").files];
  if (!versionId) return toast("请先选择校准版本", true);
  const button = event.submitter;
  button.disabled = true;
  try {
    for (let index = 0; index < files.length; index += 1) {
      await uploadBinary(`/api/v1/calibration-versions/${versionId}/samples`, files[index], {}, (ratio) => setProgress("#sample-progress", (index + ratio) / files.length));
    }
    event.target.reset();
    $("#sample-file-label").textContent = "可一次选择多张，按选择顺序登记";
    await loadProjects(state.currentProject.id);
    $("#calibration-version-select").value = versionId;
    toast(`${files.length} 个校准样本已登记`);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#finalize-calibration").addEventListener("click", async () => {
  const versionId = $("#calibration-version-select").value;
  if (!versionId) return toast("请先选择校准版本", true);
  try {
    const result = await api(`/api/v1/calibration-versions/${versionId}/finalize`, {method: "POST"});
    await loadProjects(state.currentProject.id);
    toast(result.sample_count < 20 ? "版本已定稿，但少于 20 张，暂不能转换" : "校准版本已定稿并冻结清单");
  } catch (error) { toast(error.message, true); }
});

$("#conversion-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const profileId = $("#conversion-profile").value;
  const s600 = profileId.startsWith("s600");
  const calibrationId = $("#conversion-calibration").value;
  const calibrationSet = (state.currentProject.calibration_sets || []).flatMap((item) => item.versions).find((item) => item.id === calibrationId);
  const button = event.submitter;
  button.disabled = true;
  try {
    const submission = await api("/api/v1/conversion-runs", {method: "POST", body: JSON.stringify({
      profile_id: profileId,
      model_version_id: $("#conversion-model").value,
      calibration_version_id: calibrationId,
      output_prefix: $("#output-prefix").value,
      core_num: s600 ? 2 : 1,
      max_l2m_size: s600 ? "auto" : 0,
      sample_limit: Math.min(100, calibrationSet?.sample_count || 20),
    })});
    toast(`任务 ${submission.run_id.slice(0, 8)} 已进入队列`);
    await loadRuns();
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#refresh-runs").addEventListener("click", () => loadRuns().catch((error) => toast(error.message, true)));
$("#delete-project").addEventListener("click", async () => {
  if (!state.currentProject) return;
  try {
    const preview = await api(`/api/v1/projects/${state.currentProject.id}/deletion-preview`);
    if (!preview.can_delete) return toast(preview.blocked_reason, true);
    const summary = `${preview.model_count} 个模型、${preview.calibration_set_count} 个校准集和 ${formatBytes(preview.disk_usage_bytes)} 资产`;
    if (!window.confirm(`确认永久删除“${state.currentProject.name}”及其 ${summary}？`)) return;
    await api(`/api/v1/projects/${state.currentProject.id}`, {method: "DELETE", headers: {"X-Confirm-Project": state.currentProject.id}});
    state.currentProject = null;
    $("#project-workspace").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    await loadProjects();
    toast("项目及未共享资产已删除");
  } catch (error) { toast(error.message, true); }
});

async function initialize() {
  try {
    const [session, profiles, preflight] = await Promise.all([api("/api/v1/session"), api("/api/v1/profiles"), api("/api/v1/system/preflight")]);
    state.csrf = session.csrf_token;
    $("#upload-limit").textContent = formatBytes(session.max_upload_bytes);
    state.profiles = profiles;
    renderProfiles();
    const status = $("#system-status");
    status.className = `status-pill ${preflight.available ? "good" : "bad"}`;
    status.innerHTML = `<span></span>${preflight.available ? "Runner 已就绪" : "Runner 不可用"}`;
    await Promise.all([loadProjects(), loadRuns()]);
    setInterval(() => loadRuns().catch(() => {}), 5000);
  } catch (error) { toast(error.message, true); }
}

initialize();
