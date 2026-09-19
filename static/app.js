function readChartData(element) {
  const raw = element.dataset.chart;
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (error) {
    console.error("No se pudo leer el JSON del grafico", error);
    return null;
  }
}

function resizeCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
  const width = Math.max(Math.round(rect.width), 1);
  const height = Math.max(Math.round(rect.height), 1);
  const pixelWidth = Math.round(width * ratio);
  const pixelHeight = Math.round(height * ratio);

  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width, height };
}

function finiteNumber(value) {
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function drawEmptyState(ctx, width, height, message = "Sin datos suficientes") {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#9aa8bb";
  ctx.font = "16px Aptos, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(message, width / 2, height / 2);
}

function formatMoney(value) {
  return `${(finiteNumber(value) || 0).toLocaleString("es-ES", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })} EUR`;
}

function renderDonutLegend(canvas, data, total) {
  const existingLegend =
    canvas.nextElementSibling && canvas.nextElementSibling.classList.contains("chart-legend")
      ? canvas.nextElementSibling
      : null;
  const rows = Array.isArray(data)
    ? data.filter((item) => (finiteNumber(item && item.value) || 0) > 0)
    : [];

  if (!rows.length || total <= 0) {
    if (existingLegend) existingLegend.remove();
    return;
  }

  const legend = existingLegend || document.createElement("div");
  legend.className = "chart-legend";
  legend.replaceChildren();

  rows.forEach((item) => {
    const value = finiteNumber(item.value) || 0;
    const percent = total > 0 ? (value / total) * 100 : 0;
    const row = document.createElement("article");
    row.className = "chart-legend-item";

    const swatch = document.createElement("span");
    swatch.className = "chart-legend-swatch";
    swatch.style.background = item.color || "#56CCF2";
    swatch.setAttribute("aria-hidden", "true");

    const copy = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = item.label || "Sin categoria";
    const detail = document.createElement("small");
    detail.textContent = `${percent.toLocaleString("es-ES", { maximumFractionDigits: 1 })}%`;
    copy.append(label, detail);

    const amount = document.createElement("span");
    amount.className = "chart-legend-value";
    amount.textContent = formatMoney(value);

    row.append(swatch, copy, amount);
    legend.append(row);
  });

  if (!existingLegend) {
    canvas.insertAdjacentElement("afterend", legend);
  }
}

function drawDonut(canvas, data) {
  const { ctx, width, height } = resizeCanvas(canvas);
  const slices = Array.isArray(data)
    ? data
        .map((item) => ({ ...item, value: finiteNumber(item && item.value) }))
        .filter((item) => item.value !== null && item.value > 0)
    : [];
  if (!slices.length) {
    renderDonutLegend(canvas, [], 0);
    drawEmptyState(ctx, width, height);
    return;
  }

  const total = slices.reduce((sum, item) => sum + item.value, 0);
  if (total <= 0) {
    renderDonutLegend(canvas, [], 0);
    drawEmptyState(ctx, width, height);
    return;
  }

  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(width, height) * 0.28;
  const lineWidth = Math.max(20, radius * 0.35);
  ctx.clearRect(0, 0, width, height);

  let startAngle = -Math.PI / 2;
  slices.forEach((item) => {
    const value = item.value;
    const slice = (value / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.strokeStyle = item.color || "#56CCF2";
    ctx.lineWidth = lineWidth;
    ctx.lineCap = "round";
    ctx.arc(centerX, centerY, radius, startAngle, startAngle + slice);
    ctx.stroke();
    startAngle += slice;
  });

  ctx.fillStyle = "#9aa8bb";
  ctx.font = "13px Aptos, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("Total", centerX, centerY - 8);
  ctx.fillStyle = "#f4f7fb";
  ctx.font = "700 22px Bahnschrift, Aptos, sans-serif";
  ctx.fillText(`${total.toLocaleString("es-ES", { maximumFractionDigits: 0 })} EUR`, centerX, centerY + 18);
  renderDonutLegend(canvas, slices, total);
}

function normalizeLineData(data, fallbackColor = "#56CCF2") {
  if (!data) return { labels: [], series: [] };
  const labels = Array.isArray(data.labels) ? data.labels.map((label) => String(label ?? "")) : [];
  if (Array.isArray(data.series)) {
    if (data.series.length && typeof data.series[0] !== "object") {
      return {
        labels,
        series: [{ label: "Serie", values: data.series, color: fallbackColor }],
      };
    }
    return {
      labels,
      series: data.series,
    };
  }
  if (Array.isArray(data.total)) {
    return {
      labels,
      series: [{ label: "Total", values: data.total, color: fallbackColor }],
    };
  }
  return { labels: [], series: [] };
}

function renderLineLegend(canvas, seriesList) {
  const existingLegend =
    canvas.nextElementSibling && canvas.nextElementSibling.classList.contains("chart-series-legend")
      ? canvas.nextElementSibling
      : null;

  if (!seriesList.length) {
    if (existingLegend) existingLegend.remove();
    return;
  }

  const legend = existingLegend || document.createElement("div");
  legend.className = "chart-series-legend";
  legend.replaceChildren();
  seriesList.forEach((series) => {
    const item = document.createElement("span");
    item.className = "chart-series-item";
    const swatch = document.createElement("span");
    swatch.className = "chart-series-swatch";
    swatch.style.background = series.color || "#56CCF2";
    swatch.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = series.label || "Serie";
    item.append(swatch, label);
    legend.append(item);
  });

  if (!existingLegend) canvas.insertAdjacentElement("afterend", legend);
}

function tickIndices(labelCount, maxTicks) {
  if (labelCount <= 0) return [];
  if (labelCount === 1 || maxTicks <= 1) return [0];
  const tickCount = Math.min(labelCount, maxTicks);
  return Array.from(
    new Set(
      Array.from({ length: tickCount }, (_, index) =>
        Math.round((index * (labelCount - 1)) / (tickCount - 1)),
      ),
    ),
  );
}

function drawLineChart(canvas, rawData) {
  const data = normalizeLineData(rawData);
  const { ctx, width, height } = resizeCanvas(canvas);
  if (!data.labels.length || !data.series.length) {
    renderLineLegend(canvas, []);
    drawEmptyState(ctx, width, height);
    return;
  }

  const seriesList = data.series
    .filter((series) => series && Array.isArray(series.values))
    .map((series) => ({
      ...series,
      values: data.labels.map((_, index) => finiteNumber(series.values[index])),
    }))
    .filter((series) => series.values.some((value) => value !== null));
  const values = seriesList.flatMap((item) => item.values.filter((value) => value !== null));
  if (!values.length) {
    renderLineLegend(canvas, []);
    drawEmptyState(ctx, width, height);
    return;
  }

  renderLineLegend(canvas, seriesList);
  const padding = { top: 24, right: 24, bottom: 48, left: 64 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  if (innerWidth <= 0 || innerHeight <= 0) {
    drawEmptyState(ctx, width, height);
    return;
  }
  const dataMax = values.reduce((max, value) => Math.max(max, value), 0);
  const minValue = values.reduce((min, value) => Math.min(min, value), 0);
  const maxValue = dataMax === minValue ? dataMax + 1 : dataMax;
  const yRange = maxValue - minValue;

  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  ctx.font = "12px Aptos, sans-serif";
  ctx.fillStyle = "#9aa8bb";

  for (let index = 0; index <= 4; index += 1) {
    const y = padding.top + (innerHeight / 4) * index;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();

    const value = maxValue - (yRange / 4) * index;
    ctx.textAlign = "right";
    ctx.fillText(value.toLocaleString("es-ES", { maximumFractionDigits: 0 }), padding.left - 10, y + 4);
  }

  const stepX = data.labels.length > 1 ? innerWidth / (data.labels.length - 1) : innerWidth;
  seriesList.forEach((series, seriesIndex) => {
    ctx.beginPath();
    ctx.lineWidth = seriesIndex === 0 ? 3 : 2;
    ctx.strokeStyle = series.color || "#56CCF2";
    let drawing = false;
    series.values.forEach((value, index) => {
      if (value === null) {
        drawing = false;
        return;
      }
      const x = padding.left + stepX * index;
      const y = padding.top + innerHeight - ((value - minValue) / yRange) * innerHeight;
      if (!drawing) {
        ctx.moveTo(x, y);
        drawing = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
  });

  ctx.font = "12px Aptos, sans-serif";
  const widestLabel = data.labels.reduce(
    (max, label) => Math.max(max, ctx.measureText(label).width),
    0,
  );
  const maxTicks = Math.max(2, Math.floor(innerWidth / Math.max(widestLabel + 24, 72)) + 1);
  const visibleTicks = tickIndices(data.labels.length, maxTicks);
  visibleTicks.forEach((index, tickPosition) => {
    const label = data.labels[index];
    const x = padding.left + stepX * index;
    ctx.textAlign = tickPosition === 0 ? "left" : tickPosition === visibleTicks.length - 1 ? "right" : "center";
    ctx.fillText(label, x, height - 16);
  });
}

function drawMultiLine(canvas, data) {
  const safeData = data || {};
  drawLineChart(canvas, {
    labels: safeData.labels || [],
    series: [
      { label: "Total", values: safeData.total || [], color: "#56CCF2" },
      { label: "Banco", values: safeData.bank || [], color: "#6FCF97" },
      { label: "Efectivo", values: safeData.cash || [], color: "#9B51E0" },
      { label: "Trade Republic", values: safeData.trade_republic || [], color: "#F2C94C" },
      { label: "Binance", values: safeData.binance || [], color: "#F2994A" },
    ],
  });
}

function bootCharts() {
  document.querySelectorAll("[data-chart-type]").forEach((canvas) => {
    const data = readChartData(canvas);
    const type = canvas.dataset.chartType;
    if (type === "donut") drawDonut(canvas, data);
    if (type === "line") drawLineChart(canvas, data);
    if (type === "single-line") drawLineChart(canvas, data);
    if (type === "multi-line") drawMultiLine(canvas, data);
  });
}

let chartFrame = null;

function scheduleCharts() {
  if (chartFrame !== null) window.cancelAnimationFrame(chartFrame);
  chartFrame = window.requestAnimationFrame(() => {
    chartFrame = null;
    bootCharts();
  });
}

function bootDropzones() {
  document.querySelectorAll("[data-dropzone]").forEach((zone) => {
    ["dragenter", "dragover"].forEach((eventName) => {
      zone.addEventListener(eventName, () => zone.classList.add("is-over"));
    });
    ["dragleave", "drop"].forEach((eventName) => {
      zone.addEventListener(eventName, () => zone.classList.remove("is-over"));
    });
  });
}

window.addEventListener("DOMContentLoaded", () => {
  bootCharts();
  bootDropzones();
  if ("ResizeObserver" in window) {
    const observer = new ResizeObserver(scheduleCharts);
    document.querySelectorAll("[data-chart-type]").forEach((canvas) => observer.observe(canvas));
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleCharts);
});

window.addEventListener("resize", scheduleCharts);
