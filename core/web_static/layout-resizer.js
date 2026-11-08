/* Shared mouse/touch/keyboard resizers for the C/S/T workbenches. */
(() => {
  "use strict";
  const STORAGE_PREFIX = "scada-layout-size-v2-";
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const configs = [
    {selector: ".workbench-shell", key: "workbench-sidebar", property: "--workbench-sidebar-width", min: 150, max: 420, otherMin: 320, axis: "x", className: "wb-shell-resizable"},
    {selector: ".shell", key: "c-sidebar", property: "--c-sidebar-width", min: 150, max: 420, otherMin: 320, axis: "x", className: "c-shell-resizable"},
    {selector: ".lb-workspace", key: "log-files", property: "--log-files-width", min: 190, max: 560, otherMin: 320, axis: "x", className: "log-workspace-resizable"},
    {selector: ".log-workspace", key: "legacy-log-files", property: "--legacy-log-files-width", min: 190, max: 560, otherMin: 320, axis: "x", className: "legacy-log-workspace-resizable"},
    {selector: ".split", key: "split-first", property: "--split-first-width", min: 240, max: 760, otherMin: 320, axis: "x", className: "split-resizable"},
    /* The S test pages use a two-column .grid.  Only exact two-child grids
       are eligible; form/status grids with many children must stay intact. */
    {selector: ".grid", key: "grid-first", property: "--grid-first-width", min: 260, max: 760, otherMin: 320, axis: "x", className: "grid-resizable"},
    {selector: ".test-body[data-resize-axis=\"y\"]", key: "test-summary-height", property: "--layout-first-height", min: 90, max: 300, axis: "y", className: "layout-resizable-y"},
    {selector: "[data-resize-axis=\"y\"]", key: "custom-height", property: "--layout-first-height", min: 140, max: 900, axis: "y", className: "layout-resizable-y"},
  ];
  const effectiveMax = (config, element) => {
    if (config.axis !== "x" || !config.otherMin) return config.max;
    const width = element.getBoundingClientRect().width;
    if (!Number.isFinite(width) || width <= 0) return config.max;
    // Keep enough room for the second panel so an old or oversized saved
    // value cannot collapse it into an unusable sliver.
    return Math.min(config.max, Math.max(config.min, width - 8 - config.otherMin));
  };
  const clampFor = (config, element, value) => clamp(value, config.min, effectiveMax(config, element));
  const storageKey = (config, index) => `${STORAGE_PREFIX}${location.pathname}:${config.key}:${index}`;
  const clampStored = (config, element, index) => {
    try {
      const value = Number.parseFloat(localStorage.getItem(storageKey(config, index)) ?? localStorage.getItem(STORAGE_PREFIX + config.key));
      if (Number.isFinite(value)) element.style.setProperty(config.property, `${clampFor(config, element, value)}px`);
    } catch (_) { /* private browsing or disabled storage */ }
  };
  const setSize = (config, element, value, index) => {
    const size = clampFor(config, element, value);
    element.style.setProperty(config.property, `${size}px`);
    try { localStorage.setItem(storageKey(config, index), String(Math.round(size))); } catch (_) { /* ignore */ }
  };
  function install(config, element, index) {
    if (element.dataset.layoutResizerInstalled === "1" || element.children.length !== 2) return;
    element.dataset.layoutResizerInstalled = "1";
    element.classList.add(config.className);
    clampStored(config, element, index);
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = `layout-resizer layout-resizer-${config.axis}`;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", config.axis === "x" ? "vertical" : "horizontal");
    handle.setAttribute("aria-label", config.axis === "x" ? "拖动调整左右区域宽度；拖到边缘隐藏左侧区域" : "拖动调整上下区域高度；拖到边缘隐藏上方区域");
    handle.title = config.axis === "x" ? "拖动调整左右区域宽度；拖到边缘隐藏，向外拖动恢复" : "拖动调整上下区域高度；拖到边缘隐藏，向外拖动恢复";
    element.insertBefore(handle, element.children[1]);
    let start = null;
    let moved = false;
    let lastSize = config.min;
    const applyHidden = (hidden) => {
      element.classList.toggle("layout-first-hidden", hidden);
      handle.setAttribute("aria-expanded", String(!hidden));
      handle.textContent = hidden ? (config.axis === "x" ? "›" : "⌄") : (config.axis === "x" ? "‹" : "⌃");
      try { localStorage.setItem(`${storageKey(config, index)}:hidden`, hidden ? "1" : "0"); } catch (_) { /* ignore */ }
    };
    let hidden = false;
    try {
      hidden = localStorage.getItem(`${storageKey(config, index)}:hidden`) === "1";
      const saved = Number.parseFloat(localStorage.getItem(`${storageKey(config, index)}:last`));
      if (Number.isFinite(saved)) lastSize = clampFor(config, element, saved);
    } catch (_) { /* ignore */ }
    applyHidden(hidden);
    const begin = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      const first = element.children[0];
      if (!first) return;
      start = {pointer: config.axis === "x" ? event.clientX : event.clientY,
        size: config.axis === "x" ? first.getBoundingClientRect().width : first.getBoundingClientRect().height};
      moved = false;
      handle.classList.add("is-dragging");
      document.body.classList.add("layout-resizing");
      handle.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    };
    const move = (event) => {
      if (!start) return;
      const current = config.axis === "x" ? event.clientX : event.clientY;
      const delta = current - start.pointer;
      if (Math.abs(delta) > 3) moved = true;
      const raw = start.size + delta;
      if (hidden) {
        if (delta > 8) {
          hidden = false;
          applyHidden(false);
          setSize(config, element, Math.max(lastSize, Math.min(config.min, raw)), index);
        }
        event.preventDefault();
        return;
      }
      if (raw <= 24) {
        lastSize = clampFor(config, element, start.size);
        hidden = true;
        try { localStorage.setItem(`${storageKey(config, index)}:last`, String(Math.round(lastSize))); } catch (_) { /* ignore */ }
        applyHidden(true);
        event.preventDefault();
        return;
      }
      setSize(config, element, raw, index);
      event.preventDefault();
    };
    const end = () => {
      if (!start) return;
      start = null;
      handle.classList.remove("is-dragging");
      document.body.classList.remove("layout-resizing");
    };
    handle.addEventListener("pointerdown", begin);
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    handle.addEventListener("keydown", (event) => {
      const first = element.children[0];
      if (!first) return;
      const current = config.axis === "x" ? first.getBoundingClientRect().width : first.getBoundingClientRect().height;
      const step = event.shiftKey ? 40 : 10;
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") { setSize(config, element, current - step, index); event.preventDefault(); }
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { setSize(config, element, current + step, index); event.preventDefault(); }
      if (event.key === "Home") { setSize(config, element, config.min, index); event.preventDefault(); }
      if (event.key === "End") { setSize(config, element, config.max, index); event.preventDefault(); }
    });
  }
  function scan() {
    for (const config of configs) document.querySelectorAll(config.selector).forEach((element, index) => install(config, element, index));
  }
  scan();
  new MutationObserver(scan).observe(document.body, {childList: true, subtree: true});
})();
