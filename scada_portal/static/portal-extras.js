/* SCADA 综合门户交互增强引擎：偏好持久化、Toast、告警音频、命令面板(Ctrl+K)、控调审计、潮流小地图与 SSE 推送 */
(() => {
  "use strict";

  const PREFS_KEY = "scada_portal_prefs_v2";
  const tr = (text) =>
    window.scadaI18n && typeof window.scadaI18n.translateText === "function"
      ? window.scadaI18n.translateText(String(text))
      : String(text);

  const DEFAULT_PREFS = {
    refreshMs: 5000,
    pfArrows: true,
    pfMetrics: true,
    pfLabels: true,
    pfCanvasMode: "native", // native | dark-blend
    pfFaultOnly: false,
    controlUnlocked: true,
    alarmAudio: false,
    hideMaintAlarms: false,
    trendAxisMode: "dual", // dual | single | norm
    starredDevs: [],
  };

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) return { ...DEFAULT_PREFS };
      const parsed = JSON.parse(raw);
      return {
        ...DEFAULT_PREFS,
        ...(parsed && typeof parsed === "object" ? parsed : {}),
        starredDevs: Array.isArray(parsed?.starredDevs) ? parsed.starredDevs : [],
      };
    } catch (_) {
      return { ...DEFAULT_PREFS };
    }
  }

  function savePrefs(patch) {
    const next = { ...loadPrefs(), ...(patch || {}) };
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(next));
    } catch (_) {
      // 隐私模式忽略
    }
    return next;
  }

  // ==================== 非阻塞 Toast 通知 ====================
  function showToast(message, type = "info", durationMs = 3200) {
    let host = document.getElementById("scada-toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "scada-toast-host";
      host.className = "scada-toast-host";
      document.body.append(host);
    }
    const item = document.createElement("div");
    item.className = `scada-toast is-${type}`;
    item.textContent = tr(message);
    host.append(item);
    requestAnimationFrame(() => item.classList.add("show"));
    setTimeout(() => {
      item.classList.remove("show");
      setTimeout(() => item.remove(), 220);
    }, durationMs);
  }

  // ==================== 轻量告警音频合成（Web Audio API） ====================
  let audioCtx = null;
  function playAlarmBeep(severity = "high") {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioCtx) audioCtx = new Ctx();
      if (audioCtx.state === "suspended") audioCtx.resume();
      const now = audioCtx.currentTime;
      const freqs = severity === "high" ? [880, 660] : [587];
      freqs.forEach((f, idx) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(f, now + idx * 0.14);
        gain.gain.setValueAtTime(0.06, now + idx * 0.14);
        gain.gain.exponentialRampToValueAtTime(0.001, now + idx * 0.14 + 0.12);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now + idx * 0.14);
        osc.stop(now + idx * 0.14 + 0.13);
      });
    } catch (_) {
      // 浏览器静音策略拦截时静默忽略
    }
  }

  // ==================== SSE 实时事件流客户端 ====================
  class SseClient {
    constructor(url, onEvent) {
      this.url = url;
      this.onEvent = onEvent;
      this.es = null;
      this.connected = false;
      this.retryTimer = null;
      this.retryMs = 2000;
    }

    start() {
      if (typeof EventSource !== "function" || this.es) return;
      try {
        const es = new EventSource(this.url);
        this.es = es;
        es.onopen = () => {
          this.connected = true;
          this.retryMs = 2000;
        };
        ["hello", "tick", "alarm", "control", "maintenance"].forEach((ev) => {
          es.addEventListener(ev, (msg) => {
            let data = {};
            try { data = JSON.parse(msg.data || "{}"); } catch (_) {}
            this.onEvent?.(ev, data);
          });
        });
        es.onerror = () => {
          this.connected = false;
          try { es.close(); } catch (_) {}
          this.es = null;
          if (!this.retryTimer) {
            this.retryTimer = setTimeout(() => {
              this.retryTimer = null;
              this.retryMs = Math.min(15000, this.retryMs * 1.5);
              if (!document.hidden) this.start();
            }, this.retryMs);
          }
        };
      } catch (_) {
        this.connected = false;
      }
    }

    stop() {
      if (this.retryTimer) {
        clearTimeout(this.retryTimer);
        this.retryTimer = null;
      }
      if (this.es) {
        try { this.es.close(); } catch (_) {}
        this.es = null;
      }
      this.connected = false;
    }
  }

  // ==================== 全局命令面板（Ctrl+K） ====================
  function createCommandPalette(getItems) {
    let dialog = null;
    let input = null;
    let list = null;
    let activeIdx = 0;
    let currentFiltered = [];

    function ensureDom() {
      if (dialog) return;
      dialog = document.createElement("dialog");
      dialog.id = "scada-cmd-palette";
      dialog.className = "cmd-palette-dialog";
      dialog.innerHTML =
        '<div class="cmd-palette-box">' +
        '<div class="cmd-palette-head">' +
        '<input type="search" id="cmd-palette-input" autocomplete="off" placeholder="快速搜索设备、视图、主题或操作…" />' +
        '<kbd class="cmd-kbd">ESC</kbd>' +
        "</div>" +
        '<div class="cmd-palette-list" id="cmd-palette-list"></div>' +
        "</div>";
      document.body.append(dialog);
      input = dialog.querySelector("#cmd-palette-input");
      list = dialog.querySelector("#cmd-palette-list");

      dialog.addEventListener("click", (e) => {
        if (e.target === dialog) close();
      });
      input.addEventListener("input", () => {
        activeIdx = 0;
        render();
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          activeIdx = Math.min(Math.max(0, currentFiltered.length - 1), activeIdx + 1);
          render();
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          activeIdx = Math.max(0, activeIdx - 1);
          render();
        } else if (e.key === "Enter") {
          e.preventDefault();
          const chosen = currentFiltered[activeIdx];
          if (chosen) {
            close();
            chosen.action?.();
          }
        }
      });
    }

    function render() {
      if (!list) return;
      const kw = (input?.value || "").trim().toLowerCase();
      const items = getItems() || [];
      currentFiltered = items
        .filter((it) => {
          if (!kw) return true;
          const hay = `${it.title} ${tr(it.title)} ${it.group || ""} ${it.sub || ""}`.toLowerCase();
          return hay.includes(kw);
        })
        .slice(0, 40);
      if (!currentFiltered.length) {
        list.innerHTML = `<div class="cmd-empty">${tr("没有匹配的记录")}</div>`;
        return;
      }
      if (activeIdx >= currentFiltered.length) activeIdx = 0;
      list.innerHTML = currentFiltered
        .map(
          (it, idx) =>
            `<button type="button" class="cmd-item ${idx === activeIdx ? "active" : ""}" data-cmd-idx="${idx}">` +
            `<span class="cmd-item-group">${tr(it.group || "")}</span>` +
            `<span class="cmd-item-title">${tr(it.title)}</span>` +
            `<small class="cmd-item-sub">${it.sub || ""}</small>` +
            `</button>`
        )
        .join("");
      list.querySelectorAll("[data-cmd-idx]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const chosen = currentFiltered[Number(btn.dataset.cmdIdx)];
          if (chosen) {
            close();
            chosen.action?.();
          }
        });
      });
    }

    function open() {
      ensureDom();
      input.value = "";
      input.placeholder = tr("快速搜索设备、视图、主题或操作…");
      activeIdx = 0;
      render();
      if (!dialog.open && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
      setTimeout(() => input.focus(), 10);
    }

    function close() {
      if (dialog?.open) dialog.close();
    }

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (dialog?.open) close();
        else open();
      }
    });

    return { open, close };
  }

  // ==================== 潮流图缩略小地图（Minimap） ====================
  function createMinimap({ hostEl, getSvgMeta, getViewport, onPanTo }) {
    if (!hostEl) return { update: () => {} };
    let dragging = false;

    function moveFromEvent(e) {
      const meta = getSvgMeta();
      if (!meta || !meta.w || !meta.h) return;
      const rect = hostEl.getBoundingClientRect();
      const rx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ry = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
      onPanTo(rx * meta.w, ry * meta.h);
    }

    hostEl.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      dragging = true;
      moveFromEvent(e);
    });
    window.addEventListener("mousemove", (e) => {
      if (dragging) moveFromEvent(e);
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
    });

    function update() {
      const box = hostEl.querySelector(".pf-minimap-vp");
      const meta = getSvgMeta();
      const vp = getViewport();
      if (!box || !meta || !vp || !meta.w || !meta.h) return;
      const leftPct = Math.max(0, Math.min(100, (vp.x / meta.w) * 100));
      const topPct = Math.max(0, Math.min(100, (vp.y / meta.h) * 100));
      const widthPct = Math.max(6, Math.min(100 - leftPct, (vp.w / meta.w) * 100));
      const heightPct = Math.max(6, Math.min(100 - topPct, (vp.h / meta.h) * 100));
      box.style.left = `${leftPct.toFixed(2)}%`;
      box.style.top = `${topPct.toFixed(2)}%`;
      box.style.width = `${widthPct.toFixed(2)}%`;
      box.style.height = `${heightPct.toFixed(2)}%`;
    }

    return { update };
  }

  window.ScadaExtras = {
    loadPrefs,
    savePrefs,
    showToast,
    playAlarmBeep,
    SseClient,
    createCommandPalette,
    createMinimap,
  };
})();
