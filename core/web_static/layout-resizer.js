/* Shared mouse/touch/keyboard resizers for the C/S/T workbenches. */
(() => {
  "use strict";
  const STORAGE_PREFIX = "scada-layout-size-";
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const configs = [
    {selector: ".workbench-shell", key: "workbench-sidebar", property: "--workbench-sidebar-width", min: 150, max: 420, axis: "x", className: "wb-shell-resizable"},
    {selector: ".shell", key: "c-sidebar", property: "--c-sidebar-width", min: 150, max: 420, axis: "x", className: "c-shell-resizable"},
    {selector: ".lb-workspace", key: "log-files", property: "--log-files-width", min: 190, max: 560, axis: "x", className: "log-workspace-resizable"},
    {selector: ".log-workspace", key: "legacy-log-files", property: "--legacy-log-files-width", min: 190, max: 560, axis: "x", className: "legacy-log-workspace-resizable"},
    {selector: ".split", key: "split-first", property: "--split-first-width", min: 240, max: 760, axis: "x", className: "split-resizable"},
    /* The S test pages use a two-column .grid.  Only exact two-child grids
       are eligible; form/status grids with many children must stay intact. */
    {selector: ".grid", key: "grid-first", property: "--grid-first-width", min: 260, max: 760, axis: "x", className: "grid-resizable"},
    {selector: "[data-resize-axis=\"y\"]", key: "custom-height", property: "--layout-first-height", min: 140, max: 900, axis: "y", className: "layout-resizable-y"},
  ];
  const clampStored = (config, element) => {
    try {
      const value = Number.parseFloat(localStorage.getItem(STORAGE_PREFIX + config.key));
      if (Number.isFinite(value)) element.style.setProperty(config.property, `${clamp(value, config.min, config.max)}px`);
    } catch (_) { /* private browsing or disabled storage */ }
  };
  const setSize = (config, element, value) => {
    const size = clamp(value, config.min, config.max);
    element.style.setProperty(config.property, `${size}px`);
    try { localStorage.setItem(STORAGE_PREFIX + config.key, String(Math.round(size))); } catch (_) { /* ignore */ }
  };
  function install(config, element) {
    if (element.dataset.layoutResizerInstalled === "1" || element.children.length !== 2) return;
    element.dataset.layoutResizerInstalled = "1";
    element.classList.add(config.className);
    clampStored(config, element);
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = `layout-resizer layout-resizer-${config.axis}`;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", config.axis === "x" ? "vertical" : "horizontal");
    handle.setAttribute("aria-label", config.axis === "x" ? "调整左右区域宽度" : "调整上下区域高度");
    handle.title = config.axis === "x" ? "拖动调整左右区域宽度" : "拖动调整上下区域高度";
    element.insertBefore(handle, element.children[1]);
    let start = null;
    const begin = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      const first = element.children[0];
      if (!first) return;
      start = {pointer: config.axis === "x" ? event.clientX : event.clientY,
        size: config.axis === "x" ? first.getBoundingClientRect().width : first.getBoundingClientRect().height};
      handle.classList.add("is-dragging");
      document.body.classList.add("layout-resizing");
      handle.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    };
    const move = (event) => {
      if (!start) return;
      const current = config.axis === "x" ? event.clientX : event.clientY;
      setSize(config, element, start.size + current - start.pointer);
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
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") { setSize(config, element, current - step); event.preventDefault(); }
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { setSize(config, element, current + step); event.preventDefault(); }
      if (event.key === "Home") { setSize(config, element, config.min); event.preventDefault(); }
      if (event.key === "End") { setSize(config, element, config.max); event.preventDefault(); }
    });
  }
  function scan() {
    for (const config of configs) document.querySelectorAll(config.selector).forEach((element) => install(config, element));
  }
  scan();
  new MutationObserver(scan).observe(document.body, {childList: true, subtree: true});
})();
