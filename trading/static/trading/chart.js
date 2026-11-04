// Intraday chart: closes with 9 EMA / 21 EMA (Chart.js 4, vanilla JS).
(function () {
  const canvas = document.getElementById("priceChart");
  if (!canvas || !window.CHART_URL) return;
  const box = canvas.closest(".chart") || canvas;
  const fail = (msg) => box.replaceWith(Object.assign(document.createElement("p"), { className: "muted", textContent: msg }));

  fetch(window.CHART_URL, { credentials: "same-origin" })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) return fail("Chart unavailable: " + data.error);
      if (!data.labels.length) return fail("No intraday bars yet for " + data.symbol + ".");
      new Chart(canvas, {
        type: "line",
        data: {
          labels: data.labels,
          datasets: [
            { label: data.symbol + " close", data: data.close, borderColor: "#9ca3af", borderWidth: 1.5, pointRadius: 0, tension: 0.1 },
            { label: "EMA 9", data: data.ema_fast, borderColor: "#4f46e5", borderWidth: 1.5, pointRadius: 0 },
            { label: "EMA 21", data: data.ema_slow, borderColor: "#f59e0b", borderWidth: 1.5, pointRadius: 0 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          spanGaps: true,
          scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { ticks: { callback: (v) => "$" + v } } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } } },
        },
      });
    })
    .catch((err) => fail("Failed to load chart: " + err));
})();
