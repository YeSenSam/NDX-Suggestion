const REFRESH_INTERVAL_MS = 60_000;

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function statusClass(status) {
  if (status === "Nasdaq 官方" || status === "官方在线" || status === "官方指数口径") return "good";
  if (status === "Nasdaq 官方缓存") return "warn";
  if (status === "官方缓存") return "warn";
  if (status === "民间转载缓存") return "warn";
  if (
    status === "民间源" ||
    status === "民间源复算" ||
    status === "民间源在线" ||
    status === "民间源在线复算" ||
    status === "民间源缓存" ||
    status === "民间源缓存复算" ||
    status === "混合源"
  ) return "warn";
  return "muted";
}

function renderStatus(data) {
  document.getElementById("board-status").textContent = data.meta.boardStatus;
  document.getElementById("board-summary").textContent =
    `${data.meta.boardSummary} 上次刷新：${data.meta.fetchedAtLocal}。`;

  const chips = [
    { value: "NDX", label: "Nasdaq 官方" },
    { value: "SPX", label: "Yahoo 民间源" },
    { value: `${data.meta.refreshSeconds} 秒`, label: "自动刷新周期" },
  ];

  document.getElementById("status-chips").innerHTML = chips
    .map((chip) => `
      <div class="chip">
        <strong>${escapeHtml(chip.value)}</strong>
        <span>${escapeHtml(chip.label)}</span>
      </div>
    `)
    .join("");
}

function renderHighlights(indexKey, indexData) {
  document.getElementById(`${indexKey}-summary`).textContent = indexData.summary;
  document.getElementById(`${indexKey}-highlights`).innerHTML = indexData.highlights
    .map((item) => `
      <div class="mini-stat">
        <strong>${escapeHtml(item.value)}</strong>
        <span>${escapeHtml(item.label)}</span>
        <small>${escapeHtml(item.asOf)} · ${escapeHtml(item.status)}</small>
      </div>
    `)
    .join("");
}

function renderOfficialIndexMetrics(metrics) {
  document.getElementById("index-metrics-table").innerHTML = metrics
    .map((metric) => `
      <tr>
        <td><strong>${escapeHtml(metric.name)}</strong></td>
        <td>${escapeHtml(metric.ndx)}</td>
        <td>${escapeHtml(metric.spx)}</td>
        <td>
          <a href="${metric.sourceUrlNdx}" target="_blank" rel="noreferrer">Nasdaq</a>
          /
          <a href="${metric.sourceUrlSpx}" target="_blank" rel="noreferrer">S&P DJI</a>
          <br>
          <span>${escapeHtml(metric.sourceLabel)}</span>
        </td>
        <td>${escapeHtml(metric.asOf)}</td>
        <td><span class="status-badge ${statusClass(metric.status)}">${escapeHtml(metric.status)}</span></td>
        <td>${escapeHtml(metric.note)}</td>
      </tr>
    `)
    .join("");
}

function renderMacroMetrics(metrics) {
  document.getElementById("macro-metrics-table").innerHTML = metrics
    .map((metric) => `
      <tr>
        <td><strong>${escapeHtml(metric.name)}</strong></td>
        <td>${escapeHtml(metric.value)}</td>
        <td>${escapeHtml(metric.asOf)}</td>
        <td><a href="${metric.sourceUrl}" target="_blank" rel="noreferrer">${escapeHtml(metric.sourceLabel)}</a></td>
        <td><span class="status-badge ${statusClass(metric.status)}">${escapeHtml(metric.status)}</span></td>
        <td>${escapeHtml(metric.note)}</td>
      </tr>
    `)
    .join("");
}

function renderStrategy(strategy) {
  document.getElementById("strategy-action").textContent = strategy.action;
  document.getElementById("strategy-score").textContent = `${strategy.score} / 100`;
  document.getElementById("strategy-allocation").textContent = strategy.allocation;
  document.getElementById("strategy-asof").textContent = `生成时间：${strategy.asOf}`;
  document.getElementById("strategy-factors").innerHTML = strategy.factorScores
    .map((factor) => `
      <div class="factor-row">
        <div>
          <strong>${escapeHtml(factor.name)}</strong>
          <span>${escapeHtml(factor.comment)}</span>
        </div>
        <b>${escapeHtml(factor.score)} / 100</b>
      </div>
    `)
    .join("");
  document.getElementById("strategy-snapshot").innerHTML = strategy.dataSnapshot
    .map((item) => `
      <div class="mini-stat compact">
        <strong>${escapeHtml(item.value)}</strong>
        <span>${escapeHtml(item.label)}</span>
      </div>
    `)
    .join("");
  document.getElementById("strategy-analysis").innerHTML = strategy.analysis
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  document.getElementById("strategy-reasons").innerHTML = strategy.reasons
    .map((reason) => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  document.getElementById("strategy-plan").innerHTML = strategy.executionPlan
    .map((step) => `<li>${escapeHtml(step)}</li>`)
    .join("");
  document.getElementById("strategy-risks").innerHTML = strategy.riskNotes
    .map((risk) => `<li>${escapeHtml(risk)}</li>`)
    .join("");
  document.getElementById("strategy-disclaimer").textContent = strategy.disclaimer;
}

function renderSentimentScore(module) {
  document.getElementById("sentiment-action").textContent = module.action;
  document.getElementById("sentiment-score").textContent = `${module.score} / 100`;
  document.getElementById("sentiment-allocation").textContent = module.allocation;
  document.getElementById("sentiment-asof").textContent = `生成时间：${module.asOf}`;
  document.getElementById("sentiment-weights").innerHTML = module.weights
    .map((item) => `
      <div class="factor-row">
        <div>
          <strong>${escapeHtml(item.name)} · 权重 ${escapeHtml(item.weight)}</strong>
          <span>当前值：${escapeHtml(item.value)} · ${escapeHtml(item.status)}</span>
        </div>
        <b>${escapeHtml(item.score)} / 100</b>
      </div>
    `)
    .join("");
  document.getElementById("sentiment-analysis").innerHTML = module.analysis
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  document.getElementById("sentiment-sources").innerHTML = module.sources
    .map((source) => `
      <li>
        <strong><a href="${source.url}" target="_blank" rel="noreferrer">${escapeHtml(source.label)}</a></strong><br>
        截至：${escapeHtml(source.asOf)}
      </li>
    `)
    .join("");
  document.getElementById("sentiment-naaim").innerHTML = module.recentNaaim
    .map((item) => `
      <div class="mini-stat compact">
        <strong>${escapeHtml(item.value.toFixed(2))}</strong>
        <span>${escapeHtml(item.date)}</span>
      </div>
    `)
    .join("");
  document.getElementById("sentiment-note").textContent = module.note;
}

function renderDcaEngine(engine) {
  document.getElementById("dca-action").textContent = engine.action;
  document.getElementById("dca-multiplier").textContent = `${engine.multiplier.toFixed(2)}x`;
  document.getElementById("dca-summary").textContent = engine.summary;
  document.getElementById("dca-asof").textContent = `生成时间：${engine.asOf}`;
  document.getElementById("dca-inputs").innerHTML = engine.inputs
    .map((item) => `
      <div class="mini-stat compact">
        <strong>${escapeHtml(item.value)}</strong>
        <span>${escapeHtml(item.label)}</span>
      </div>
    `)
    .join("");
  document.getElementById("dca-rules").innerHTML = engine.rules
    .map((rule) => `<li>${escapeHtml(rule)}</li>`)
    .join("");
  document.getElementById("dca-execution").innerHTML = engine.execution
    .map((step) => `<li>${escapeHtml(step)}</li>`)
    .join("");
  document.getElementById("dca-warning").textContent = engine.warning;
}

function renderRules(rules) {
  document.getElementById("rules-list").innerHTML = rules
    .map((rule) => `<li><strong>${escapeHtml(rule.title)}</strong><br>${escapeHtml(rule.body)}</li>`)
    .join("");
}

function renderSources(sources) {
  document.getElementById("sources-list").innerHTML = sources
    .map((source) => `<li><strong>${escapeHtml(source.title)}</strong><br>${escapeHtml(source.body)}</li>`)
    .join("");
}

function renderDashboard(data) {
  renderStatus(data);
  renderHighlights("ndx", data.indices.ndx);
  renderHighlights("spx", data.indices.spx);
  renderStrategy(data.strategy);
  renderSentimentScore(data.sentimentScore);
  renderDcaEngine(data.dcaEngine);
  renderOfficialIndexMetrics(data.officialIndexMetrics);
  renderMacroMetrics(data.macroMetrics);
  renderRules(data.rules);
  renderSources(data.sources);
}

function renderError(message) {
  document.getElementById("board-status").textContent = "刷新失败";
  document.getElementById("board-summary").textContent = message;
}

async function loadDashboard() {
  const response = await fetch("/api/dashboard", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}`);
  }
  renderDashboard(await response.json());
}

async function refreshLoop() {
  try {
    await loadDashboard();
  } catch (error) {
    renderError(`数据接口暂时不可用：${error.message}`);
  } finally {
    window.setTimeout(refreshLoop, REFRESH_INTERVAL_MS);
  }
}

refreshLoop();
