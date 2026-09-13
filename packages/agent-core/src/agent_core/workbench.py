"""Package-embedded, zero-build assets for the local Agent Workbench.

The transport layer deliberately owns route registration.  This module only
publishes immutable response bodies and security metadata so it remains usable
without importing FastAPI when the optional ``web`` dependencies are absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Final

WORKBENCH_CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

WORKBENCH_SECURITY_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Content-Security-Policy": WORKBENCH_CONTENT_SECURITY_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
)


@dataclass(frozen=True, slots=True)
class WorkbenchAsset:
    """One exact-route response asset suitable for a Web framework adapter."""

    route: str
    media_type: str
    content: bytes
    sha256: str

    @classmethod
    def from_text(cls, route: str, media_type: str, text: str) -> WorkbenchAsset:
        encoded = text.encode("utf-8")
        return cls(
            route=route,
            media_type=media_type,
            content=encoded,
            sha256=sha256(encoded).hexdigest(),
        )

    @property
    def headers(self) -> dict[str, str]:
        """Return a fresh header mapping safe for mutation by an adapter."""

        return {
            **WORKBENCH_SECURITY_HEADERS,
            "ETag": f'"sha256-{self.sha256}"',
        }


_WORKBENCH_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Agent Workbench</title>
  <link rel="stylesheet" href="/workbench/workbench.css">
  <script src="/workbench/workbench.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#main-workspace">跳到任务工作区</a>
  <header class="topbar">
    <div>
      <p class="eyebrow">Agent Core</p>
      <h1>任务工作台</h1>
    </div>
    <form id="connection-form" class="connection-form">
      <label for="api-token">API Token（仅保存在当前页面内存）</label>
      <div class="inline-controls">
        <input id="api-token" type="password" autocomplete="off">
        <button type="submit" class="secondary-button">连接</button>
      </div>
      <p id="connection-status" class="status-message" role="status" aria-live="polite">
        正在连接本地服务…
      </p>
    </form>
  </header>

  <main id="main-workspace" class="workbench-grid">
    <aside class="panel navigation-panel" aria-labelledby="navigation-title">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">Workspace</p>
          <h2 id="navigation-title">任务与模板</h2>
        </div>
        <button id="refresh-runs" type="button" class="icon-button">刷新</button>
      </div>

      <section aria-labelledby="plugins-title">
        <h3 id="plugins-title">工作模板</h3>
        <div id="plugin-list" class="plugin-list"></div>
      </section>

      <section aria-labelledby="recent-runs-title">
        <div class="section-heading">
          <h3 id="recent-runs-title">最近任务</h3>
          <label for="run-status-filter">状态</label>
          <select id="run-status-filter">
            <option value="">全部</option>
            <option value="queued">排队中</option>
            <option value="running">运行中</option>
            <option value="succeeded">成功</option>
            <option value="degraded">部分完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
        </div>
        <div id="runs-list" class="runs-list" aria-live="polite"></div>
      </section>
    </aside>

    <section class="panel task-panel" aria-labelledby="task-title">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">New Task</p>
          <h2 id="task-title">创建并观察任务</h2>
        </div>
        <span id="current-run-status" class="status-pill">未运行</span>
      </div>

      <form id="run-form" class="run-form">
        <div class="form-row two-columns">
          <label>
            <span>Plugin</span>
            <select id="plugin-select" required></select>
          </label>
          <label>
            <span>Provider</span>
            <select id="provider-select" required></select>
          </label>
        </div>

        <fieldset>
          <legend>输入来源</legend>
          <label for="source-type">来源类型</label>
          <select id="source-type">
            <option value="file">上传文件</option>
            <option value="text">粘贴文本</option>
            <option value="json">粘贴 JSON object</option>
          </select>

          <div id="file-source" class="source-panel">
            <label for="source-file">文件</label>
            <input id="source-file" type="file">
          </div>
          <div id="text-source" class="source-panel" hidden>
            <label for="source-text">文本</label>
            <textarea id="source-text" rows="8"></textarea>
            <label for="text-filename">文件名</label>
            <input id="text-filename" value="input.txt" maxlength="255">
          </div>
          <div id="json-source" class="source-panel" hidden>
            <label for="source-json">JSON object</label>
            <textarea id="source-json" rows="8">{}</textarea>
          </div>
        </fieldset>

        <fieldset>
          <legend>Plugin 选项</legend>
          <p class="field-note">
            基础 JSON Schema 字段显示为控件；复杂字段使用 JSON 输入。
          </p>
          <div id="plugin-options" class="options-grid"></div>
        </fieldset>

        <details>
          <summary>高级运行设置</summary>
          <div class="form-row two-columns details-body">
            <label>
              <span>Model（可选）</span>
              <input id="model-name" maxlength="128" autocomplete="off">
            </label>
            <label>
              <span>Timeout（秒）</span>
              <input id="timeout-seconds" type="number" min="1" max="3600" value="300">
            </label>
            <label>
              <span>Action mode</span>
              <select id="action-mode"></select>
            </label>
            <label class="checkbox-label">
              <input id="web-enrichment" type="checkbox">
              <span>允许 Web enrichment</span>
            </label>
          </div>
        </details>

        <div class="form-actions">
          <button id="submit-run" type="submit" class="primary-button">确认并运行</button>
          <button id="cancel-run" type="button" class="danger-button" disabled>取消任务</button>
          <button id="download-artifact" type="button" class="secondary-button" disabled>
            下载 Artifact
          </button>
        </div>
        <p id="form-message" class="status-message" role="alert" aria-live="assertive"></p>
      </form>

      <section class="timeline-section" aria-labelledby="timeline-title">
        <div class="section-heading">
          <h3 id="timeline-title">执行 Timeline</h3>
          <span id="run-id-label" class="monospace">尚无 Run</span>
        </div>
        <ol id="timeline" class="timeline" aria-live="polite"></ol>
      </section>
    </section>

    <aside class="panel contract-panel" aria-labelledby="contract-title">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">Task Contract</p>
          <h2 id="contract-title">固定与可变边界</h2>
        </div>
      </div>

      <section class="ownership-card program-fact" aria-labelledby="facts-title">
        <span class="ownership-badge">Program fact</span>
        <h3 id="facts-title">程序事实</h3>
        <dl id="program-facts" class="contract-list"></dl>
        <div id="ownership-program-fact" class="ownership-fields"></div>
      </section>

      <section class="ownership-card user-choice" aria-labelledby="choices-title">
        <span class="ownership-badge">User choice</span>
        <h3 id="choices-title">用户选择</h3>
        <pre id="user-choices" class="contract-json">尚未填写</pre>
        <div id="ownership-user-choice" class="ownership-fields"></div>
      </section>

      <section class="ownership-card policy-locked" aria-labelledby="policy-title">
        <span class="ownership-badge">Policy locked</span>
        <h3 id="policy-title">服务器策略</h3>
        <dl id="policy-facts" class="contract-list"></dl>
        <div id="ownership-policy-locked" class="ownership-fields"></div>
      </section>

      <section class="ownership-card ai-candidate" aria-labelledby="candidate-title">
        <span class="ownership-badge">AI-assisted</span>
        <h3 id="candidate-title">候选输出</h3>
        <p id="candidate-note">
          Provider 产生候选内容，Plugin 再进行确定性校验与 Artifact 渲染。
          当前 Manifest 尚未声明逐字段 ownership。
        </p>
        <div id="ownership-ai-candidate" class="ownership-fields"></div>
      </section>

      <section class="ownership-card action-input" aria-labelledby="action-input-title">
        <span class="ownership-badge">Action input</span>
        <h3 id="action-input-title">副作用输入</h3>
        <p id="action-input-note">
          Action 参数必须受服务器策略限制；当前工作台不会把聊天文本当作执行授权。
        </p>
        <div id="ownership-action-input" class="ownership-fields"></div>
      </section>
    </aside>
  </main>
</body>
</html>
"""


_WORKBENCH_CSS = r""":root {
  color-scheme: light dark;
  --background: #eef2f6;
  --panel: #ffffff;
  --panel-muted: #f7f9fb;
  --text: #19212a;
  --muted: #627080;
  --line: #d8e0e8;
  --accent: #155eef;
  --accent-soft: #eaf1ff;
  --danger: #b42318;
  --danger-soft: #fff0ee;
  --success: #067647;
  --warning: #a15c00;
  --radius: 14px;
  --shadow: 0 10px 30px rgba(21, 37, 56, 0.08);
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  background: var(--background);
  color: var(--text);
}

button,
input,
select,
textarea {
  color: inherit;
  font: inherit;
}

button,
select,
input,
textarea,
summary {
  outline-offset: 3px;
}

button {
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

input,
select,
textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--panel);
  padding: 10px 12px;
}

textarea {
  resize: vertical;
}

h1,
h2,
h3,
p {
  margin-top: 0;
}

h1 {
  margin-bottom: 0;
  font-size: clamp(1.35rem, 2vw, 1.8rem);
}

h2 {
  margin-bottom: 0;
  font-size: 1.08rem;
}

h3 {
  margin: 0 0 12px;
  font-size: 0.94rem;
}

.skip-link {
  position: fixed;
  z-index: 20;
  top: 8px;
  left: 8px;
  transform: translateY(-160%);
  border-radius: 8px;
  background: var(--text);
  color: var(--panel);
  padding: 8px 12px;
}

.skip-link:focus {
  transform: translateY(0);
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 28px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  padding: 15px 22px;
}

.connection-form {
  width: min(480px, 52vw);
}

.connection-form > label {
  display: block;
  margin-bottom: 5px;
  color: var(--muted);
  font-size: 0.78rem;
}

.inline-controls {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
}

.workbench-grid {
  display: grid;
  grid-template-columns: minmax(230px, 0.75fr) minmax(420px, 1.7fr) minmax(260px, 0.9fr);
  gap: 14px;
  max-width: 1720px;
  margin: 0 auto;
  padding: 14px;
}

.panel {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  box-shadow: var(--shadow);
  padding: 18px;
}

.navigation-panel,
.contract-panel {
  align-self: start;
  position: sticky;
  top: 14px;
  max-height: calc(100vh - 28px);
  overflow: auto;
}

.panel-heading,
.section-heading,
.form-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.panel-heading {
  margin-bottom: 22px;
}

.section-heading label {
  margin-left: auto;
  color: var(--muted);
  font-size: 0.78rem;
}

.section-heading select {
  width: auto;
  min-width: 100px;
  padding: 6px 8px;
}

.eyebrow {
  margin-bottom: 3px;
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 750;
  letter-spacing: 0.11em;
  text-transform: uppercase;
}

.status-message,
.field-note {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.45;
}

.status-message.is-error {
  color: var(--danger);
}

.status-message.is-success {
  color: var(--success);
}

.plugin-list,
.runs-list {
  display: grid;
  gap: 8px;
  margin-bottom: 24px;
}

.plugin-button,
.run-button {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel-muted);
  padding: 10px;
  text-align: left;
}

.plugin-button.is-selected,
.run-button.is-selected {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.run-button strong,
.run-button span {
  display: block;
}

.run-button span {
  margin-top: 4px;
  color: var(--muted);
  font-size: 0.76rem;
}

.run-form {
  display: grid;
  gap: 16px;
}

.run-form fieldset,
.run-form details {
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--panel-muted);
  padding: 15px;
}

.run-form legend,
.run-form summary {
  font-weight: 700;
}

.run-form label > span,
.source-panel > label,
.run-form fieldset > label {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 0.8rem;
  font-weight: 650;
}

.form-row {
  display: grid;
  gap: 12px;
}

.two-columns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.source-panel {
  display: grid;
  gap: 7px;
  margin-top: 12px;
}

.options-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.option-field {
  min-width: 0;
}

.option-field.is-wide {
  grid-column: 1 / -1;
}

.option-description {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: 0.74rem;
  line-height: 1.4;
}

.details-body {
  margin-top: 14px;
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 42px;
}

.checkbox-label input {
  width: auto;
}

.checkbox-label > span {
  margin: 0;
}

.form-actions {
  justify-content: flex-start;
  flex-wrap: wrap;
}

.primary-button,
.secondary-button,
.danger-button,
.icon-button {
  border-radius: 9px;
  padding: 9px 13px;
  font-weight: 700;
}

.primary-button {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #ffffff;
}

.secondary-button,
.icon-button {
  border: 1px solid var(--line);
  background: var(--panel);
}

.danger-button {
  border: 1px solid #f1b4ae;
  background: var(--danger-soft);
  color: var(--danger);
}

.status-pill,
.ownership-badge {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--panel-muted);
  padding: 4px 9px;
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 750;
}

.status-pill[data-status="succeeded"] {
  border-color: #98d3b9;
  color: var(--success);
}

.status-pill[data-status="failed"],
.status-pill[data-status="cancelled"] {
  border-color: #f1b4ae;
  color: var(--danger);
}

.status-pill[data-status="degraded"] {
  border-color: #efca85;
  color: var(--warning);
}

.timeline-section {
  margin-top: 24px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
}

.timeline {
  display: grid;
  gap: 10px;
  margin: 14px 0 0;
  padding: 0;
  list-style: none;
}

.timeline-item {
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  background: var(--panel-muted);
  padding: 10px 12px;
}

.timeline-item strong,
.timeline-item span {
  display: block;
}

.timeline-item span {
  margin-top: 3px;
  color: var(--muted);
  font-size: 0.76rem;
}

.monospace,
.contract-json {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.monospace {
  overflow: hidden;
  color: var(--muted);
  font-size: 0.72rem;
  text-overflow: ellipsis;
}

.ownership-card {
  margin-bottom: 12px;
  border: 1px solid var(--line);
  border-left-width: 4px;
  border-radius: 10px;
  background: var(--panel-muted);
  padding: 13px;
}

.ownership-card h3 {
  margin-top: 9px;
}

.program-fact {
  border-left-color: #155eef;
}

.user-choice {
  border-left-color: #7a5af8;
}

.policy-locked {
  border-left-color: #b54708;
}

.ai-candidate {
  border-left-color: #067647;
}

.action-input {
  border-left-color: #c11574;
}

.contract-list {
  display: grid;
  grid-template-columns: minmax(80px, 0.6fr) minmax(0, 1fr);
  gap: 7px 10px;
  margin: 0;
  font-size: 0.8rem;
}

.contract-list dt {
  color: var(--muted);
}

.contract-list dd {
  overflow-wrap: anywhere;
  margin: 0;
}

.contract-json {
  overflow: auto;
  max-height: 260px;
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 0.76rem;
}

.ai-candidate p {
  margin-bottom: 0;
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.55;
}

.action-input p {
  margin-bottom: 0;
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.55;
}

.ownership-fields {
  display: grid;
  gap: 7px;
  margin-top: 10px;
}

.ownership-field {
  border-top: 1px solid var(--line);
  padding-top: 8px;
}

.ownership-field strong,
.ownership-field span,
.ownership-field small {
  display: block;
}

.ownership-field span,
.ownership-field small {
  margin-top: 3px;
  color: var(--muted);
  font-size: 0.72rem;
  line-height: 1.4;
}

.option-ownership {
  display: inline-flex;
  width: fit-content;
  margin: 0 0 6px 6px;
  border-radius: 999px;
  background: var(--accent-soft);
  padding: 2px 6px;
  color: var(--accent);
  font-size: 0.66rem;
  font-weight: 750;
}

@media (max-width: 1180px) {
  .workbench-grid {
    grid-template-columns: minmax(210px, 0.7fr) minmax(440px, 1.5fr);
  }

  .contract-panel {
    position: static;
    grid-column: 1 / -1;
    max-height: none;
  }
}

@media (max-width: 760px) {
  .topbar {
    align-items: stretch;
    flex-direction: column;
  }

  .connection-form {
    width: 100%;
  }

  .workbench-grid {
    grid-template-columns: 1fr;
  }

  .navigation-panel,
  .contract-panel {
    position: static;
    grid-column: auto;
    max-height: none;
  }

  .two-columns,
  .options-grid {
    grid-template-columns: 1fr;
  }
}
"""


_WORKBENCH_JS = r"""(() => {
  "use strict";

  const endpoints = Object.freeze({
    config: "/api/v1/workbench/config",
    plugins: "/api/v1/plugins",
    runs: "/api/v1/runs",
    uploads: "/api/v1/uploads",
    validate: "/api/v1/runs/validate",
  });

  const terminalStatuses = new Set([
    "succeeded",
    "degraded",
    "failed",
    "cancelled",
  ]);
  const actionRanks = Object.freeze({ disabled: 0, dry_run: 1, apply: 2 });
  const ownershipTargets = Object.freeze({
    program_fact: "ownership-program-fact",
    user_choice: "ownership-user-choice",
    ai_candidate: "ownership-ai-candidate",
    policy_locked: "ownership-policy-locked",
    action_input: "ownership-action-input",
  });
  const ownershipLabels = Object.freeze({
    program_fact: "Program fact",
    user_choice: "User choice",
    ai_candidate: "AI-assisted",
    policy_locked: "Policy locked",
    action_input: "Action input",
  });
  const state = {
    token: "",
    config: null,
    plugins: [],
    runs: [],
    selectedPluginId: "",
    currentRun: null,
    eventCursor: 0,
    eventKeys: new Set(),
    streamController: null,
    activeUpload: null,
    runChoices: new Map(),
  };

  const dom = {};

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = String(text);
    }
    return node;
  }

  function bindDom() {
    const ids = [
      "connection-form",
      "api-token",
      "connection-status",
      "refresh-runs",
      "plugin-list",
      "run-status-filter",
      "runs-list",
      "run-form",
      "plugin-select",
      "provider-select",
      "source-type",
      "file-source",
      "text-source",
      "json-source",
      "source-file",
      "source-text",
      "text-filename",
      "source-json",
      "plugin-options",
      "model-name",
      "timeout-seconds",
      "action-mode",
      "web-enrichment",
      "submit-run",
      "cancel-run",
      "download-artifact",
      "form-message",
      "current-run-status",
      "run-id-label",
      "timeline",
      "program-facts",
      "user-choices",
      "policy-facts",
      "candidate-note",
      "action-input-note",
      ...Object.values(ownershipTargets),
    ];
    for (const id of ids) {
      dom[id] = document.getElementById(id);
    }
  }

  function setMessage(target, message, tone) {
    target.textContent = message || "";
    target.className = "status-message";
    if (tone) {
      target.classList.add(`is-${tone}`);
    }
  }

  function requestHeaders(extra) {
    const headers = new Headers(extra || {});
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/json");
    }
    if (state.token) {
      headers.set("Authorization", `Bearer ${state.token}`);
    }
    return headers;
  }

  async function parseError(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      return `HTTP ${response.status}`;
    }
    const detail = payload && payload.detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((issue) => {
          const location = Array.isArray(issue.loc) ? issue.loc.join(".") : "request";
          const message = typeof issue.msg === "string" ? issue.msg : "Invalid value";
          return `${location}: ${message}`;
        })
        .join("; ");
    }
    return `HTTP ${response.status}`;
  }

  async function apiFetch(path, init) {
    const options = { ...(init || {}) };
    options.headers = requestHeaders(options.headers);
    const response = await fetch(path, options);
    if (!response.ok) {
      throw new ApiError(await parseError(response), response.status);
    }
    return response;
  }

  async function apiJson(path, init) {
    const response = await apiFetch(path, init);
    if (response.status === 204) {
      return null;
    }
    return response.json();
  }

  async function loadConfig() {
    try {
      return await apiJson(endpoints.config);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) {
        throw error;
      }
      const ready = await apiJson("/readyz");
      return {
        providers: Array.isArray(ready.providers) ? ready.providers : [],
        policy: {
          allow_web_enrichment: false,
          max_action_mode: "disabled",
        },
      };
    }
  }

  function configValue(name, fallback) {
    const config = state.config || {};
    const policy = config.policy || {};
    if (Object.hasOwn(config, name)) {
      return config[name];
    }
    if (Object.hasOwn(policy, name)) {
      return policy[name];
    }
    return fallback;
  }

  async function initialize() {
    stopEventStream();
    setMessage(dom["connection-status"], "正在连接服务…", "");
    try {
      const [config, pluginPayload] = await Promise.all([
        loadConfig(),
        apiJson(endpoints.plugins),
      ]);
      state.config = config || {};
      state.plugins = Array.isArray(pluginPayload.plugins) ? pluginPayload.plugins : [];
      renderProviders();
      renderPlugins();
      renderPolicy();
      await refreshRuns();
      dom["submit-run"].disabled = !Boolean(
        configValue("preflight_available", false),
      );
      if (!configValue("preflight_available", false)) {
        setMessage(
          dom["form-message"],
          "服务器未配置 Plugin preflight；Workbench 已禁用提交。",
          "error",
        );
      }
      setMessage(dom["connection-status"], "服务已连接", "success");
    } catch (error) {
      const message = error instanceof ApiError && error.status === 401
        ? "需要有效的 Bearer token"
        : safeErrorMessage(error);
      setMessage(dom["connection-status"], message, "error");
    }
  }

  function renderProviders() {
    const select = dom["provider-select"];
    select.replaceChildren();
    const providers = Array.isArray(state.config.providers) ? state.config.providers : [];
    for (const provider of providers) {
      if (typeof provider !== "string") {
        continue;
      }
      const option = element("option", "", provider);
      option.value = provider;
      select.append(option);
    }

    const maxMode = String(configValue("max_action_mode", "disabled"));
    const maxRank = actionRanks[maxMode] ?? actionRanks.disabled;
    const modeSelect = dom["action-mode"];
    modeSelect.replaceChildren();
    for (const mode of ["disabled", "dry_run", "apply"]) {
      if (actionRanks[mode] <= maxRank) {
        const option = element("option", "", mode);
        option.value = mode;
        modeSelect.append(option);
      }
    }
    dom["web-enrichment"].disabled = !Boolean(
      configValue("allow_web_enrichment", false),
    );
  }

  function renderPlugins() {
    dom["plugin-list"].replaceChildren();
    dom["plugin-select"].replaceChildren();
    for (const plugin of state.plugins) {
      const pluginId = typeof plugin.plugin_id === "string" ? plugin.plugin_id : "";
      if (!pluginId) {
        continue;
      }
      const label = plugin.description || plugin.display_name || pluginId;
      const button = element("button", "plugin-button", label);
      button.type = "button";
      button.dataset.pluginId = pluginId;
      button.addEventListener("click", () => selectPlugin(pluginId));
      dom["plugin-list"].append(button);

      const option = element("option", "", label);
      option.value = pluginId;
      dom["plugin-select"].append(option);
    }

    const firstId = state.plugins.length > 0 ? state.plugins[0].plugin_id : "";
    const selected = pluginById(state.selectedPluginId) ? state.selectedPluginId : firstId;
    selectPlugin(typeof selected === "string" ? selected : "");
  }

  function pluginById(pluginId) {
    return state.plugins.find((plugin) => plugin.plugin_id === pluginId) || null;
  }

  function declaredOwnershipFields(plugin) {
    const contract = plugin && plugin.ownership;
    if (!contract || !Array.isArray(contract.fields)) {
      return [];
    }
    return contract.fields.filter((field) => {
      return field && typeof field === "object" && Object.hasOwn(
        ownershipTargets,
        field.ownership,
      );
    });
  }

  function optionPointer(name) {
    const token = String(name).replaceAll("~", "~0").replaceAll("/", "~1");
    return `/${token}`;
  }

  function optionOwnership(plugin, name) {
    const path = optionPointer(name);
    return declaredOwnershipFields(plugin).find((field) => {
      return field.surface === "options" && field.path === path;
    }) || null;
  }

  function renderOwnership(plugin) {
    const fields = declaredOwnershipFields(plugin);
    const counts = {};
    for (const [ownership, targetId] of Object.entries(ownershipTargets)) {
      counts[ownership] = 0;
      dom[targetId].replaceChildren();
    }

    for (const field of fields) {
      const targetId = ownershipTargets[field.ownership];
      const item = element("div", "ownership-field");
      const label = typeof field.label === "string" && field.label
        ? field.label
        : field.path;
      const surface = typeof field.surface === "string" ? field.surface : "unknown";
      const path = typeof field.path === "string" ? field.path : "—";
      item.append(
        element("strong", "", label),
        element("span", "monospace", `${surface} · ${path}`),
      );
      if (typeof field.description === "string" && field.description) {
        item.append(element("small", "", field.description));
      }
      dom[targetId].append(item);
      counts[field.ownership] += 1;
    }

    for (const [ownership, targetId] of Object.entries(ownershipTargets)) {
      if (counts[ownership] === 0) {
        dom[targetId].append(element(
          "p",
          "field-note",
          `此 Plugin 未声明 ${ownershipLabels[ownership]} 字段。`,
        ));
      }
    }

    dom["candidate-note"].textContent = fields.length > 0
      ? "Provider 只负责生成契约标记的候选字段；Plugin 继续负责确定性校验和 Artifact 渲染。"
      : "Provider 产生候选内容，Plugin 再进行确定性校验与 Artifact 渲染。"
        + "当前 Manifest 尚未声明逐字段 ownership。";
  }

  function selectPlugin(pluginId) {
    state.selectedPluginId = pluginId;
    dom["plugin-select"].value = pluginId;
    for (const button of dom["plugin-list"].querySelectorAll("button")) {
      button.classList.toggle("is-selected", button.dataset.pluginId === pluginId);
    }
    renderOptions(pluginById(pluginId));
    renderOwnership(pluginById(pluginId));
    renderContractDraft();
  }

  function dereference(schema, rootSchema) {
    let current = schema;
    const visited = new Set();
    while (
      current
      && typeof current === "object"
      && typeof current.$ref === "string"
      && current.$ref.startsWith("#/$defs/")
      && !visited.has(current.$ref)
    ) {
      visited.add(current.$ref);
      const key = current.$ref.slice("#/$defs/".length);
      const target = rootSchema && rootSchema.$defs ? rootSchema.$defs[key] : null;
      if (!target || typeof target !== "object") {
        break;
      }
      const siblings = { ...current };
      delete siblings.$ref;
      current = { ...target, ...siblings };
    }
    return current && typeof current === "object" ? current : schema;
  }

  function unwrapNullable(schema, rootSchema) {
    const resolved = dereference(schema, rootSchema);
    if (!Array.isArray(resolved.anyOf)) {
      return resolved;
    }
    const nonNull = resolved.anyOf
      .map((item) => dereference(item, rootSchema))
      .filter((item) => item.type !== "null");
    if (nonNull.length !== 1) {
      return resolved;
    }
    const wrapperMetadata = { ...resolved };
    delete wrapperMetadata.anyOf;
    return { ...nonNull[0], ...wrapperMetadata };
  }

  function schemaAllowsNull(schema, rootSchema, visitedRefs = new Set()) {
    const reference = schema && typeof schema === "object" ? schema.$ref : null;
    if (typeof reference === "string") {
      if (visitedRefs.has(reference)) {
        return false;
      }
      visitedRefs = new Set(visitedRefs);
      visitedRefs.add(reference);
    }
    const resolved = dereference(schema, rootSchema);
    if (resolved.type === "null") {
      return true;
    }
    if (Array.isArray(resolved.type) && resolved.type.includes("null")) {
      return true;
    }
    for (const keyword of ["anyOf", "oneOf"]) {
      if (
        Array.isArray(resolved[keyword])
        && resolved[keyword].some((item) => schemaAllowsNull(
          item,
          rootSchema,
          visitedRefs,
        ))
      ) {
        return true;
      }
    }
    return false;
  }

  function controlKind(schema, rootSchema) {
    const resolved = unwrapNullable(schema, rootSchema);
    if (Array.isArray(resolved.enum)) {
      return "enum";
    }
    if (resolved.type === "boolean") {
      return "boolean";
    }
    if (resolved.type === "integer" || resolved.type === "number") {
      return resolved.type;
    }
    if (resolved.type === "string") {
      return "string";
    }
    if (resolved.type === "array") {
      const items = dereference(resolved.items || {}, rootSchema);
      if (items.type === "string") {
        return "string-array";
      }
    }
    return "json";
  }

  function renderOptions(plugin) {
    const container = dom["plugin-options"];
    container.replaceChildren();
    const rootSchema = plugin && plugin.options_schema;
    const properties = rootSchema && rootSchema.properties;
    if (!properties || typeof properties !== "object") {
      const label = element("label", "option-field is-wide");
      const heading = element("span", "", "Plugin 参数 JSON");
      const input = document.createElement("textarea");
      input.rows = 6;
      input.value = "{}";
      input.dataset.optionName = "__parameters__";
      input.dataset.controlKind = "json-object";
      label.append(heading, input);
      container.append(label);
      return;
    }

    const required = new Set(Array.isArray(rootSchema.required) ? rootSchema.required : []);
    let index = 0;
    for (const [name, rawSchema] of Object.entries(properties)) {
      if (name === "enrich_web") {
        continue;
      }
      const schema = unwrapNullable(rawSchema, rootSchema);
      const kind = controlKind(rawSchema, rootSchema);
      const wrapper = element("label", `option-field${kind === "json" ? " is-wide" : ""}`);
      const fieldTitle = typeof schema.title === "string" ? schema.title : name;
      const suffix = required.has(name) ? " *" : "";
      const heading = element("span", "", `${fieldTitle}${suffix}`);
      const input = createOptionControl(
        name,
        schema,
        kind,
        index,
        required.has(name),
        schemaAllowsNull(rawSchema, rootSchema),
      );
      wrapper.append(heading);
      const ownership = optionOwnership(plugin, name);
      if (ownership) {
        wrapper.append(element(
          "small",
          "option-ownership",
          ownershipLabels[ownership.ownership],
        ));
        if (ownership.ownership !== "user_choice") {
          input.disabled = true;
          input.dataset.omitOwnedValue = "true";
        }
      }
      wrapper.append(input);
      if (typeof schema.description === "string") {
        wrapper.append(element("small", "option-description", schema.description));
      }
      container.append(wrapper);
      index += 1;
    }

    if (container.childElementCount === 0) {
      container.append(element("p", "field-note", "此 Plugin 没有额外可编辑参数。"));
    }
  }

  function createOptionControl(name, schema, kind, index, isRequired, isNullable) {
    let input;
    if (kind === "enum") {
      input = document.createElement("select");
      if (!isRequired || isNullable) {
        const unset = element("option", "", "— 未设置 —");
        unset.value = "";
        if (isRequired && isNullable) {
          unset.dataset.encodedValue = "null";
        } else {
          unset.dataset.omitValue = "true";
        }
        unset.selected = !Object.hasOwn(schema, "default") || schema.default === null;
        input.append(unset);
      }
      for (const value of schema.enum) {
        const option = element("option", "", value);
        option.value = String(value);
        option.dataset.encodedValue = JSON.stringify(value);
        option.selected = Object.hasOwn(schema, "default") && schema.default === value;
        input.append(option);
      }
    } else if (kind === "boolean") {
      if (!isRequired || isNullable) {
        input = document.createElement("select");
        const unset = element("option", "", "— 未设置 —");
        unset.value = "";
        if (isRequired && isNullable) {
          unset.dataset.encodedValue = "null";
        } else {
          unset.dataset.omitValue = "true";
        }
        unset.selected = !Object.hasOwn(schema, "default") || schema.default === null;
        input.append(unset);
        for (const value of [true, false]) {
          const option = element("option", "", value ? "是" : "否");
          option.value = String(value);
          option.dataset.encodedValue = JSON.stringify(value);
          option.selected = Object.hasOwn(schema, "default") && schema.default === value;
          input.append(option);
        }
      } else {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(schema.default);
      }
    } else if (kind === "integer" || kind === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.step = kind === "integer" ? "1" : "any";
      if (typeof schema.minimum === "number") {
        input.min = String(schema.minimum);
      }
      if (typeof schema.maximum === "number") {
        input.max = String(schema.maximum);
      }
    } else if (kind === "json") {
      input = document.createElement("textarea");
      input.rows = 4;
      if (Object.hasOwn(schema, "default") && schema.default !== null) {
        input.value = JSON.stringify(schema.default, null, 2);
      }
    } else {
      input = document.createElement("input");
      input.type = "text";
      if (typeof schema.maxLength === "number") {
        input.maxLength = schema.maxLength;
      }
    }

    input.id = `plugin-option-${index}`;
    input.dataset.optionName = name;
    input.dataset.controlKind = kind;
    input.dataset.nullWhenEmpty = String(Boolean(isRequired && isNullable));
    input.required = Boolean(isRequired && !isNullable && kind !== "boolean");
    if (
      kind !== "boolean"
      && kind !== "enum"
      && kind !== "json"
      && Object.hasOwn(schema, "default")
      && schema.default !== null
    ) {
      input.value = kind === "string-array" && Array.isArray(schema.default)
        ? schema.default.join(", ")
        : String(schema.default);
    }
    input.addEventListener("input", renderContractDraft);
    input.addEventListener("change", renderContractDraft);
    return input;
  }

  function readPluginOptions() {
    const controls = dom["plugin-options"].querySelectorAll("[data-option-name]");
    const parameters = {};
    for (const control of controls) {
      if (control.dataset.omitOwnedValue === "true") {
        continue;
      }
      const name = control.dataset.optionName;
      const kind = control.dataset.controlKind;
      if (name === "__parameters__") {
        const value = parseJsonObject(control.value, "Plugin 参数");
        Object.assign(parameters, value);
      } else if (kind === "enum") {
        const selected = control.options[control.selectedIndex];
        if (selected && selected.dataset.omitValue !== "true") {
          parameters[name] = JSON.parse(selected.dataset.encodedValue);
        }
      } else if (kind === "boolean") {
        if (control.tagName === "SELECT") {
          const selected = control.options[control.selectedIndex];
          if (selected && selected.dataset.omitValue !== "true") {
            parameters[name] = JSON.parse(selected.dataset.encodedValue);
          }
        } else {
          parameters[name] = control.checked;
        }
      } else if (kind === "integer") {
        if (control.value !== "") {
          parameters[name] = Number.parseInt(control.value, 10);
        } else if (control.dataset.nullWhenEmpty === "true") {
          parameters[name] = null;
        }
      } else if (kind === "number") {
        if (control.value !== "") {
          parameters[name] = Number(control.value);
        } else if (control.dataset.nullWhenEmpty === "true") {
          parameters[name] = null;
        }
      } else if (kind === "string-array") {
        const values = control.value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean);
        if (values.length > 0) {
          parameters[name] = values;
        } else if (control.dataset.nullWhenEmpty === "true") {
          parameters[name] = null;
        }
      } else if (kind === "json") {
        if (control.value.trim()) {
          parameters[name] = JSON.parse(control.value);
        } else if (control.dataset.nullWhenEmpty === "true") {
          parameters[name] = null;
        }
      } else if (control.value !== "") {
        parameters[name] = control.value;
      } else if (control.dataset.nullWhenEmpty === "true") {
        parameters[name] = null;
      }
    }
    return parameters;
  }

  function parseJsonObject(value, label) {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`${label}必须是 JSON object`);
    }
    return parsed;
  }

  function renderDefinitionList(target, entries) {
    target.replaceChildren();
    for (const [label, rawValue] of entries) {
      const value = rawValue === null || rawValue === undefined || rawValue === ""
        ? "—"
        : String(rawValue);
      target.append(element("dt", "", label), element("dd", "", value));
    }
  }

  function renderPolicy() {
    const maxMode = configValue("max_action_mode", "disabled");
    const allowWeb = Boolean(configValue("allow_web_enrichment", false));
    const maxUpload = configValue("max_upload_bytes", null);
    renderDefinitionList(dom["policy-facts"], [
      ["最高 Action", maxMode],
      ["Web enrichment", allowWeb ? "允许" : "禁止"],
      ["单文件上限", formatBytes(maxUpload)],
      ["Run history", configValue("durable_history", false) ? "本地持久化" : "仅当前进程"],
      ["Plugin preflight", configValue("preflight_available", false) ? "启用" : "不可用"],
    ]);
  }

  function draftChoiceSummary(parameters) {
    const sourceType = dom["source-type"].value;
    const file = dom["source-file"].files[0];
    const model = dom["model-name"].value.trim();
    return {
      plugin_id: dom["plugin-select"].value || null,
      provider: dom["provider-select"].value || null,
      model: model || null,
      source: {
        type: sourceType,
        filename: file ? file.name : currentSourceLabel(),
      },
      action_mode: dom["action-mode"].value,
      enrich_web: dom["web-enrichment"].checked,
      timeout_seconds: Number(dom["timeout-seconds"].value),
      parameters,
    };
  }

  function renderContractDraft() {
    const plugin = pluginById(dom["plugin-select"].value);
    let parameters = {};
    try {
      parameters = readPluginOptions();
    } catch (_error) {
      parameters = { status: "参数 JSON 尚未完成" };
    }
    const file = dom["source-file"].files[0];
    renderDefinitionList(dom["program-facts"], [
      ["Plugin version", plugin ? plugin.plugin_version : "—"],
      ["输入文件", file ? file.name : currentSourceLabel()],
      ["媒体类型", file ? file.type || "application/octet-stream" : "待服务解析"],
      ["SHA-256", "提交后由程序计算"],
    ]);
    dom["user-choices"].textContent = JSON.stringify(
      draftChoiceSummary(parameters),
      null,
      2,
    );
  }

  function renderRunContract(run) {
    renderDefinitionList(dom["program-facts"], [
      ["Plugin version", run.plugin_version],
      ["输入文件", run.input_filename],
      ["媒体类型", run.input_media_type],
      ["SHA-256", run.input_sha256],
      ["Parent Run", run.parent_run_id],
    ]);
    const remembered = state.runChoices.get(run.run_id);
    dom["user-choices"].textContent = JSON.stringify(
      remembered || {
        plugin_id: run.plugin_id,
        provider: run.provider,
        model: run.model,
        detail: "详细选择未写入安全运行历史",
      },
      null,
      2,
    );
  }

  function currentSourceLabel() {
    if (dom["source-type"].value === "text") {
      return dom["text-filename"].value || "input.txt";
    }
    if (dom["source-type"].value === "json") {
      return "input.json";
    }
    return "尚未选择";
  }

  async function refreshRuns() {
    const params = new URLSearchParams({ limit: "50" });
    const selectedStatus = dom["run-status-filter"].value;
    if (selectedStatus) {
      params.set("status", selectedStatus);
    }
    const payload = await apiJson(`${endpoints.runs}?${params.toString()}`);
    state.runs = Array.isArray(payload.runs) ? payload.runs : [];
    renderRuns();
  }

  function renderRuns() {
    const list = dom["runs-list"];
    list.replaceChildren();
    if (state.runs.length === 0) {
      list.append(element("p", "field-note", "没有匹配的任务。"));
      return;
    }
    for (const run of state.runs) {
      const button = element("button", "run-button");
      button.type = "button";
      button.classList.toggle(
        "is-selected",
        Boolean(state.currentRun && state.currentRun.run_id === run.run_id),
      );
      button.append(
        element("strong", "", `${run.plugin_id || "unknown"} · ${statusLabel(run.status)}`),
        element("span", "", run.input_filename || shortRunId(run.run_id)),
      );
      button.addEventListener("click", () => openRun(run.run_id));
      list.append(button);
    }
  }

  async function openRun(runId) {
    stopEventStream();
    state.eventCursor = 0;
    state.eventKeys.clear();
    dom.timeline.replaceChildren();
    try {
      const run = await apiJson(`${endpoints.runs}/${encodeURIComponent(runId)}`);
      if (pluginById(run.plugin_id)) {
        selectPlugin(run.plugin_id);
      }
      state.currentRun = run;
      renderRunState(run);
      renderRuns();
      await streamEvents(run.run_id);
    } catch (error) {
      setMessage(dom["form-message"], safeErrorMessage(error), "error");
    }
  }

  function renderRunState(run) {
    state.currentRun = run;
    dom["run-id-label"].textContent = run.run_id;
    dom["current-run-status"].textContent = statusLabel(run.status);
    dom["current-run-status"].dataset.status = run.status;
    const terminal = terminalStatuses.has(run.status);
    dom["cancel-run"].disabled = terminal;
    dom["download-artifact"].disabled = !run.artifact_available;
    renderRunContract(run);
    if (run.error && typeof run.error.message === "string") {
      setMessage(dom["form-message"], run.error.message, "error");
    } else if (Array.isArray(run.warnings) && run.warnings.length > 0) {
      setMessage(dom["form-message"], run.warnings.join(" · "), "");
    } else {
      setMessage(dom["form-message"], "", "");
    }
  }

  function statusLabel(status) {
    return {
      queued: "排队中",
      running: "运行中",
      succeeded: "成功",
      degraded: "部分完成",
      failed: "失败",
      cancelled: "已取消",
    }[status] || String(status || "未知");
  }

  function shortRunId(runId) {
    return typeof runId === "string" ? runId.slice(0, 12) : "unknown";
  }

  function stopEventStream() {
    if (state.streamController) {
      state.streamController.abort();
      state.streamController = null;
    }
  }

  async function streamEvents(runId) {
    stopEventStream();
    const controller = new AbortController();
    state.streamController = controller;
    const params = new URLSearchParams({ after: String(state.eventCursor) });
    const headers = requestHeaders({ Accept: "text/event-stream" });
    let response;
    try {
      response = await fetch(
        `${endpoints.runs}/${encodeURIComponent(runId)}/events?${params.toString()}`,
        { headers, signal: controller.signal },
      );
      if (!response.ok) {
        throw new ApiError(await parseError(response), response.status);
      }
      if (!response.body) {
        throw new Error("浏览器不支持流式事件读取");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        buffer = consumeSseFrames(buffer);
        if (chunk.done) {
          break;
        }
      }
      consumeSseFrames(`${buffer}\n\n`);
      const run = await apiJson(`${endpoints.runs}/${encodeURIComponent(runId)}`);
      renderRunState(run);
      await refreshRuns();
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      setMessage(dom["form-message"], safeErrorMessage(error), "error");
    } finally {
      if (state.streamController === controller) {
        state.streamController = null;
      }
    }
  }

  function consumeSseFrames(buffer) {
    let remaining = buffer.replaceAll("\r\n", "\n");
    let boundary = remaining.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = remaining.slice(0, boundary);
      remaining = remaining.slice(boundary + 2);
      if (frame && !frame.startsWith(":")) {
        consumeSseFrame(frame);
      }
      boundary = remaining.indexOf("\n\n");
    }
    return remaining;
  }

  function consumeSseFrame(frame) {
    let eventName = "message";
    let eventId = null;
    const dataLines = [];
    for (const line of frame.split("\n")) {
      const separator = line.indexOf(":");
      const field = separator < 0 ? line : line.slice(0, separator);
      const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
      if (field === "event") {
        eventName = value;
      } else if (field === "id") {
        eventId = value;
      } else if (field === "data") {
        dataLines.push(value);
      }
    }
    if (eventId && /^\d+$/.test(eventId)) {
      state.eventCursor = Math.max(state.eventCursor, Number(eventId));
    }
    if (dataLines.length === 0) {
      return;
    }
    try {
      const payload = JSON.parse(dataLines.join("\n"));
      appendTimelineEvent(eventName, payload);
    } catch (_error) {
      appendTimelineEvent(eventName, { status: "invalid event payload" });
    }
  }

  function appendTimelineEvent(eventName, payload) {
    const sequence = Number.isInteger(payload.sequence) ? payload.sequence : state.eventCursor;
    const key = `${payload.run_id || "run"}:${sequence}:${eventName}`;
    if (state.eventKeys.has(key)) {
      return;
    }
    state.eventKeys.add(key);
    const item = element("li", "timeline-item");
    const title = element("strong", "", eventName);
    const details = eventName === "stream.gap"
      ? [
        `历史已截断：从 ${payload.first_available_sequence || "?"} 开始回放`,
      ]
      : [statusLabel(payload.status)];
    if (payload.node_id) {
      details.push(`${payload.node_kind || "step"}: ${payload.node_id}`);
    }
    if (payload.node_status) {
      details.push(`node ${payload.node_status}`);
    }
    if (payload.attempt) {
      details.push(`attempt ${payload.attempt}/${payload.max_attempts || "?"}`);
    }
    if (Number.isInteger(payload.batch_size)) {
      details.push(`batch ${payload.batch_size}`);
    }
    if (Number.isInteger(payload.accepted_count)) {
      details.push(`accepted ${payload.accepted_count}`);
    }
    if (typeof payload.delay_seconds === "number") {
      details.push(`retry in ${payload.delay_seconds}s`);
    }
    if (typeof payload.duration_ms === "number") {
      details.push(`${Math.round(payload.duration_ms)}ms`);
    }
    if (payload.warning_count) {
      details.push(`${payload.warning_count} warnings`);
    }
    if (payload.error_code) {
      details.push(`error: ${payload.error_code}`);
    }
    if (payload.artifact_filename) {
      details.push(`artifact: ${payload.artifact_filename}`);
    }
    item.append(title, element("span", "", details.join(" · ")));
    dom.timeline.append(item);
  }

  async function buildSource() {
    const sourceType = dom["source-type"].value;
    if (sourceType === "file") {
      const file = dom["source-file"].files[0];
      if (!file) {
        throw new Error("请选择一个输入文件");
      }
      const fileKey = [file.name, file.size, file.lastModified, file.type].join(":");
      if (state.activeUpload && state.activeUpload.fileKey === fileKey) {
        return state.activeUpload.source;
      }
      await discardActiveUpload();
      const form = new FormData();
      form.append("file", file, file.name);
      const uploaded = await apiJson(endpoints.uploads, { method: "POST", body: form });
      const source = { type: "upload", upload_id: uploaded.upload_id };
      state.activeUpload = { fileKey, source };
      return source;
    }
    if (sourceType === "json") {
      return {
        type: "inline",
        data: parseJsonObject(dom["source-json"].value, "输入"),
      };
    }
    if (!dom["source-text"].value) {
      throw new Error("请输入文本内容");
    }
    return {
      type: "text",
      text: dom["source-text"].value,
      filename: dom["text-filename"].value || "input.txt",
      media_type: "text/plain; charset=utf-8",
    };
  }

  async function discardActiveUpload() {
    const active = state.activeUpload;
    state.activeUpload = null;
    if (!active || !active.source || !active.source.upload_id) {
      return;
    }
    try {
      await apiFetch(
        `${endpoints.uploads}/${encodeURIComponent(active.source.upload_id)}`,
        { method: "DELETE" },
      );
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) {
        throw error;
      }
    }
  }

  async function buildRunSpec() {
    const model = dom["model-name"].value.trim();
    return {
      plugin_id: dom["plugin-select"].value,
      provider: dom["provider-select"].value,
      source: await buildSource(),
      sink: { type: "artifact" },
      options: {
        model: model || null,
        enrich_web: dom["web-enrichment"].checked,
        timeout_seconds: Number(dom["timeout-seconds"].value),
        action_mode: dom["action-mode"].value,
        parameters: readPluginOptions(),
      },
    };
  }

  async function validateRunSpec(spec) {
    const response = await fetch(endpoints.validate, {
      method: "POST",
      headers: requestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(spec),
    });
    if (response.status === 204) {
      return;
    }
    if (!response.ok) {
      throw new ApiError(await parseError(response), response.status);
    }
    throw new ApiError("Plugin preflight 返回了非预期响应", response.status);
  }

  async function submitRun(event) {
    event.preventDefault();
    dom["submit-run"].disabled = true;
    setMessage(dom["form-message"], "正在验证并提交…", "");
    try {
      const spec = await buildRunSpec();
      await validateRunSpec(spec);
      const run = await apiJson(endpoints.runs, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(spec),
      });
      state.eventCursor = 0;
      state.eventKeys.clear();
      dom.timeline.replaceChildren();
      state.runChoices.set(run.run_id, draftChoiceSummary(spec.options.parameters));
      renderRunState(run);
      discardActiveUpload().catch(() => {});
      setMessage(dom["form-message"], "任务已进入队列", "success");
      await refreshRuns();
      await streamEvents(run.run_id);
    } catch (error) {
      setMessage(dom["form-message"], safeErrorMessage(error), "error");
    } finally {
      dom["submit-run"].disabled = !Boolean(
        configValue("preflight_available", false),
      );
    }
  }

  async function cancelCurrentRun() {
    if (!state.currentRun || terminalStatuses.has(state.currentRun.status)) {
      return;
    }
    try {
      const run = await apiJson(
        `${endpoints.runs}/${encodeURIComponent(state.currentRun.run_id)}/cancel`,
        { method: "POST" },
      );
      renderRunState(run);
      setMessage(dom["form-message"], "取消请求已提交", "success");
    } catch (error) {
      setMessage(dom["form-message"], safeErrorMessage(error), "error");
    }
  }

  function safeDownloadFilename(response, runId) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const candidate = match ? match[1] : `agent-artifact-${shortRunId(runId)}`;
    const safe = candidate.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128);
    return safe || "agent-artifact.bin";
  }

  async function downloadCurrentArtifact() {
    if (!state.currentRun || !state.currentRun.artifact_available) {
      return;
    }
    const runId = state.currentRun.run_id;
    try {
      const response = await apiFetch(
        `${endpoints.runs}/${encodeURIComponent(runId)}/artifact`,
        { headers: { Accept: "application/octet-stream" } },
      );
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = safeDownloadFilename(response, runId);
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (error) {
      setMessage(dom["form-message"], safeErrorMessage(error), "error");
    }
  }

  function updateSourcePanels() {
    const selected = dom["source-type"].value;
    if (selected !== "file" && state.activeUpload) {
      discardActiveUpload().catch(() => {});
    }
    dom["file-source"].hidden = selected !== "file";
    dom["text-source"].hidden = selected !== "text";
    dom["json-source"].hidden = selected !== "json";
    renderContractDraft();
  }

  function formatBytes(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "由服务器限制";
    }
    if (value < 1024) {
      return `${value} B`;
    }
    if (value < 1024 * 1024) {
      return `${(value / 1024).toFixed(1)} KiB`;
    }
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function safeErrorMessage(error) {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return "操作失败";
  }

  function bindEvents() {
    dom["connection-form"].addEventListener("submit", (event) => {
      event.preventDefault();
      state.token = dom["api-token"].value;
      dom["api-token"].value = "";
      initialize();
    });
    dom["refresh-runs"].addEventListener("click", () => {
      refreshRuns().catch((error) => {
        setMessage(dom["connection-status"], safeErrorMessage(error), "error");
      });
    });
    dom["run-status-filter"].addEventListener("change", () => {
      refreshRuns().catch((error) => {
        setMessage(dom["connection-status"], safeErrorMessage(error), "error");
      });
    });
    dom["plugin-select"].addEventListener("change", () => {
      selectPlugin(dom["plugin-select"].value);
    });
    dom["source-type"].addEventListener("change", updateSourcePanels);
    dom["source-file"].addEventListener("change", () => {
      discardActiveUpload().catch(() => {});
      renderContractDraft();
    });
    dom["text-filename"].addEventListener("input", renderContractDraft);
    dom["provider-select"].addEventListener("change", renderContractDraft);
    dom["model-name"].addEventListener("input", renderContractDraft);
    dom["timeout-seconds"].addEventListener("input", renderContractDraft);
    dom["action-mode"].addEventListener("change", renderContractDraft);
    dom["web-enrichment"].addEventListener("change", renderContractDraft);
    dom["run-form"].addEventListener("submit", submitRun);
    dom["cancel-run"].addEventListener("click", cancelCurrentRun);
    dom["download-artifact"].addEventListener("click", downloadCurrentArtifact);
    window.addEventListener("beforeunload", stopEventStream);
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindDom();
    bindEvents();
    updateSourcePanels();
    initialize();
  });
})();
"""


WORKBENCH_INDEX: Final = WorkbenchAsset.from_text(
    "/workbench",
    "text/html; charset=utf-8",
    _WORKBENCH_HTML,
)
WORKBENCH_STYLES: Final = WorkbenchAsset.from_text(
    "/workbench/workbench.css",
    "text/css; charset=utf-8",
    _WORKBENCH_CSS,
)
WORKBENCH_SCRIPT: Final = WorkbenchAsset.from_text(
    "/workbench/workbench.js",
    "text/javascript; charset=utf-8",
    _WORKBENCH_JS,
)

WORKBENCH_ASSETS: Final[Mapping[str, WorkbenchAsset]] = MappingProxyType(
    {asset.route: asset for asset in (WORKBENCH_INDEX, WORKBENCH_STYLES, WORKBENCH_SCRIPT)}
)


def get_workbench_asset(route: str) -> WorkbenchAsset:
    """Return an asset for an exact public route or raise ``KeyError``."""

    return WORKBENCH_ASSETS[route]


__all__ = [
    "WORKBENCH_ASSETS",
    "WORKBENCH_CONTENT_SECURITY_POLICY",
    "WORKBENCH_INDEX",
    "WORKBENCH_SCRIPT",
    "WORKBENCH_SECURITY_HEADERS",
    "WORKBENCH_STYLES",
    "WorkbenchAsset",
    "get_workbench_asset",
]
