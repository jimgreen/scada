/* Client hints are diagnostic claims, never authentication or server trace IDs. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const id = () => crypto.randomUUID?.() || [...crypto.getRandomValues(new Uint8Array(16))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  const tab = id();
  let gesture = {label: '页面脚本', at: 0};
  const mark = (event) => {
    const target = event.submitter || event.target?.closest?.('button, [role="button"], input[type="submit"]');
    if (!target) return;
    // IDs and button labels only: never collect input values or form content.
    const label = target.id || target.getAttribute('aria-label') || target.textContent?.trim() || '表单提交';
    gesture = {label: label.replace(/\s+/g, ' ').slice(0, 100), at: performance.now()};
  };
  document.addEventListener('click', mark, true);
  document.addEventListener('submit', mark, true);
  window.fetch = (input, init) => {
    const request = input instanceof Request ? input : null;
    const url = new URL(request ? request.url : input, location.href);
    const method = String(init?.method || request?.method || 'GET').toUpperCase();
    if (url.origin !== location.origin || ['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      return nativeFetch(input, init);
    }
    const headers = new Headers(init?.headers || request?.headers);
    headers.set('X-Scada-Tab', tab);
    headers.set('X-Scada-Request', id());
    const action = performance.now() - gesture.at < 10000 ? gesture.label : '页面脚本';
    // HTTP header values are bytes; encode CJK labels explicitly.
    headers.set('X-Scada-Action', encodeURIComponent(action));
    return nativeFetch(input, {...init, headers});
  };
})();
