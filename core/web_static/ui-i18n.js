/* SCADA 全站多语言（zh-CN / en-US / ru-RU）与多风格主题引擎。
 * 支持全部 8 套 Design Tokens 预设：
 * minimalist-light / precision-dark / sci-fi-cyan / sci-fi-purple / sci-fi-matrix / bento-grid / glassmorphism / editorial。
 * 无外部依赖、完全兼容严格 CSP（无内联脚本），支持动态 DOM 增量翻译与 localStorage 持久化。
 */
(() => {
  "use strict";

  const THEME_KEY = "scada_ui_theme";
  const LANG_KEY = "scada_ui_lang";
  const THEMES = [
    "minimalist-light",
    "precision-dark",
    "sci-fi-cyan",
    "sci-fi-purple",
    "sci-fi-matrix",
    "bento-grid",
    "glassmorphism",
    "editorial",
  ];
  const LEGACY_THEME_MAP = {
    "sci-fi": "sci-fi-cyan",
    "neo-brutalism": "minimalist-light",
  };
  const DARK_THEMES = new Set([
    "precision-dark",
    "sci-fi-cyan",
    "sci-fi-purple",
    "sci-fi-matrix",
    "glassmorphism",
  ]);
  const LANGS = ["zh-CN", "en-US", "ru-RU"];

  // 集中式多语言字典：以简体中文为基准键，便于对现有零散静态/动态页面无缝覆盖。
  const I18N_DICT = {
    "zh-CN": {
      "ui.theme.label": "界面风格",
      "ui.theme.minimalist-light": "极简明亮",
      "ui.theme.precision-dark": "精密暗黑",
      "ui.theme.sci-fi-cyan": "科幻 · 电光青",
      "ui.theme.sci-fi-purple": "科幻 · 赛博紫",
      "ui.theme.sci-fi-matrix": "科幻 · 矩阵绿",
      "ui.theme.bento-grid": "便当盒网格",
      "ui.theme.glassmorphism": "毛玻璃拟态",
      "ui.theme.editorial": "编辑杂志",
      "ui.lang.label": "界面语言",
    },
    "en-US": {
      "ui.theme.label": "UI Theme",
      "ui.theme.minimalist-light": "Minimalist Light",
      "ui.theme.precision-dark": "Precision Dark",
      "ui.theme.sci-fi-cyan": "Sci-Fi · Cyan",
      "ui.theme.sci-fi-purple": "Sci-Fi · Cyber Purple",
      "ui.theme.sci-fi-matrix": "Sci-Fi · Matrix Green",
      "ui.theme.bento-grid": "Bento Grid",
      "ui.theme.glassmorphism": "Glassmorphism",
      "ui.theme.editorial": "Editorial",
      "ui.lang.label": "Language",

      // 顶栏与模块名
      "运行工作台": "Operations Workbench",
      "协议服务": "Protocol Service",
      "四遥转发": "Telemetry Forwarding",
      "设备量测": "Device Measurements",
      "scdfe_c · 运行工作台": "scdfe_c · Operations",
      "scdfe_s · 协议服务": "scdfe_s · Protocols",
      "scdfe_t · 四遥转发": "scdfe_t · Forwarding",
      "scdfe_d · 设备量测": "scdfe_d · Devices",
      "接口文档 ↗": "API Docs ↗",
      "连接检查中…": "Checking…",
      "连接检查中": "Checking…",
      "Web 已连接": "Web Connected",
      "Web 连接失败": "Disconnected",
      "连接失败": "Disconnected",
      "5 秒刷新": "5s Auto",
      "等待更新": "Pending",
      "跳到页面内容": "Skip to content",
      "选择数据库": "Database",
      "运行态": "Runtime",
      "场景": "Scene",

      // 侧边栏导航
      "运行管理": "Operations",
      "数据与定义": "Data & Schema",
      "通信测试": "Comm Test",
      "进程监控": "Processes",
      "通信监控": "Communications",
      "运行日志": "Runtime Logs",
      "日志报文": "Logs & Packets",
      "报文日志": "Packet Logs",
      "SOE 事件": "SOE Events",
      "运行诊断": "Diagnostics",
      "指令追踪": "Command Traces",
      "采样管理": "Trend Manager",
      "客户端连接": "Connections",
      "通道数据": "Channels",
      "四遥数据": "Four-Remote",
      "通道定义": "Channels",
      "RTU 定义": "RTUs",
      "四遥定义": "Points",
      "协议参数": "Protocol Params",
      "辅助表": "Aux Tables",
      "通道配置": "Channel Config",
      "连接信息": "Connections",
      "设备采集": "Collector",
      "采集与修改记录": "Audit & History",
      "量测定义": "Mappings",
      "量测查询": "Live Values",
      "参数配置": "Parameters",

      // 通用按钮与筛选
      "刷新": "Refresh",
      "刷新日志": "Refresh Logs",
      "刷新最新": "Latest",
      "刷新并检查配置": "Refresh & Check",
      "查询": "Query",
      "筛选": "Filter",
      "清空": "Clear",
      "清空条件": "Reset",
      "清除筛选": "Reset Filters",
      "新增": "Add",
      "新建": "New",
      "编辑": "Edit",
      "删除": "Delete",
      "保存": "Save",
      "取消": "Cancel",
      "关闭": "Close",
      "确认": "Confirm",
      "应用": "Apply",
      "启动": "Start",
      "停止": "Stop",
      "导出本批": "Export Batch",
      "导出当前显示": "Export View",
      "导出 CSV": "Export CSV",
      "上一页": "Prev",
      "下一页": "Next",
      "更早记录": "Older",
      "较早一批": "Older Batch",
      "较新一批": "Newer Batch",
      "查看最新": "Latest",
      "从最早读取": "From Oldest",
      "继续读取": "Load More",
      "回到最新": "Back to Latest",
      "显示筛选": "Show Filters",
      "隐藏筛选": "Hide Filters",
      "跟随最新": "Follow Live",
      "自动刷新": "Auto Refresh",
      "自动换行": "Wrap Lines",
      "全部": "All",
      "全部 RTU": "All RTUs",
      "全部通道": "All Channels",
      "全部文件": "All Files",
      "全部方向": "All Directions",
      "全部类型": "All Types",
      "当前文件": "Current File",
      "全部匹配文件": "All Matching",
      "新增测点": "Add Point",
      "批量加入": "Batch Add",
      "新增设备": "Add Device",
      "导入设备模型": "Import Model",
      "新增属性映射": "Add Mapping",
      "采集一次": "Collect Once",
      "启动采集": "Start Collector",
      "停止采集": "Stop Collector",
      "保存周期": "Save Interval",
      "保存参数": "Save Params",
      "撤销修改": "Discard",
      "批量设置": "Bulk Edit",
      "配置回采关联": "Feedback Rules",

      // 四遥与趋势
      "遥测 YC": "Analog (YC)",
      "遥信 YX": "Binary (YX)",
      "遥控 YK": "Control (YK)",
      "遥调 YT": "Setpoint (YT)",
      "实时曲线": "Live Trends",
      "纵轴设置": "Y-Axis Settings",
      "纵轴设置…": "Y-Axis Settings…",
      "曲线设置": "Series Settings",
      "曲线设置…": "Series Settings…",
      "重置视图": "Reset View",
      "全部设备": "All Devices",
      "全部设备表": "All Tables",
      "放大": "Zoom In",
      "缩小": "Zoom Out",
      "重置": "Reset",
      "跟随当前时刻": "Rolling",
      "固定起止": "Fixed Range",
      "自动范围": "Auto",
      "自定义范围": "Manual",
      "左轴": "Left Axis",
      "右轴": "Right Axis",

      // 状态词汇
      "运行中": "Running",
      "已停止": "Stopped",
      "启动中": "Starting",
      "停止中": "Stopping",
      "错误": "Error",
      "降级": "Degraded",
      "未知": "Unknown",
      "可用": "Valid",
      "未绑定": "Unbound",
      "启用": "Enabled",
      "停用": "Disabled",
      "开启": "On",
      "是": "Yes",
      "否": "No",
    },
    "ru-RU": {
      "ui.theme.label": "Стиль интерфейса",
      "ui.theme.minimalist-light": "Светлый минимализм",
      "ui.theme.precision-dark": "Тёмный приборный",
      "ui.theme.sci-fi-cyan": "Sci-Fi · Циан",
      "ui.theme.sci-fi-purple": "Sci-Fi · Кибер-пурпур",
      "ui.theme.sci-fi-matrix": "Sci-Fi · Матрица",
      "ui.theme.bento-grid": "Сетка Бенто",
      "ui.theme.glassmorphism": "Стекломорфизм",
      "ui.theme.editorial": "Журнальный",
      "ui.lang.label": "Язык интерфейса",

      // 顶栏与模块名
      "运行工作台": "Рабочая панель",
      "协议服务": "Служба протоколов",
      "四遥转发": "Ретрансляция",
      "设备量测": "Измерения устройств",
      "scdfe_c · 运行工作台": "scdfe_c · Панель",
      "scdfe_s · 协议服务": "scdfe_s · Протоколы",
      "scdfe_t · 四遥转发": "scdfe_t · Ретрансляция",
      "scdfe_d · 设备量测": "scdfe_d · Устройства",
      "接口文档 ↗": "API ↗",
      "连接检查中…": "Проверка…",
      "连接检查中": "Проверка…",
      "Web 已连接": "Подключено",
      "Web 连接失败": "Нет связи",
      "连接失败": "Нет связи",
      "5 秒刷新": "Авто 5с",
      "等待更新": "Ожидание",
      "跳到页面内容": "К содержимому",
      "选择数据库": "База данных",
      "运行态": "Среда",
      "场景": "Сценарий",

      // 侧边栏导航
      "运行管理": "Управление",
      "数据与定义": "Данные и схемы",
      "通信测试": "Тест связи",
      "进程监控": "Процессы",
      "通信监控": "Каналы связи",
      "运行日志": "Журнал работы",
      "日志报文": "Журналы и кадры",
      "报文日志": "Кадры обмена",
      "SOE 事件": "События SOE",
      "运行诊断": "Диагностика",
      "指令追踪": "Трасса команд",
      "采样管理": "Тренды",
      "客户端连接": "Подключения",
      "通道数据": "Каналы",
      "四遥数据": "Точки ТМ/ТУ",
      "通道定义": "Каналы",
      "RTU 定义": "Устройства RTU",
      "四遥定义": "Точки ТМ/ТУ",
      "协议参数": "Параметры",
      "辅助表": "Доп. таблицы",
      "通道配置": "Параметры канала",
      "连接信息": "Соединения",
      "设备采集": "Сбор данных",
      "采集与修改记录": "История и аудит",
      "量测定义": "Привязки",
      "量测查询": "Текущие значения",
      "参数配置": "Настройки",

      // 通用按钮与筛选
      "刷新": "Обновить",
      "刷新日志": "Обновить",
      "刷新最新": "Новые",
      "刷新并检查配置": "Проверить",
      "查询": "Поиск",
      "筛选": "Фильтр",
      "清空": "Сброс",
      "清空条件": "Сбросить",
      "清除筛选": "Сбросить",
      "新增": "Добавить",
      "新建": "Создать",
      "编辑": "Изменить",
      "删除": "Удалить",
      "保存": "Сохранить",
      "取消": "Отмена",
      "关闭": "Закрыть",
      "确认": "Подтвердить",
      "应用": "Применить",
      "启动": "Пуск",
      "停止": "Стоп",
      "导出本批": "Экспорт",
      "导出当前显示": "Экспорт",
      "导出 CSV": "Экспорт CSV",
      "上一页": "Назад",
      "下一页": "Далее",
      "更早记录": "Ранее",
      "较早一批": "Ранее",
      "较新一批": "Новее",
      "查看最新": "Последние",
      "从最早读取": "С начала",
      "继续读取": "Ещё",
      "回到最新": "К новым",
      "显示筛选": "Фильтры",
      "隐藏筛选": "Скрыть фильтры",
      "跟随最新": "Следить",
      "自动刷新": "Автообновление",
      "自动换行": "Перенос строк",
      "全部": "Все",
      "全部 RTU": "Все RTU",
      "全部通道": "Все каналы",
      "全部文件": "Все файлы",
      "全部方向": "Все направления",
      "全部类型": "Все типы",
      "当前文件": "Текущий файл",
      "全部匹配文件": "Все файлы",
      "新增测点": "Добавить точку",
      "批量加入": "Пакетно",
      "新增设备": "Добавить устр.",
      "导入设备模型": "Импорт модели",
      "新增属性映射": "Добавить связь",
      "采集一次": "Опрос",
      "启动采集": "Пуск сбора",
      "停止采集": "Стоп сбора",
      "保存周期": "Сохранить",
      "保存参数": "Сохранить",
      "撤销修改": "Отменить",
      "批量设置": "Пакетно",
      "配置回采关联": "Правила ОС",

      // 四遥与趋势
      "遥测 YC": "ТИ (YC)",
      "遥信 YX": "ТС (YX)",
      "遥控 YK": "ТУ (YK)",
      "遥调 YT": "ТР (YT)",
      "实时曲线": "Графики реального времени",
      "纵轴设置": "Оси Y",
      "纵轴设置…": "Оси Y…",
      "曲线设置": "Настройка кривых",
      "曲线设置…": "Настройка кривых…",
      "重置视图": "Сбросить вид",
      "全部设备": "Все устройства",
      "全部设备表": "Все таблицы",
      "放大": "Увеличить",
      "缩小": "Уменьшить",
      "重置": "Сброс",
      "跟随当前时刻": "Скользящее окно",
      "固定起止": "Интервал",
      "自动范围": "Авто",
      "自定义范围": "Вручную",
      "左轴": "Левая ось",
      "右轴": "Правая ось",

      // 状态词汇
      "运行中": "Работает",
      "已停止": "Остановлен",
      "启动中": "Запуск",
      "停止中": "Остановка",
      "错误": "Ошибка",
      "降级": "Сбой",
      "未知": "Неизвестно",
      "可用": "Норма",
      "未绑定": "Не привязан",
      "启用": "Вкл",
      "停用": "Откл",
      "开启": "Вкл",
      "是": "Да",
      "否": "Нет",
    },
  };

  let currentTheme = "minimalist-light";
  let currentLang = "zh-CN";
  const originalTexts = new WeakMap();
  const originalAttrs = new WeakMap();

  function normalizeTheme(theme) {
    if (THEMES.includes(theme)) return theme;
    if (theme && Object.prototype.hasOwnProperty.call(LEGACY_THEME_MAP, theme)) {
      return LEGACY_THEME_MAP[theme];
    }
    return "minimalist-light";
  }

  try {
    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme) currentTheme = normalizeTheme(savedTheme);
    const savedLang = localStorage.getItem(LANG_KEY);
    if (LANGS.includes(savedLang)) currentLang = savedLang;
  } catch (_) {
    // 隐私模式忽略
  }

  function t(key, fallback) {
    const table = I18N_DICT[currentLang] || I18N_DICT["zh-CN"];
    if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
    if (Object.prototype.hasOwnProperty.call(I18N_DICT["zh-CN"], key)) return I18N_DICT["zh-CN"][key];
    return fallback !== undefined ? fallback : key;
  }

  function translateTextNode(node) {
    if (!node || node.nodeType !== Node.TEXT_NODE) return;
    const parent = node.parentElement;
    if (!parent || parent.closest("script, style, code, pre, .lb-content, .log-content, #status-json, #schema-json")) {
      return;
    }
    const raw = node.nodeValue || "";
    const trimmed = raw.trim();
    if (!trimmed) return;
    let source = originalTexts.get(node);
    if (source === undefined) {
      // 仅登记字典中存在的中文源文案，避免污染动态业务数据（如站点名/测点名/数值）
      if (Object.prototype.hasOwnProperty.call(I18N_DICT["en-US"], trimmed)) {
        source = { full: raw, trimmed, lead: raw.slice(0, raw.indexOf(trimmed)), trail: raw.slice(raw.indexOf(trimmed) + trimmed.length) };
        originalTexts.set(node, source);
      } else {
        return;
      }
    }
    const translated = currentLang === "zh-CN" ? source.trimmed : t(source.trimmed, source.trimmed);
    const nextValue = source.lead + translated + source.trail;
    if (node.nodeValue !== nextValue) node.nodeValue = nextValue;
    if (currentLang !== "zh-CN" && parent.matches(".nav b, button, th") && !parent.title) {
      parent.title = translated;
    }
  }

  function translateRoot(root = document.body) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      translateTextNode(root);
      return;
    }
    // 显式 data-i18n 属性优先翻译
    if (root.querySelectorAll) {
      root.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.getAttribute("data-i18n");
        if (key) el.textContent = t(key, el.textContent);
      });
      root.querySelectorAll("[data-i18n-title]").forEach(el => {
        const key = el.getAttribute("data-i18n-title");
        if (key) el.title = t(key, el.title);
      });
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) translateTextNode(node);
  }

  function isDarkTheme(theme = currentTheme) {
    return DARK_THEMES.has(theme);
  }

  function updateControls() {
    const langSelect = document.getElementById("ui-lang-select");
    if (langSelect) {
      langSelect.value = currentLang;
      langSelect.setAttribute("aria-label", t("ui.lang.label"));
    }
    const themeSelect = document.getElementById("ui-theme-select");
    if (themeSelect) {
      themeSelect.setAttribute("aria-label", t("ui.theme.label"));
      themeSelect.title = t("ui.theme.label");
      Array.from(themeSelect.options).forEach(opt => {
        opt.textContent = t(`ui.theme.${opt.value}`, opt.value);
      });
      themeSelect.value = currentTheme;
    }
  }

  function applyTheme(theme, persist = true) {
    theme = normalizeTheme(theme);
    currentTheme = theme;
    const scheme = isDarkTheme(theme) ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.colorScheme = scheme;
    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* ignore */ }
    }
    updateControls();
    document.dispatchEvent(new CustomEvent("scada:theme-change", { detail: { theme, colorScheme: scheme } }));
  }

  function applyLang(lang, persist = true) {
    if (!LANGS.includes(lang)) lang = "zh-CN";
    currentLang = lang;
    document.documentElement.lang = lang;
    if (persist) {
      try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* ignore */ }
    }
    translateRoot(document.body);
    updateControls();
    document.dispatchEvent(new CustomEvent("scada:lang-change", { detail: { lang } }));
  }

  function mountGlobalControls() {
    const tools = document.querySelector(".header-tools");
    if (!tools || document.getElementById("ui-global-controls")) return;
    const box = document.createElement("div");
    box.id = "ui-global-controls";
    box.className = "ui-global-controls";

    const select = document.createElement("select");
    select.id = "ui-lang-select";
    select.className = "ui-lang-select";
    select.innerHTML =
      '<option value="zh-CN">中文</option>' +
      '<option value="en-US">English</option>' +
      '<option value="ru-RU">Русский</option>';
    select.value = currentLang;
    select.addEventListener("change", () => applyLang(select.value));

    const themeSelect = document.createElement("select");
    themeSelect.id = "ui-theme-select";
    themeSelect.className = "ui-theme-select";
    for (const themeId of THEMES) {
      const opt = document.createElement("option");
      opt.value = themeId;
      opt.textContent = t(`ui.theme.${themeId}`, themeId);
      themeSelect.append(opt);
    }
    themeSelect.value = currentTheme;
    themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));

    box.append(select, themeSelect);
    tools.append(box);
    updateControls();
  }

  // 全局快捷操作：按 / 快速聚焦可见搜索框
  document.addEventListener("keydown", event => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = event.target?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || event.target?.isContentEditable) return;
    if (document.querySelector("dialog[open]")) return;
    const candidates = document.querySelectorAll('input[type="search"], #point-search, #search, #lb-keyword, #log-search, #keyword');
    for (const input of candidates) {
      if (input.offsetParent !== null && !input.disabled && !input.closest("[hidden]")) {
        event.preventDefault();
        input.focus();
        input.select?.();
        return;
      }
    }
  });

  // 立即在 html 根元素激活主题与明暗属性，避免首屏闪烁
  document.documentElement.dataset.theme = currentTheme;
  document.documentElement.dataset.colorScheme = isDarkTheme(currentTheme) ? "dark" : "light";
  document.documentElement.lang = currentLang;

  let pendingTranslate = false;
  function scheduleTranslate() {
    if (currentLang === "zh-CN" || pendingTranslate) return;
    pendingTranslate = true;
    const run = () => {
      pendingTranslate = false;
      if (currentLang !== "zh-CN") translateRoot(document.body);
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  function boot() {
    mountGlobalControls();
    applyTheme(currentTheme, false);
    if (currentLang !== "zh-CN") translateRoot(document.body);
    if (document.body && typeof MutationObserver === "function") {
      new MutationObserver(scheduleTranslate).observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  window.scadaI18n = {
    t,
    themes: THEMES.slice(),
    isDarkTheme,
    getLang: () => currentLang,
    setLang: lang => applyLang(lang, true),
    getTheme: () => currentTheme,
    setTheme: theme => applyTheme(theme, true),
    dict: I18N_DICT,
  };
})();
