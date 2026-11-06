(function () {
  "use strict";
  async function refreshHealth() {
    const status = document.querySelector("#connection");
    try {
      const response = await fetch("/api/health");
      if (!response.ok) throw new Error("health");
      const result = await response.json();
      if (result.status !== "ok") throw new Error("health");
      status.textContent = "Web 已连接";
      status.classList.add("ok");
      status.classList.remove("error", "bad");
      document.querySelector("#workbench-database").textContent = result.database || "已连接";
    } catch (_) {
      status.textContent = "Web 连接失败";
      status.classList.remove("ok");
      status.classList.add("error");
    }
  }
  refreshHealth();
  setInterval(() => { if (!document.hidden) refreshHealth(); }, 15000);
})();
