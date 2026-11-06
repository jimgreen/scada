(function () {
  "use strict";
  fetch("/api/health").then(response => {
    if (!response.ok) throw new Error("服务不可用");
    return response.json();
  }).then(result => {
    document.querySelector("#health").textContent = result.status === "ok" ? "测试服务已就绪 · " + result.database : "服务异常";
  }).catch(() => { document.querySelector("#health").textContent = "服务不可用"; });
})();
