// Live US market clock in the header: New York time + market status + countdown.
// Regular session hours come from settings (data-open / data-close). Exchange holidays
// are not included. Uses the browser's Intl time-zone support, so EST/EDT is handled.
(function () {
  const el = document.getElementById("mkt-clock");
  if (!el) return;

  const tz = el.dataset.tz || "America/New_York";
  const toMin = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
  const OPEN = toMin(el.dataset.open || "09:30");
  const CLOSE = toMin(el.dataset.close || "16:00");
  const PRE = toMin("04:00");    // pre-market starts
  const AFTER = toMin("20:00");  // after-hours ends
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", weekday: "short",
    hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short",
  });
  const timeEl = el.querySelector(".clock-time");
  const statusEl = el.querySelector(".clock-status");

  function nowParts() {
    const p = {};
    for (const part of fmt.formatToParts(new Date())) p[part.type] = part.value;
    return p;
  }

  function duration(minutes) {
    minutes = Math.max(0, Math.round(minutes));
    const d = Math.floor(minutes / 1440), h = Math.floor((minutes % 1440) / 60), m = minutes % 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function tick() {
    const p = nowParts();
    const day = DAYS.indexOf(p.weekday);
    const mins = Number(p.hour) * 60 + Number(p.minute) + Number(p.second) / 60;
    const weekday = day >= 1 && day <= 5;

    let state, text;
    if (weekday && mins >= OPEN && mins < CLOSE) {
      state = "open";
      text = `Market open · closes in ${duration(CLOSE - mins)}`;
    } else {
      // Next weekday open.
      let ahead = 0, d = day;
      if (!(weekday && mins < OPEN)) {
        do { ahead += 1; d = (d + 1) % 7; } while (d === 0 || d === 6);
      }
      const until = ahead * 1440 + OPEN - mins;
      const when = ahead === 0 ? "today" : ahead === 1 ? "tomorrow" : DAYS[d];
      if (weekday && mins >= PRE && mins < OPEN) {
        state = "pre";
        text = `Pre-market · opens in ${duration(until)}`;
      } else if (weekday && mins >= CLOSE && mins < AFTER) {
        state = "after";
        text = `After hours · opens ${when} ${el.dataset.open} (in ${duration(until)})`;
      } else {
        state = "closed";
        text = `Market closed · opens ${when} ${el.dataset.open} (in ${duration(until)})`;
      }
    }

    timeEl.textContent = `${p.weekday} ${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}`;
    statusEl.textContent = text;
    el.dataset.state = state;
  }

  tick();
  setInterval(tick, 1000);
})();
