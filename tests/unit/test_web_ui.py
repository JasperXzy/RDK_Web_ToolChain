from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "services" / "controller" / "rdkwt_controller" / "web"
HTML = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
CSS = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
JAVASCRIPT = (WEB_ROOT / "app.js").read_text(encoding="utf-8")


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node_id = dict(attrs).get("id")
        if node_id:
            self.ids.append(node_id)


def _hex_variable(name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\s*;", CSS)
    assert match, f"missing hexadecimal CSS variable --{name}"
    return match.group(1)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_dark_theme_semantic_colors_meet_contrast_thresholds() -> None:
    canvas = _hex_variable("canvas")
    panel = _hex_variable("panel")
    foreground = _hex_variable("ink")
    muted = _hex_variable("muted")
    accent = _hex_variable("accent")
    on_accent = _hex_variable("on-accent")
    input_border = _hex_variable("line-dark")

    assert _contrast(foreground, canvas) >= 4.5
    assert _contrast(muted, panel) >= 4.5
    assert _contrast(accent, canvas) >= 4.5
    assert _contrast(on_accent, accent) >= 4.5
    assert _contrast(input_border, panel) >= 3.0


def test_p0_mobile_navigation_and_accessibility_structure_are_present() -> None:
    assert '<meta name="color-scheme" content="dark">' in HTML
    assert '<a class="skip-link" href="#main-content">' in HTML
    assert '<main id="main-content" class="workspace" tabindex="-1">' in HTML
    mobile_navigation = re.search(r'<nav class="mobile-nav".*?</nav>', HTML, re.DOTALL)
    assert mobile_navigation
    assert mobile_navigation.group(0).count("data-view=") == 4
    assert 'role="tablist"' in HTML
    assert HTML.count('role="tabpanel"') == 4
    assert HTML.count('role="progressbar"') == 3
    assert 'id="wizard-error-summary"' in HTML
    assert 'class="sidebar-foot"' not in HTML
    assert "LOCAL ONLY" not in HTML
    assert "转换容器默认断网运行" not in HTML

    collector = _IdCollector()
    collector.feed(HTML)
    assert len(collector.ids) == len(set(collector.ids)), "HTML IDs must remain unique"


def test_mobile_rules_restore_critical_run_actions_and_touch_targets() -> None:
    legacy_hide = CSS.index(".run-actions .button, .run-actions .status-pill { display: none; }")
    restored_actions = CSS.index(".run-actions .button, .run-actions .status-pill { display: inline-flex; }")
    assert restored_actions > legacy_hide
    assert ".mobile-nav {" in CSS
    assert "min-height: 44px" in CSS
    assert "env(safe-area-inset-bottom)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS


def test_conversion_and_calibration_actions_are_state_gated() -> None:
    assert "function projectConversionReadiness()" in JAVASCRIPT
    assert "function projectHasSuccessfulConversion()" in JAVASCRIPT
    assert 'run.kind === "CONVERSION"' in JAVASCRIPT
    assert 'run.status === "SUCCEEDED"' in JAVASCRIPT
    assert 'nextAction.classList.toggle("hidden", completed)' in JAVASCRIPT
    assert "wizardButton.disabled = false" in JAVASCRIPT
    assert 'wizardButton.setAttribute("aria-disabled", String(!readiness.ready))' in JAVASCRIPT
    assert 'classList.toggle("has-tooltip", unavailable)' in JAVASCRIPT
    assert 'version.status === "READY" && version.sample_count >= 20' in JAVASCRIPT
    assert "const canFinalize = draftSelected && selected.sample_count >= 20" in JAVASCRIPT
    assert "sampleInput.disabled = !draftSelected || multi" in JAVASCRIPT
    assert 'node.setAttribute("role", error ? "alert" : "status")' in JAVASCRIPT
    assert 'button.setAttribute("aria-busy", "true")' in JAVASCRIPT


def test_project_copy_actions_and_editing_follow_the_compact_chinese_ui() -> None:
    assert 'id="project-name" maxlength="200" required>' in HTML
    assert 'id="project-description" maxlength="4000" rows="2">' in HTML
    assert "例如：ResNet18 基线" not in HTML
    assert "用途、数据来源或负责人" not in HTML
    assert "创建一个项目开始管理模型和校准数据" in HTML
    assert "还没有项目" not in HTML
    assert "Current project" not in HTML
    assert "Recommended action" not in HTML
    assert "工作空间" not in HTML
    assert "M5 · 发布与维护" not in HTML
    assert "导入 ONNX，冻结校准数据和平台配置" not in HTML
    assert "执行历史" not in HTML
    assert "排队、转换、模型检查和所有历史 Attempt 都保存在本机" not in HTML
    assert "开发板验证" not in HTML
    assert "Controller 直接通过 SSH / SFTP 连接开发板" not in HTML
    assert "发布与维护" not in HTML
    assert "查看磁盘、预览后清理可再生文件" not in HTML
    assert "STEP 01" not in HTML
    assert '<span class="next-action-marker" aria-hidden="true">建议操作</span>' in HTML
    assert 'id="next-action-description"' not in HTML
    assert '$("#next-action-description")' not in JAVASCRIPT
    assert "01 · 模型" not in HTML
    assert "02 · 校准" not in HTML
    assert "任务动态" not in HTML
    assert "<h2>ONNX 模型</h2>" in HTML
    assert "<h2>校准数据集</h2>" in HTML
    assert "<h2>项目任务</h2>" in HTML
    assert 'id="edit-project"' in HTML
    assert 'id="delete-project" class="button danger"' in HTML
    assert 'id="project-edit-form"' in HTML
    assert 'method: "PATCH"' in JAVASCRIPT
    assert 'id="conversion-prereq" class="button-tooltip" role="tooltip"' in HTML
    assert ".tooltip-anchor.has-tooltip:hover .button-tooltip" in CSS
    assert ".tooltip-anchor.has-tooltip:focus-within .button-tooltip" in CSS
    assert "。" not in HTML
    assert "。" not in JAVASCRIPT


def test_asset_uploads_use_compact_single_entry_workflows() -> None:
    assert 'class="upload-form model-upload-form"' in HTML
    assert 'id="model-upload-details" class="upload-entry-details hidden"' in HTML
    assert '<button class="button secondary" type="submit">上传并检查</button>' in HTML
    assert 'file.name.replace(/\\.onnx$/i, "").slice(0, 200)' in JAVASCRIPT
    assert 'class="calibration-create"' in HTML
    assert 'id="calibration-action-hint" class="calibration-status hidden"' in HTML
    assert "先创建并选择一个 DRAFT 校准版本" not in HTML
    assert "先创建并选择一个 DRAFT 校准版本" not in JAVASCRIPT
    assert "请选择一个 DRAFT 校准版本" not in JAVASCRIPT
    assert "新建草稿后可批量上传图片或直接 NPY" not in JAVASCRIPT
    assert "暂无可编辑草稿" in HTML
    assert "暂无校准数据集" in JAVASCRIPT


def test_board_and_maintenance_panels_keep_only_primary_titles() -> None:
    assert '<div class="panel-heading"><h2>受信任设备</h2></div>' in HTML
    assert '<div class="panel-heading"><h2>板端验证历史</h2>' in HTML
    assert '<div class="panel-heading"><h2>可回收空间</h2>' in HTML
    assert '<div class="panel-heading"><h2>本地备份</h2></div>' in HTML
    assert '<span class="step-label">设备</span>' not in HTML
    assert '<span class="step-label">板端任务</span>' not in HTML
    assert '<span class="step-label">安全清理</span>' not in HTML
    assert '<span class="step-label">备份与恢复</span>' not in HTML
    removed_copy = (
        "尚未添加开发板",
        "设备探测成功、且已有 HBM 后即可运行 model_info / infer / perf",
        "首次探测必须确认 SHA256 Host Key",
        "尚未预览",
        "缓存不进入备份",
        "当前没有活动任务；执行前仍会重新核对文件列表与一次性确认令牌",
        "恢复必须停服执行",
    )
    for copy in removed_copy:
        assert copy not in HTML
        assert copy not in JAVASCRIPT
    assert 'id="device-list" class="device-grid"></div>' in HTML
    assert 'id="board-run-list" class="run-list"></div>' in HTML
    assert 'id="cleanup-total" class="hint"></span>' in HTML
    assert '$("#cleanup-note")' not in JAVASCRIPT


def test_dialog_headers_keep_only_primary_titles() -> None:
    headers = re.findall(r'<header class="dialog-header[^>]*>.*?</header>', HTML, re.DOTALL)
    assert len(headers) == 8
    assert all("<h2" in header for header in headers)
    assert all('class="eyebrow"' not in header for header in headers)
    assert all("<p" not in header for header in headers)
    assert "设备管理" not in HTML
    assert "Host Key 可先留空；首次探测只显示指纹，确认后才会建立信任" not in HTML
    assert "只允许上传已完成转换的 HBM，并调用白名单内的 hrt_model_exec 模式" not in HTML
    assert "同一模型的结构化指标" not in HTML
    assert "不会静默拉取镜像，也不会向外部发送模型数据" not in HTML
    for removed_id in ("wizard-draft-status", "board-run-subtitle", "comparison-subtitle", "run-kind", "run-subtitle"):
        assert f'id="{removed_id}"' not in HTML
        assert f'$("#{removed_id}")' not in JAVASCRIPT


def test_workspace_selects_use_accessible_custom_popovers() -> None:
    assert HTML.count('data-custom-select') == 3
    assert HTML.count('role="combobox"') == 2
    assert HTML.count('role="listbox"') == 3
    assert 'id="calibration-source-type-trigger"' in HTML
    assert 'id="calibration-version-select-trigger"' in HTML
    assert "function initializeCustomSelects(scope = document)" in JAVASCRIPT
    assert "function enhanceNativeSelect(select)" in JAVASCRIPT
    assert "function enhanceNativeSelects(scope = document)" in JAVASCRIPT
    assert 'select:not(.custom-select-native)' in JAVASCRIPT
    assert 'root.dataset.customSelect = ""' in JAVASCRIPT
    assert 'trigger.setAttribute("role", "combobox")' in JAVASCRIPT
    assert 'select.addEventListener("invalid"' in JAVASCRIPT
    assert "initializeCustomSelects(root)" in JAVASCRIPT
    assert "function handleCustomSelectKeydown(event, instance)" in JAVASCRIPT
    assert '["ArrowDown", "ArrowUp"]' in JAVASCRIPT
    assert '["Home", "End"]' in JAVASCRIPT
    assert '["Enter", " "]' in JAVASCRIPT
    assert 'event.key === "Escape"' in JAVASCRIPT
    assert 'instance.select.dispatchEvent(new Event("change", {bubbles: true}))' in JAVASCRIPT
    assert "function positionCustomSelect(instance)" in JAVASCRIPT
    assert 'classList.toggle("opens-upward", opensUpward)' in JAVASCRIPT
    assert "instance.menu.scrollTop = activeBottom - instance.menu.clientHeight" in JAVASCRIPT
    assert 'window.addEventListener("scroll", repositionOpenSelects, {passive: true})' in JAVASCRIPT
    assert 'document.addEventListener("scroll", repositionOpenSelects, {capture: true, passive: true})' in JAVASCRIPT
    assert ".custom-select-menu {" in CSS
    assert ".custom-select.opens-upward .custom-select-menu" in CSS
    assert "right: 14px" in CSS
    assert "max-height: 240px" in CSS


def test_run_comparison_uses_aligned_bounded_multiselect() -> None:
    assert 'id="comparison-runs" class="custom-select-native" multiple' in HTML
    assert 'data-max-selections="4"' in HTML
    assert 'aria-multiselectable="true"' in HTML
    assert 'id="comparison-runs-count"' in HTML
    assert "function customSelectMaxSelections(instance)" in JAVASCRIPT
    assert "if (instance.select.multiple)" in JAVASCRIPT
    assert "option.selected = !option.selected" in JAVASCRIPT
    assert "customSelectSelectedCount(instance) >= customSelectMaxSelections(instance)" in JAVASCRIPT
    assert "refreshCustomSelect(select)" in JAVASCRIPT
    assert 'classList.add("custom-select-option-multiple")' in JAVASCRIPT
    assert "event.composedPath()" in JAVASCRIPT
    assert ".custom-select-count {" in CSS
    assert ".custom-select-option-multiple" in CSS
    assert ".custom-select-checkbox" in CSS
    assert ".comparison-picker select { min-height: 78px" not in CSS


def test_run_rows_do_not_repeat_terminal_status_as_stage() -> None:
    assert "function runState(status, stage)" in JAVASCRIPT
    assert "if (stage && stage !== status)" in JAVASCRIPT
    assert "runState(run.status, latest?.stage)" in JAVASCRIPT
    assert "runState(run.status, run.phase)" in JAVASCRIPT
    assert 'el("span", "", latest?.stage || run.status)' not in JAVASCRIPT
    assert "grid-template-columns: minmax(220px, 1fr) minmax(200px, 260px) 145px 18px" in CSS


def test_run_detail_uses_fixed_scroll_panes_and_wraps_long_metrics() -> None:
    assert '<label class="log-search" for="log-search"><span>搜索</span><input id="log-search"' in HTML
    assert '$(".run-body").classList.toggle("fixed-pane-active", ["logs", "config"].includes(tab))' in JAVASCRIPT
    assert ".run-body.fixed-pane-active { display: flex; flex-direction: column; overflow: hidden; }" in CSS
    assert ".log-search { flex: 1 1 260px; display: grid; grid-template-columns: max-content minmax(0, 1fr);" in CSS
    assert ".log-viewer { flex: 1; min-height: 0; height: auto; overflow: auto;" in CSS
    assert ".snapshot-grid .code-block { flex: 1; min-height: 0; height: auto; max-height: none; overflow: auto;" in CSS
    assert ".summary-card small { color: var(--faint); font-size: 9px; overflow-wrap: anywhere; word-break: break-word; }" in CSS
    assert "grid-template-columns: 145px minmax(0, 1fr); align-items: center" in CSS


def test_conversion_wizard_uses_compact_copy_contextual_help_and_responsive_fields() -> None:
    wizard = re.search(r'<dialog id="wizard-dialog".*?</dialog>', HTML, re.DOTALL)
    assert wizard
    markup = wizard.group(0)

    assert "<h2>转换向导</h2>" in markup
    assert "六步转换向导" not in markup
    assert '<span class="eyebrow">新建转换</span>' not in markup
    assert not re.search(r"第 [1-6] 步", markup)
    wizard_copy_blocks = re.findall(r'<div class="wizard-copy">.*?</div>', markup, re.DOTALL)
    assert len(wizard_copy_blocks) == 6
    assert all("<p" not in block for block in wizard_copy_blocks)
    assert markup.count('class="help-button"') == 10
    assert markup.count('aria-expanded="false"') == 10
    assert 'id="runner-mode-note"' not in markup
    assert 'class="form-grid calibration-core-fields"' in markup
    assert 'class="form-grid recipe-fields"' in markup
    assert '<option value="latency">低延迟</option>' in markup
    assert '<option value="bandwidth">低带宽</option>' in markup
    assert '<option value="balance">自定义平衡</option>' in markup
    assert r'pattern="[A-Za-z0-9][A-Za-z0-9_.\-]*"' in markup
    assert 'id="balance-factor-field" class="field-control balance-factor-field hidden"' in markup
    assert 'id="balance-factor" class="balance-factor-input" type="range"' in markup
    assert '<span>带宽 0</span><span>延迟 100</span>' in markup

    assert '$("#runner-mode-note")' not in JAVASCRIPT
    assert '$("#balance-factor-field").classList.toggle("hidden", !balance)' in JAVASCRIPT
    assert '$("#balance-factor-value").textContent = input.value' in JAVASCRIPT
    assert 'button.addEventListener("click", toggleFieldHelp)' in JAVASCRIPT
    assert 'if (event.key === "Escape") closeFieldHelp()' in JAVASCRIPT
    assert 'left: calc(50% + 18px); right: calc(-50% + 18px); top: 14px' in CSS
    assert '.runner-fields { grid-template-columns: minmax(0, 220px); margin-top: 24px; }' in CSS
    assert '.recipe-fields { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 16px; }' in CSS
    assert '#yaml-preview { max-height: none; overflow: visible; }' in CSS
