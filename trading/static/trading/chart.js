// Price + moving-average chart with crossover markers (Chart.js 4, vanilla JS).
(function () {
  const canvas = document.getElementById("priceChart");
  if (!canvas || !window.CHART_URL) return;

  fetch(window.CHART_URL, { credentials: "same-origin" })
    .then((r) => r.json())
    .then((data) => {
      const labels = data.labels.map((d) => d.slice(0, 10));
      const index = new Map(data.labels.map((d, i) => [d, i]));
      const buys = new Array(labels.length).fill(null);
      const sells = new Array(labels.length).fill(null);
      data.signals.forEach((s) => {
        const i = index.get(s.x);
        if (i === undefined) return;
        (s.signal === "BUY" ? buys : sells)[i] = s.y;
      });

      new Chart(canvas, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: data.symbol + " close", data: data.close, borderColor: "#9ca3af", borderWidth: 1.5, pointRadius: 0, tension: 0.1 },
            { label: data.ma_type + " " + data.short_window, data: data.short_ma, borderColor: "#4f46e5", borderWidth: 1.5, pointRadius: 0 },
            { label: data.ma_type + " " + data.long_window, data: data.long_ma, borderColor: "#f59e0b", borderWidth: 1.5, pointRadius: 0 },
            { label: "BUY signal", data: buys, showLine: false, pointStyle: "triangle", pointRadius: 7, backgroundColor: "#16a34a", borderColor: "#16a34a" },
            { label: "SELL signal", data: sells, showLine: false, pointStyle: "triangle", rotation: 180, pointRadius: 7, backgroundColor: "#dc2626", borderColor: "#dc2626" },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          spanGaps: true,
          scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { ticks: { callback: (v) => (data.currency || "$") + v } } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } } },
        },
      });
    })
    .catch((err) => {
      canvas.replaceWith(Object.assign(document.createElement("p"), { className: "error", textContent: "Failed to load chart: " + err }));
    });
})();
