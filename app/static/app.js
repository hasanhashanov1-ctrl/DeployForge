const ACTIVE_STATUSES = new Set(["queued", "cloning", "building", "starting"]);
const ACTIVE_LIFECYCLE_STATES = new Set(["stopping", "starting", "rolling_back"]);

const STATUS = {
  queued: { label: "В очереди", tone: "progress", detail: "Задача ожидает свободный worker." },
  cloning: { label: "Клонирование", tone: "progress", detail: "Загружаем исходный код из GitHub." },
  building: { label: "Сборка", tone: "progress", detail: "Docker собирает новый образ приложения." },
  starting: { label: "Запуск", tone: "progress", detail: "Контейнер запускается и проходит проверку." },
  running: { label: "Работает", tone: "running", detail: "Последняя версия доступна по публичному адресу." },
  failed: { label: "Ошибка", tone: "failed", detail: "Деплой завершился с ошибкой." },
  stopped: { label: "Остановлен", tone: "neutral", detail: "Версия больше не обслуживает трафик." },
  cancelled: { label: "Отменён", tone: "neutral", detail: "Деплой остановлен по запросу пользователя." },
};

const LIFECYCLE_STATUS = {
  stopping: { label: "Останавливается", tone: "progress", detail: "Worker корректно останавливает контейнер приложения." },
  stopped: { label: "Остановлен", tone: "neutral", detail: "Приложение не запущено, но его контейнер и образ сохранены." },
  starting: { label: "Запускается", tone: "progress", detail: "Worker запускает сохранённую версию и проверяет её готовность." },
  rolling_back: { label: "Выполняется откат", tone: "progress", detail: "Worker переключает сервис на выбранную прошлую версию." },
};

const state = {
  projects: [],
  selectedProjectId: localStorage.getItem("deployforge:selected-project"),
  deployments: [],
  variables: [],
  variablesProjectId: null,
  variablesDirty: false,
  selectedDeploymentId: null,
  logs: null,
  logType: "build",
  tab: "deployments",
  projectFormMode: "create",
  logStream: null,
  logStreamDeploymentId: null,
  pollingTimer: null,
};

const elements = {
  healthPill: document.querySelector("#health-pill"),
  healthLabel: document.querySelector("#health-label"),
  metricProjects: document.querySelector("#metric-projects"),
  metricRunning: document.querySelector("#metric-running"),
  metricAttention: document.querySelector("#metric-attention"),
  projectList: document.querySelector("#project-list"),
  emptyState: document.querySelector("#empty-state"),
  projectDetail: document.querySelector("#project-detail"),
  projectName: document.querySelector("#project-name"),
  projectMonogram: document.querySelector("#project-monogram"),
  projectStatus: document.querySelector("#project-status"),
  projectRepo: document.querySelector("#project-repo"),
  projectMeta: document.querySelector("#project-meta"),
  settingsGrid: document.querySelector("#settings-grid"),
  environmentList: document.querySelector("#environment-list"),
  saveVariables: document.querySelector("#save-variables"),
  activityStrip: document.querySelector("#activity-strip"),
  activityTitle: document.querySelector("#activity-title"),
  activityCopy: document.querySelector("#activity-copy"),
  activityTime: document.querySelector("#activity-time"),
  deployButton: document.querySelector("#deploy-button"),
  cancelDeploymentButton: document.querySelector("#cancel-deployment-button"),
  lifecycleButton: document.querySelector("#lifecycle-button"),
  openAppButton: document.querySelector("#open-app-button"),
  deploymentList: document.querySelector("#deployment-list"),
  logContext: document.querySelector("#log-context"),
  logViewer: document.querySelector("#log-viewer code"),
  logTail: document.querySelector("#log-tail"),
  liveLogStatus: document.querySelector("#live-log-status"),
  liveLogLabel: document.querySelector("#live-log-label"),
  projectModal: document.querySelector("#project-modal"),
  projectForm: document.querySelector("#project-form"),
  modalEyebrow: document.querySelector("#modal-eyebrow"),
  modalTitle: document.querySelector("#modal-title"),
  modalCopy: document.querySelector("#modal-copy"),
  slugHelp: document.querySelector("#slug-help"),
  portHelp: document.querySelector("#port-help"),
  formError: document.querySelector("#form-error"),
  createProjectSubmit: document.querySelector("#create-project-submit"),
  editProjectButton: document.querySelector("#edit-project-button"),
  deleteModal: document.querySelector("#delete-modal"),
  confirmDelete: document.querySelector("#confirm-delete"),
  toastRegion: document.querySelector("#toast-region"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getStatus(status) {
  return STATUS[status] ?? { label: "Нет деплоев", tone: "neutral", detail: "" };
}

function getProjectStatus(project, deploymentStatus = project.latest_status) {
  return LIFECYCLE_STATUS[project.lifecycle_state] ?? getStatus(deploymentStatus);
}

function initials(name) {
  const chunks = String(name || "DF").trim().split(/\s+/).filter(Boolean);
  return chunks.slice(0, 2).map((chunk) => chunk[0]).join("").toUpperCase();
}

function shortSha(sha) {
  return sha ? sha.slice(0, 9) : "—";
}

function formatDate(value, includeTime = true) {
  if (!value) return "—";
  const options = includeTime
    ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "short", year: "numeric" };
  return new Intl.DateTimeFormat("ru-RU", options).format(new Date(value));
}

function duration(startedAt, finishedAt) {
  if (!startedAt) return "—";
  const end = finishedAt ? new Date(finishedAt) : new Date();
  const seconds = Math.max(0, Math.floor((end - new Date(startedAt)) / 1000));
  if (seconds < 60) return `${seconds} сек`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes} мин ${rest} сек`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });

  if (!response.ok) {
    let detail = `Запрос завершился с кодом ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      if (Array.isArray(body.detail)) {
        detail = body.detail.map((item) => item.msg).filter(Boolean).join("; ") || detail;
      }
    } catch (_error) {
      // Сервер мог вернуть пустой ответ.
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204 || response.headers.get("content-length") === "0") return null;
  return response.json();
}

function toast(title, message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.innerHTML = `
    <span class="toast-mark">${type === "error" ? "!" : "✓"}</span>
    <div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>
  `;
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 5200);
}

async function checkHealth() {
  try {
    await api("/health");
    elements.healthPill.className = "health-pill ok";
    elements.healthLabel.textContent = "Все системы работают";
  } catch (_error) {
    elements.healthPill.className = "health-pill error";
    elements.healthLabel.textContent = "Система недоступна";
  }
}

function selectedProject() {
  return state.projects.find((project) => project.id === state.selectedProjectId) ?? null;
}

function renderMetrics() {
  const running = state.projects.filter(
    (project) => project.latest_status === "running" && project.lifecycle_state === "active",
  ).length;
  const attention = state.projects.filter(
    (project) => project.latest_status === "failed" || project.cleanup_error || project.operation_error,
  ).length;
  elements.metricProjects.textContent = state.projects.length;
  elements.metricRunning.textContent = running;
  elements.metricAttention.textContent = attention;
}

function renderProjectList() {
  if (!state.projects.length) {
    elements.projectList.innerHTML = `
      <div class="list-empty">Проектов пока нет.<br />Создай первый и запусти деплой.</div>
    `;
    return;
  }

  elements.projectList.innerHTML = state.projects
    .map((project) => {
      const status = getProjectStatus(project);
      const active = project.id === state.selectedProjectId ? "active" : "";
      return `
        <button class="project-card ${active}" type="button" data-project-id="${escapeHtml(project.id)}">
          <span class="mini-monogram">${escapeHtml(initials(project.name))}</span>
          <span>
            <strong>${escapeHtml(project.name)}</strong>
            <small>${escapeHtml(project.slug)}.localhost</small>
          </span>
          <span class="status-dot ${status.tone}" title="${escapeHtml(status.label)}"></span>
        </button>
      `;
    })
    .join("");
}

function renderProjectDetail() {
  const project = selectedProject();
  if (!project) {
    elements.emptyState.classList.remove("hidden");
    elements.projectDetail.classList.add("hidden");
    return;
  }

  const latest = state.deployments[0] ?? null;
  const activeDeployment = state.deployments.find((item) => ACTIVE_STATUSES.has(item.status)) ?? null;
  const runningDeployment = state.deployments.find((item) => item.status === "running") ?? null;
  const deploymentInProgress = Boolean(activeDeployment);
  const lifecycleInProgress = ACTIVE_LIFECYCLE_STATES.has(project.lifecycle_state);
  const inProgress = deploymentInProgress || lifecycleInProgress;
  const status = deploymentInProgress
    ? getStatus(activeDeployment.status)
    : getProjectStatus(project, runningDeployment?.status ?? latest?.status);

  elements.emptyState.classList.add("hidden");
  elements.projectDetail.classList.remove("hidden");
  elements.projectName.textContent = project.name;
  elements.projectMonogram.textContent = initials(project.name);
  elements.projectRepo.textContent = project.repo_url.replace("https://github.com/", "github.com/");
  elements.projectRepo.href = project.repo_url;
  elements.projectStatus.textContent = status.label;
  elements.projectStatus.className = `status-badge ${status.tone}`;

  elements.openAppButton.href = project.public_url;
  elements.openAppButton.classList.toggle(
    "hidden",
    !runningDeployment || project.lifecycle_state !== "active",
  );
  const canStop = Boolean(runningDeployment) && project.lifecycle_state === "active" && !deploymentInProgress;
  const canStart = project.lifecycle_state === "stopped" && !deploymentInProgress;
  elements.lifecycleButton.classList.toggle("hidden", !canStop && !canStart && !lifecycleInProgress);
  elements.lifecycleButton.disabled = lifecycleInProgress;
  elements.lifecycleButton.dataset.action = canStart ? "start" : "stop";
  elements.lifecycleButton.innerHTML = lifecycleInProgress
    ? `<span class="activity-spinner" aria-hidden="true"></span> ${escapeHtml(status.label)}`
    : canStart
      ? '<span aria-hidden="true">▶</span> Запустить снова'
      : '<span aria-hidden="true">■</span> Остановить';
  elements.deployButton.disabled = inProgress
    || project.state === "deleting"
    || !["active", "stopped"].includes(project.lifecycle_state);
  elements.deployButton.innerHTML = deploymentInProgress
    ? '<span class="activity-spinner" aria-hidden="true"></span> Деплой выполняется'
    : '<span class="deploy-icon" aria-hidden="true">▲</span> Запустить деплой';
  elements.cancelDeploymentButton.classList.toggle("hidden", !activeDeployment);
  elements.cancelDeploymentButton.disabled = Boolean(activeDeployment?.cancel_requested);
  elements.cancelDeploymentButton.textContent = activeDeployment?.cancel_requested
    ? "Отмена запрошена…"
    : "× Отменить деплой";

  elements.projectMeta.innerHTML = [
    ["Публичный адрес", project.public_url],
    ["Ветка", project.branch || "ветка по умолчанию"],
    ["Dockerfile", project.dockerfile_path],
    ["Порт", project.container_port],
  ]
    .map(([label, value]) => `<div class="meta-item"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");

  elements.settingsGrid.innerHTML = [
    ["Project ID", project.id],
    ["Slug", project.slug],
    ["Репозиторий", project.repo_url],
    ["Ветка", project.branch || "автоматически"],
    ["Dockerfile", project.dockerfile_path],
    ["Container port", project.container_port],
    ["Создан", formatDate(project.created_at)],
    ["Состояние", status.label],
  ]
    .map(([label, value]) => `<div class="setting-card"><span>${label}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`)
    .join("");
  elements.editProjectButton.disabled = inProgress || project.state === "deleting";

  elements.activityStrip.classList.toggle("hidden", !inProgress);
  if (inProgress) {
    elements.activityTitle.textContent = status.label;
    elements.activityCopy.textContent = status.detail;
    elements.activityTime.textContent = lifecycleInProgress
      ? "фоновая операция"
      : duration(
        activeDeployment.started_at || activeDeployment.created_at,
        null,
      );
  }

  renderDeployments();
  renderLogs();
  if (state.variablesProjectId === project.id && !state.variablesDirty) renderVariables();
}

function renderDeployments() {
  if (!state.deployments.length) {
    elements.deploymentList.innerHTML = `
      <div class="list-empty">История пуста. Запусти первый деплой проекта.</div>
    `;
    return;
  }

  elements.deploymentList.innerHTML = state.deployments
    .map((deployment, index) => {
      const status = getStatus(deployment.status);
      const selected = deployment.id === state.selectedDeploymentId ? "selected" : "";
      const label = index === 0 ? "Последний деплой" : `Деплой #${state.deployments.length - index}`;
      const canRollback = deployment.status === "stopped"
        && Boolean(deployment.container_id)
        && Boolean(deployment.image_tag)
        && selectedProject()?.lifecycle_state === "active"
        && state.deployments.some((item) => item.status === "running");
      return `
        <div class="deployment-row ${selected}" role="button" tabindex="0" data-deployment-id="${escapeHtml(deployment.id)}">
          <span class="deployment-status-line ${status.tone}"></span>
          <span class="deployment-primary">
            <strong>${escapeHtml(label)}</strong>
            <span>${escapeHtml(formatDate(deployment.created_at))}</span>
          </span>
          <span class="deployment-cell">
            <strong>${escapeHtml(deployment.error_message || deployment.image_tag || "Образ ещё не создан")}</strong>
            <span>${escapeHtml(status.label)}</span>
          </span>
          <span class="deployment-cell">
            <strong>${escapeHtml(shortSha(deployment.commit_sha))}</strong>
            <span>${escapeHtml(duration(deployment.started_at, deployment.finished_at))}</span>
          </span>
          ${canRollback ? `<button class="button button-ghost button-small rollback-button" type="button" data-rollback-id="${escapeHtml(deployment.id)}">Откатить</button>` : '<span class="deployment-open" aria-hidden="true">›</span>'}
        </div>
      `;
    })
    .join("");
}

function renderLogs() {
  const deployment = state.deployments.find((item) => item.id === state.selectedDeploymentId);
  if (!deployment) {
    elements.logContext.textContent = "Выбери деплой в истории, чтобы посмотреть его журнал.";
    elements.logViewer.textContent = "Логи пока не выбраны.";
    return;
  }

  elements.logContext.textContent = `${formatDate(deployment.created_at)} · ${getStatus(deployment.status).label} · ${shortSha(deployment.commit_sha)}`;
  if (!state.logs || state.logs.deployment_id !== deployment.id) {
    elements.logViewer.textContent = "Загружаем журнал…";
    return;
  }

  const value = state.logType === "build" ? state.logs.build_log : state.logs.runtime_log;
  const truncated = state.logType === "build"
    ? state.logs.build_log_truncated
    : state.logs.runtime_log_truncated;
  const prefix = truncated ? "[Показан сохранённый хвост журнала]\n\n" : "";
  elements.logViewer.textContent = value ? prefix + value : "Для этого этапа записей пока нет.";
}

function renderVariables() {
  if (!state.variables.length) {
    elements.environmentList.innerHTML = `
      <div class="environment-empty">Переменных пока нет. Добавь первую конфигурацию для контейнера.</div>
    `;
    return;
  }

  elements.environmentList.innerHTML = state.variables
    .map((variable, index) => {
      const preserved = variable.preserved ? "true" : "false";
      const placeholder = variable.preserved ? "Секрет сохранён — введи только для замены" : "Значение";
      return `
        <div class="environment-row" data-variable-index="${index}" data-preserved="${preserved}" data-original-key="${escapeHtml(variable.originalKey || "")}">
          <input
            type="text"
            data-variable-field="key"
            value="${escapeHtml(variable.key)}"
            placeholder="DATABASE_URL"
            aria-label="Ключ переменной"
            autocomplete="off"
          />
          <input
            type="${variable.is_secret ? "password" : "text"}"
            data-variable-field="value"
            data-changed="${variable.changed ? "true" : "false"}"
            value="${escapeHtml(variable.value)}"
            placeholder="${escapeHtml(placeholder)}"
            aria-label="Значение переменной"
            autocomplete="new-password"
          />
          <label class="secret-toggle" title="Скрывать значение в API и интерфейсе">
            <input type="checkbox" data-variable-field="secret" ${variable.is_secret ? "checked" : ""} aria-label="Секретная переменная" />
          </label>
          <button class="icon-button remove-variable" type="button" data-remove-variable="${index}" aria-label="Удалить переменную">×</button>
        </div>
      `;
    })
    .join("");
}

async function loadVariables(projectId) {
  const variables = await api(`/projects/${projectId}/variables`);
  state.variables = variables.map((variable) => ({
    key: variable.key,
    originalKey: variable.key,
    value: variable.value ?? "",
    is_secret: variable.is_secret,
    preserved: variable.is_secret && variable.has_value,
    changed: false,
  }));
  state.variablesProjectId = projectId;
  state.variablesDirty = false;
  renderVariables();
}

async function loadDeployments(projectId, { keepSelection = true } = {}) {
  const previousSelection = state.selectedDeploymentId;
  state.deployments = await api(`/projects/${projectId}/deployments`);
  if (!keepSelection || !state.deployments.some((item) => item.id === state.selectedDeploymentId)) {
    state.selectedDeploymentId = state.deployments[0]?.id ?? null;
    state.logs = null;
  }
  if (previousSelection !== state.selectedDeploymentId) stopLogStream();
}

async function loadProjects({ preserveProject = true, silent = false } = {}) {
  try {
    const projects = await api("/projects");
    state.projects = projects;
    const selectedStillExists = projects.some((project) => project.id === state.selectedProjectId);

    if (!preserveProject || !selectedStillExists) {
      stopLogStream();
      state.selectedProjectId = projects[0]?.id ?? null;
      state.selectedDeploymentId = null;
      state.logs = null;
      state.variables = [];
      state.variablesProjectId = null;
      state.variablesDirty = false;
    }

    if (state.selectedProjectId) {
      localStorage.setItem("deployforge:selected-project", state.selectedProjectId);
      const requests = [loadDeployments(state.selectedProjectId)];
      if (state.variablesProjectId !== state.selectedProjectId) {
        requests.push(loadVariables(state.selectedProjectId));
      }
      await Promise.all(requests);
    } else {
      localStorage.removeItem("deployforge:selected-project");
      state.deployments = [];
    }

    renderMetrics();
    renderProjectList();
    renderProjectDetail();
    schedulePolling();
  } catch (error) {
    if (!silent) toast("Не удалось загрузить проекты", error.message, "error");
    elements.projectList.innerHTML = '<div class="list-empty">API недоступен. Проверь состояние системы.</div>';
  }
}

async function selectProject(projectId) {
  if (projectId === state.selectedProjectId) return;
  stopLogStream();
  state.selectedProjectId = projectId;
  state.selectedDeploymentId = null;
  state.logs = null;
  state.variables = [];
  state.variablesProjectId = null;
  state.variablesDirty = false;
  localStorage.setItem("deployforge:selected-project", projectId);
  renderProjectList();
  elements.deploymentList.innerHTML = '<div class="list-empty">Загружаем историю…</div>';
  try {
    await Promise.all([
      loadDeployments(projectId, { keepSelection: false }),
      loadVariables(projectId),
    ]);
    renderProjectDetail();
    schedulePolling();
  } catch (error) {
    toast("Не удалось открыть проект", error.message, "error");
  }
}

async function selectDeployment(deploymentId, openLogs = true) {
  stopLogStream();
  state.selectedDeploymentId = deploymentId;
  state.logs = null;
  renderDeployments();
  renderLogs();
  if (openLogs) setTab("logs");
  await loadLogs();
}

async function loadLogs() {
  if (!state.selectedDeploymentId) return;
  renderLogs();
  try {
    state.logs = await api(`/deployments/${state.selectedDeploymentId}/logs?tail=${elements.logTail.value}`);
    renderLogs();
  } catch (error) {
    elements.logViewer.textContent = `Не удалось загрузить логи: ${error.message}`;
  }
}

function setLiveLogStatus(tone, label) {
  elements.liveLogStatus.className = `live-log-status ${tone}`;
  elements.liveLogLabel.textContent = label;
}

function stopLogStream(label = "Ожидание") {
  if (state.logStream) state.logStream.close();
  state.logStream = null;
  state.logStreamDeploymentId = null;
  setLiveLogStatus("idle", label);
}

function startLogStream() {
  if (state.tab !== "logs" || !state.selectedDeploymentId) {
    stopLogStream();
    return;
  }
  if (
    state.logStream
    && state.logStreamDeploymentId === state.selectedDeploymentId
  ) return;

  stopLogStream();
  const deploymentId = state.selectedDeploymentId;
  const source = new EventSource(
    `/deployments/${deploymentId}/logs/stream?tail=${elements.logTail.value}`,
  );
  state.logStream = source;
  state.logStreamDeploymentId = deploymentId;
  setLiveLogStatus("connecting", "Подключение…");

  source.onopen = () => {
    if (state.logStream === source) setLiveLogStatus("live", "Живое обновление");
  };
  source.onmessage = (event) => {
    if (state.logStream !== source) return;
    try {
      const logs = JSON.parse(event.data);
      state.logs = logs;
      const deployment = state.deployments.find((item) => item.id === deploymentId);
      if (deployment) deployment.status = logs.status;
      renderDeployments();
      renderLogs();
    } catch (_error) {
      setLiveLogStatus("connecting", "Ошибка данных");
    }
  };
  source.addEventListener("complete", () => {
    if (state.logStream !== source) return;
    stopLogStream("Деплой завершён");
  });
  source.onerror = () => {
    if (state.logStream === source) setLiveLogStatus("connecting", "Переподключение…");
  };
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  ["deployments", "logs", "settings"].forEach((name) => {
    document.querySelector(`#${name}-panel`).classList.toggle("hidden", name !== tab);
  });
  if (tab === "logs") startLogStream();
  else stopLogStream();
}

function setLogType(type) {
  state.logType = type;
  document.querySelectorAll(".segment").forEach((button) => {
    button.classList.toggle("active", button.dataset.log === type);
  });
  renderLogs();
}

async function deployProject() {
  const project = selectedProject();
  if (!project) return;
  elements.deployButton.disabled = true;
  try {
    const deployment = await api(`/projects/${project.id}/deploy`, { method: "POST" });
    state.deployments.unshift(deployment);
    state.selectedDeploymentId = deployment.id;
    state.logs = null;
    renderProjectDetail();
    toast("Деплой поставлен в очередь", `${project.name}: worker уже получил задачу.`);
    schedulePolling(true);
  } catch (error) {
    toast("Не удалось запустить деплой", error.message, "error");
    elements.deployButton.disabled = false;
  }
}

async function cancelDeployment() {
  const deployment = state.deployments.find((item) => ACTIVE_STATUSES.has(item.status));
  if (!deployment || deployment.cancel_requested) return;
  elements.cancelDeploymentButton.disabled = true;
  try {
    const updated = await api(`/deployments/${deployment.id}/cancel`, { method: "POST" });
    state.deployments = state.deployments.map((item) => (
      item.id === updated.id ? updated : item
    ));
    renderProjectDetail();
    toast(
      updated.status === "cancelled" ? "Деплой отменён" : "Отмена запрошена",
      updated.status === "cancelled"
        ? "Задача не успела начаться и удалена из активной очереди."
        : "Worker завершит текущую безопасную операцию и очистит кандидата.",
    );
    schedulePolling(true);
  } catch (error) {
    toast("Не удалось отменить деплой", error.message, "error");
    elements.cancelDeploymentButton.disabled = false;
  }
}

async function changeLifecycle() {
  const project = selectedProject();
  if (!project) return;
  const action = elements.lifecycleButton.dataset.action;
  if (!action || !["stop", "start"].includes(action)) return;
  elements.lifecycleButton.disabled = true;
  try {
    await api(`/projects/${project.id}/${action}`, { method: "POST" });
    toast(
      action === "stop" ? "Остановка началась" : "Запуск начался",
      action === "stop"
        ? "Worker сохранит контейнер, чтобы его можно было запустить снова."
        : "Worker запускает сохранённую версию и проверяет её готовность.",
    );
    await loadProjects({ silent: true });
    schedulePolling(true);
  } catch (error) {
    toast(action === "stop" ? "Не удалось остановить проект" : "Не удалось запустить проект", error.message, "error");
    elements.lifecycleButton.disabled = false;
  }
}

async function rollbackDeployment(deploymentId) {
  const project = selectedProject();
  const deployment = state.deployments.find((item) => item.id === deploymentId);
  if (!project || !deployment) return;
  const version = shortSha(deployment.commit_sha);
  if (!window.confirm(`Вернуть ${project.name} к версии ${version}? Текущая версия будет сохранена.`)) return;
  try {
    await api(`/projects/${project.id}/rollback`, {
      method: "POST",
      body: JSON.stringify({ deployment_id: deployment.id }),
    });
    toast("Откат начался", `Worker переключает сервис на версию ${version}.`);
    await loadProjects({ silent: true });
    schedulePolling(true);
  } catch (error) {
    toast("Не удалось выполнить откат", error.message, "error");
  }
}

function addVariable() {
  state.variables.push({
    key: "",
    originalKey: "",
    value: "",
    is_secret: true,
    preserved: false,
    changed: false,
  });
  state.variablesDirty = true;
  renderVariables();
  const keyInputs = elements.environmentList.querySelectorAll('[data-variable-field="key"]');
  keyInputs[keyInputs.length - 1]?.focus();
}

async function saveVariables() {
  const project = selectedProject();
  if (!project) return;
  const rows = [...elements.environmentList.querySelectorAll(".environment-row")];
  const variables = [];
  const keys = new Set();

  for (const row of rows) {
    const keyInput = row.querySelector('[data-variable-field="key"]');
    const valueInput = row.querySelector('[data-variable-field="value"]');
    const secretInput = row.querySelector('[data-variable-field="secret"]');
    const key = keyInput.value.trim();
    const preserved = row.dataset.preserved === "true";
    const changed = valueInput.dataset.changed === "true";

    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key)) {
      toast("Проверь ключ переменной", `${key || "Пустой ключ"}: разрешены буквы, цифры и подчёркивания.`, "error");
      keyInput.focus();
      return;
    }
    if (keys.has(key)) {
      toast("Повторяющийся ключ", `${key} указан больше одного раза.`, "error");
      keyInput.focus();
      return;
    }
    if (preserved && row.dataset.originalKey !== key && !changed) {
      toast("Нужно значение секрета", `Введи новое значение, чтобы переименовать ${row.dataset.originalKey}.`, "error");
      valueInput.focus();
      return;
    }
    keys.add(key);
    variables.push({
      key,
      value: preserved && !changed ? null : valueInput.value,
      is_secret: secretInput.checked,
    });
  }

  elements.saveVariables.disabled = true;
  try {
    const updated = await api(`/projects/${project.id}/variables`, {
      method: "PUT",
      body: JSON.stringify({ variables }),
    });
    state.variables = updated.map((variable) => ({
      key: variable.key,
      originalKey: variable.key,
      value: variable.value ?? "",
      is_secret: variable.is_secret,
      preserved: variable.is_secret && variable.has_value,
      changed: false,
    }));
    state.variablesProjectId = project.id;
    state.variablesDirty = false;
    renderVariables();
    toast("Переменные сохранены", "Они применятся при следующем деплое проекта.");
  } catch (error) {
    toast("Не удалось сохранить переменные", error.message, "error");
  } finally {
    elements.saveVariables.disabled = false;
  }
}

function openCreateModal() {
  state.projectFormMode = "create";
  elements.projectForm.reset();
  elements.projectForm.elements.dockerfile_path.value = "Dockerfile";
  elements.projectForm.elements.slug.readOnly = false;
  elements.projectForm.elements.container_port.readOnly = false;
  elements.projectForm.elements.slug.dataset.edited = "false";
  elements.modalEyebrow.textContent = "NEW PROJECT";
  elements.modalTitle.textContent = "Подключить репозиторий";
  elements.modalCopy.textContent = "DeployForge поддерживает доверенные публичные GitHub-репозитории с Dockerfile.";
  elements.slugHelp.textContent = "Станет адресом: slug.localhost";
  elements.portHelp.textContent = "Порт, который слушает приложение внутри контейнера";
  elements.createProjectSubmit.textContent = "Создать проект";
  elements.formError.classList.add("hidden");
  elements.projectModal.showModal();
  window.setTimeout(() => elements.projectForm.elements.name.focus(), 0);
}

function openEditModal() {
  const project = selectedProject();
  if (!project) return;
  state.projectFormMode = "edit";
  const form = elements.projectForm.elements;
  form.name.value = project.name;
  form.slug.value = project.slug;
  form.repo_url.value = project.repo_url;
  form.branch.value = project.branch || "";
  form.dockerfile_path.value = project.dockerfile_path;
  form.container_port.value = project.container_port;
  const hasContainer = state.deployments.some((item) => Boolean(item.container_id));
  form.slug.readOnly = hasContainer;
  form.container_port.readOnly = hasContainer;
  form.slug.dataset.edited = "true";
  elements.modalEyebrow.textContent = "PROJECT SETTINGS";
  elements.modalTitle.textContent = "Редактировать проект";
  elements.modalCopy.textContent = "Новые настройки репозитория и сборки применятся при следующем деплое.";
  elements.slugHelp.textContent = hasContainer
    ? "Slug закреплён, потому что у проекта уже есть контейнеры"
    : "Станет адресом: slug.localhost";
  elements.portHelp.textContent = hasContainer
    ? "Порт закреплён для совместимости с сохранёнными версиями"
    : "Порт, который слушает приложение внутри контейнера";
  elements.createProjectSubmit.textContent = "Сохранить изменения";
  elements.formError.classList.add("hidden");
  elements.projectModal.showModal();
  window.setTimeout(() => form.name.focus(), 0);
}

function closeCreateModal() {
  elements.projectModal.close();
}

async function submitProjectForm(event) {
  event.preventDefault();
  const form = new FormData(elements.projectForm);
  const payload = {
    name: form.get("name"),
    slug: form.get("slug"),
    repo_url: form.get("repo_url"),
    branch: form.get("branch") || null,
    dockerfile_path: form.get("dockerfile_path") || "Dockerfile",
    container_port: Number(form.get("container_port")),
  };

  elements.createProjectSubmit.disabled = true;
  elements.formError.classList.add("hidden");
  try {
    const editing = state.projectFormMode === "edit";
    const selected = selectedProject();
    const project = await api(editing ? `/projects/${selected.id}` : "/projects", {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    closeCreateModal();
    state.selectedProjectId = project.id;
    if (!editing) {
      stopLogStream();
      state.selectedDeploymentId = null;
      state.logs = null;
      state.variables = [];
      state.variablesProjectId = null;
      state.variablesDirty = false;
    }
    await loadProjects();
    toast(
      editing ? "Настройки сохранены" : "Проект создан",
      editing
        ? "Изменения сборки применятся при следующем деплое."
        : `${project.name} готов к первому деплою.`,
    );
  } catch (error) {
    elements.formError.textContent = error.message;
    elements.formError.classList.remove("hidden");
  } finally {
    elements.createProjectSubmit.disabled = false;
  }
}

function openDeleteModal() {
  if (selectedProject()) elements.deleteModal.showModal();
}

async function deleteProject(event) {
  event.preventDefault();
  const project = selectedProject();
  if (!project) return;
  elements.confirmDelete.disabled = true;
  try {
    await api(`/projects/${project.id}`, { method: "DELETE" });
    stopLogStream();
    elements.deleteModal.close();
    toast("Удаление запущено", `${project.name} очищается в фоне.`);
    state.selectedProjectId = null;
    state.selectedDeploymentId = null;
    state.logs = null;
    state.variables = [];
    state.variablesProjectId = null;
    state.variablesDirty = false;
    await loadProjects({ preserveProject: false });
  } catch (error) {
    toast("Не удалось удалить проект", error.message, "error");
  } finally {
    elements.confirmDelete.disabled = false;
  }
}

function schedulePolling(immediate = false) {
  window.clearTimeout(state.pollingTimer);
  const hasActive = state.deployments.some((item) => ACTIVE_STATUSES.has(item.status))
    || ACTIVE_LIFECYCLE_STATES.has(selectedProject()?.lifecycle_state);
  const delay = immediate ? 700 : hasActive ? 2500 : 12000;
  state.pollingTimer = window.setTimeout(async () => {
    await loadProjects({ silent: true });
    if (state.tab === "logs" && state.selectedDeploymentId && !state.logStream) {
      await loadLogs();
    }
  }, delay);
}

document.querySelector("#project-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-project-id]");
  if (button) selectProject(button.dataset.projectId);
});

document.querySelector("#deployment-list").addEventListener("click", (event) => {
  const rollback = event.target.closest("[data-rollback-id]");
  if (rollback) {
    event.stopPropagation();
    rollbackDeployment(rollback.dataset.rollbackId);
    return;
  }
  const button = event.target.closest("[data-deployment-id]");
  if (button) selectDeployment(button.dataset.deploymentId);
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => setLogType(button.dataset.log));
});

document.querySelectorAll("#new-project-button, #empty-create-button").forEach((button) => {
  button.addEventListener("click", openCreateModal);
});

document.querySelectorAll("[data-close-modal]").forEach((button) => {
  button.addEventListener("click", closeCreateModal);
});

document.querySelectorAll("[data-close-delete]").forEach((button) => {
  button.addEventListener("click", () => elements.deleteModal.close());
});

document.querySelectorAll("#delete-project-button, #delete-project-secondary").forEach((button) => {
  button.addEventListener("click", openDeleteModal);
});

elements.projectForm.addEventListener("submit", submitProjectForm);
elements.editProjectButton.addEventListener("click", openEditModal);
document.querySelector("#delete-form").addEventListener("submit", deleteProject);
elements.deployButton.addEventListener("click", deployProject);
elements.cancelDeploymentButton.addEventListener("click", cancelDeployment);
elements.lifecycleButton.addEventListener("click", changeLifecycle);
document.querySelector("#refresh-projects").addEventListener("click", () => loadProjects());
document.querySelector("#refresh-logs").addEventListener("click", loadLogs);
elements.logTail.addEventListener("change", async () => {
  await loadLogs();
  startLogStream();
});
document.querySelector("#add-variable").addEventListener("click", addVariable);
elements.saveVariables.addEventListener("click", saveVariables);

elements.environmentList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-variable]");
  if (!button) return;
  state.variables.splice(Number(button.dataset.removeVariable), 1);
  state.variablesDirty = true;
  renderVariables();
});

elements.environmentList.addEventListener("input", (event) => {
  const row = event.target.closest(".environment-row");
  if (!row) return;
  const variable = state.variables[Number(row.dataset.variableIndex)];
  state.variablesDirty = true;
  if (event.target.dataset.variableField === "key") variable.key = event.target.value;
  if (event.target.dataset.variableField === "value") {
    variable.value = event.target.value;
    variable.changed = true;
    event.target.dataset.changed = "true";
  }
});

elements.environmentList.addEventListener("change", (event) => {
  if (event.target.dataset.variableField !== "secret") return;
  const row = event.target.closest(".environment-row");
  const valueInput = row.querySelector('[data-variable-field="value"]');
  state.variables[Number(row.dataset.variableIndex)].is_secret = event.target.checked;
  valueInput.type = event.target.checked ? "password" : "text";
  state.variablesDirty = true;
});

elements.projectForm.elements.name.addEventListener("input", (event) => {
  const slugInput = elements.projectForm.elements.slug;
  if (slugInput.dataset.edited === "true") return;
  slugInput.value = event.target.value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9а-яё\s-]/gi, "")
    .replace(/[а-яё]/gi, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
});

elements.projectForm.elements.slug.addEventListener("input", () => {
  elements.projectForm.elements.slug.dataset.edited = "true";
});

elements.projectModal.addEventListener("click", (event) => {
  if (event.target === elements.projectModal) closeCreateModal();
});

elements.deleteModal.addEventListener("click", (event) => {
  if (event.target === elements.deleteModal) elements.deleteModal.close();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadProjects({ silent: true });
});

checkHealth();
loadProjects();
