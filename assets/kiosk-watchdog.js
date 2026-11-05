/*
 * Kiosk self-healing.
 *
 * The wall display holds one page indefinitely - the screensaver's web view
 * has been observed alive for over a week. If that page's Dash renderer
 * wedges (most often a deploy changing a callback signature underneath it,
 * after which it posts a callback the server no longer has), the layout
 * keeps drawing but the graphs never populate again, and nobody is standing
 * there to press reload.
 *
 * So the page watches its own callback traffic and reloads when that traffic
 * is demonstrably broken. Deliberately plain browser JS on a timer rather
 * than a Dash callback: if the renderer is what is wedged, anything driven
 * by the renderer is wedged with it.
 *
 * The subtlety that matters here: the screensaver's web views are SUSPENDED
 * whenever the screensaver is not on screen, sometimes for hours. During a
 * suspension no callbacks run, so "we haven't had a successful callback in a
 * long time" does NOT mean the page is broken - and treating it as breakage
 * makes every single screensaver wake-up throw away a perfectly good
 * rendered page and show a cold start instead. That is worse than the bug
 * this file exists to fix. Hence: failures are counted explicitly, and
 * silence is only suspicious when our own timer proves we were awake for it.
 *
 * Only runs for ?kiosk=1 - an ordinary tab has a human with a reload button.
 */
(function () {
  "use strict";

  if (!/[?&]kiosk=1(&|$)/.test(window.location.search)) return;

  var CHECK_EVERY_MS = 30 * 1000;
  var STALE_AFTER_MS = 3 * 60 * 1000;   // awake, yet no successful callback
  var STARTUP_GRACE_MS = 2 * 60 * 1000; // don't judge a page that just loaded
  var FAILURES_BEFORE_RELOAD = 5;       // unambiguous: the server rejected us
  // A tick arriving this much later than scheduled means we were suspended,
  // not that anything failed.
  var SUSPEND_GAP_MS = CHECK_EVERY_MS * 3;
  // On wake, data this old means the view was frozen and is showing a
  // snapshot, not current readings.
  var WAKE_STALE_MS = 60 * 1000;
  // Survives a reload (sessionStorage), so a wake storm cannot become a
  // reload storm the way it did in the first version of this file.
  var RELOAD_COOLDOWN_MS = 45 * 1000;

  // ---- telemetry -------------------------------------------------------
  // The screensaver's page cannot be inspected from outside its process, so
  // it reports its own state. These are fire-and-forget image requests; the
  // server 404s them and nginx logs the query string, which is all we need.
  // Read with: grep kiosk-beacon /var/log/nginx/access.log
  var PAGE_ID = Math.random().toString(36).slice(2, 8);

  function beacon(event, extra) {
    try {
      var q = "ev=" + event + "&id=" + PAGE_ID +
              "&age=" + Math.round((Date.now() - loadedAt) / 1000);
      if (extra) q += "&" + extra;
      new Image().src = "/kiosk-beacon.gif?" + q + "&c=" + Date.now();
    } catch (e) { /* telemetry must never break the page */ }
  }

  window.addEventListener("error", function (e) {
    beacon("jserror", "msg=" + encodeURIComponent(String(e && e.message).slice(0, 90)));
  });

  // Did the chart actually draw? This is the thing we cannot otherwise see.
  // Kiosk renders server-side SVG (no Plotly), normal renders a Plotly
  // figure - count whichever this page actually uses, or the telemetry
  // reports "nochart" forever on the wall display.
  function chartCount() {
    var gd = document.querySelector(".js-plotly-plot");
    if (gd && gd.data) return gd.data.length;
    return document.querySelectorAll('img[src^="data:image/svg"]').length;
  }

  var charted = false;
  var chartWatch = setInterval(function () {
    var n = chartCount();
    if (!charted && n > 0) {
      charted = true;
      clearInterval(chartWatch);
      beacon("charted", "traces=" + n);
    }
  }, 2000);
  setTimeout(function () {
    if (!charted) {
      beacon("nochart", "plotdiv=" + (document.querySelector(".js-plotly-plot") ? 1 : 0) +
                        "&svg=" + document.querySelectorAll('img[src^="data:image/svg"]').length +
                        "&bodies=" + document.querySelectorAll("[id*=gauge-body]").length);
    }
  }, 60000);

  var loadedAt = Date.now();
  var lastOkAt = Date.now();
  var lastTickAt = Date.now();
  var failures = 0;
  var wakeups = 0;

  function noteResponse(url, ok) {
    if (String(url).indexOf("_dash-update-component") === -1) return;
    if (ok) {
      lastOkAt = Date.now();
      failures = 0;
    } else {
      failures += 1;
    }
  }

  // Dash's renderer posts callbacks with fetch(); wrap it to watch the
  // outcome. 204 (no update) counts as success - res.ok covers 200-299 -
  // because it still proves the round trip works.
  var originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function () {
      var target = arguments[0];
      var url = (target && target.url) || String(target || "");
      return originalFetch.apply(this, arguments).then(
        function (res) {
          try { noteResponse(url, res.ok); } catch (e) { /* never break the app */ }
          return res;
        },
        function (err) {
          try { noteResponse(url, false); } catch (e) { /* ignore */ }
          throw err;
        }
      );
    };
  }

  var originalOpen = window.XMLHttpRequest && window.XMLHttpRequest.prototype.open;
  if (originalOpen) {
    window.XMLHttpRequest.prototype.open = function (method, url) {
      var self = this;
      this.addEventListener("load", function () {
        try { noteResponse(url, self.status >= 200 && self.status < 300); } catch (e) {}
      });
      this.addEventListener("error", function () {
        try { noteResponse(url, false); } catch (e) {}
      });
      return originalOpen.apply(this, arguments);
    };
  }

  // Coming back from a suspension is a fresh start, not a fault.
  function resumeFromSuspension() {
    var now = Date.now();
    wakeups += 1;
    beacon("wake", "gapmin=" + Math.round((now - lastTickAt) / 60000));
    lastOkAt = now;
    loadedAt = now;   // re-arm the startup grace: callbacks need a moment
    failures = 0;
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") resumeFromSuspension();
  });

  // Reloading into a server that is still down swaps a stale-but-readable
  // page for a browser error page - which has no watchdog left, at a display
  // nobody is standing next to. Confirm the server answers first.
  function recentlyReloaded() {
    try {
      var t = parseInt(sessionStorage.getItem("wd-last-reload") || "0", 10);
      return Date.now() - t < RELOAD_COOLDOWN_MS;
    } catch (e) { return false; }
  }

  function markReloaded() {
    try { sessionStorage.setItem("wd-last-reload", String(Date.now())); } catch (e) {}
  }

  function probeThenReload() {
    // Arm the cooldown NOW, not in the callback: several wake checks can run
    // before an async probe resolves, and each would queue its own reload.
    // Losing a retry slot when the probe fails is cheap; a reload storm is not.
    markReloaded();
    var f = originalFetch || window.fetch;
    return f.call(window, "_dash-layout", { method: "GET", cache: "no-store" })
      .then(function (res) {
        if (res.ok) window.location.reload();
        return res.ok;
      })
      .catch(function () { return false; });
  }

  function check() {
    var now = Date.now();
    var sinceTick = now - lastTickAt;
    lastTickAt = now;

    // Our own timer did not run on schedule => the view was suspended.
    // This is the normal state for a screensaver: macOS freezes the web view
    // whenever the saver is not on screen, and on wake it RESUMES the same
    // frozen page rather than loading a new one. Telemetry showed a page that
    // charted fine, then emitted not one heartbeat in 50 minutes - so what
    // the display shows on wake is a snapshot from whenever it froze.
    // Reload once so it shows current readings. The cooldown is what keeps
    // this from turning into the reload storm the first version caused.
    if (sinceTick > SUSPEND_GAP_MS) {
      var frozenFor = now - lastOkAt;
      resumeFromSuspension();
      if (frozenFor > WAKE_STALE_MS && !recentlyReloaded()) {
        beacon("reload", "why=wake&frozenmin=" + Math.round(frozenFor / 60000));
        probeThenReload();
        return true;
      }
      return false;
    }
    if (now - loadedAt < STARTUP_GRACE_MS) return false;

    // Explicit rejections are unambiguous - this is the wedged-renderer case.
    if (failures >= FAILURES_BEFORE_RELOAD && !recentlyReloaded()) {
      beacon("reload", "why=failures&n=" + failures);
      probeThenReload();
      return true;
    }
    // Silence, while we were provably awake the whole time.
    if (now - lastOkAt > STALE_AFTER_MS && !recentlyReloaded()) {
      beacon("reload", "why=silent&sinceok=" + Math.round((now - lastOkAt) / 1000));
      probeThenReload();
      return true;
    }
    return false;
  }

  setInterval(check, CHECK_EVERY_MS);

  beacon("load", "vis=" + document.visibilityState);
  setInterval(function () {
    beacon("hb", "traces=" + chartCount() +
                 "&fail=" + failures +
                 "&sinceok=" + Math.round((Date.now() - lastOkAt) / 1000) +
                 "&wakes=" + wakeups +
                 "&vis=" + document.visibilityState);
  }, 60000);

  window.__kioskWatchdog = {
    staleAfterMs: STALE_AFTER_MS,
    failuresBeforeReload: FAILURES_BEFORE_RELOAD,
    msSinceLastOk: function () { return Date.now() - lastOkAt; },
    failures: function () { return failures; },
    wakeups: function () { return wakeups; },
    markStale: function () { lastOkAt = 0; loadedAt = 0; lastTickAt = Date.now(); },
    recentlyReloaded: recentlyReloaded,
    clearCooldown: function () { try { sessionStorage.removeItem("wd-last-reload"); } catch (e) {} },
    simulateSuspension: function () { lastTickAt = 0; },
    noteResponse: noteResponse,
    check: check,
    probeThenReload: probeThenReload,
  };
})();
