/* SCADA 全站多语言（zh-CN / en-US / ru-RU）与多风格主题引擎。
 * 支持全部 8 套 Design Tokens 预设：
 * minimalist-light / precision-dark / sci-fi-cyan / sci-fi-purple / sci-fi-matrix / bento-grid / glassmorphism / editorial。
 * 三层多语言能力：全站三语精确词典 + 参数化动态模板规则 + TextNode/placeholder/title/aria-label/document.title 增量同步。
 * 无外部依赖、完全兼容严格 CSP（无内联脚本），支持 localStorage 持久化。
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
  const TRANSLATED_ATTRS = ["placeholder", "title", "aria-label"];

  // 集中式三语基础控制键
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
    },
  };

  // 全站精确三语词条表：[简体中文, 英文, 俄文]，启动时自动注册到 I18N_DICT
  const PHRASES = [
    // ==================== 确认对话框与高危操作 ====================
    ["请确认操作", "Confirm Action", "Подтверждение"],
    ["确认删除", "Confirm Delete", "Подтвердить удаление"],
    ["确认停止", "Confirm Stop", "Подтвердить остановку"],
    ["确认执行", "Confirm", "Подтвердить"],
    ["取消", "Cancel", "Отмена"],
    ["确定", "OK", "ОК"],
    ["确认", "Confirm", "Подтвердить"],
    ["该操作不可恢复，确认继续？", "This action cannot be undone. Continue?", "Действие необратимо. Продолжить?"],
    ["确认停止全部正在运行的服务与进程？", "Stop all running services and processes?", "Остановить все запущенные службы и процессы?"],
    ["确定删除当前通道的规约参数？", "Delete protocol parameters for this channel?", "Удалить параметры протокола этого канала?"],
    ["确定删除该通道及其四遥点？", "Delete this channel and all its points?", "Удалить этот канал и все его точки?"],
    ["确定删除该测点？", "Delete this point?", "Удалить эту точку?"],
    ["确认停止当前服务？", "Stop the current service?", "Остановить текущую службу?"],
    ["确认移除该实时曲线采样点？", "Remove this trend sampling point?", "Удалить эту точку из тренда?"],
    ["设为采样点", "Add Sampling Point", "Добавить точку выборки"],
    ["设为采样点（左轴）", "Add as Sampling Point (Left Axis)", "Добавить точку (левая ось)"],
    ["设为采样点（右轴）", "Add as Sampling Point (Right Axis)", "Добавить точку (правая ось)"],
    ["左轴显示", "Show on Left Axis", "Показать (левая ось)"],
    ["右轴显示", "Show on Right Axis", "Показать (правая ось)"],
    ["隐藏", "Hidden", "Скрыть"],
    ["曲线状态", "Plot State", "Отображение"],
    ["删除采样点", "Remove Sampling Point", "Удалить точку выборки"],
    ["在曲线中定位", "Locate in Trend Chart", "Показать на графике"],
    ["确认删除该采样点并移出实时曲线？", "Remove this sampling point from the live trend?", "Удалить эту точку выборки с графика?"],
    ["折叠上方表格（实时曲线全屏）", "Collapse Table (Expand Trend)", "Свернуть таблицу (развернуть график)"],
    ["隐藏下方实时曲线（表格全屏）", "Hide Trend (Expand Table)", "Скрыть график (развернуть таблицу)"],
    ["恢复显示上方表格", "Restore Table", "Показать таблицу"],
    ["恢复显示下方实时曲线", "Restore Live Trend", "Показать график"],
    ["展开下方实时曲线", "Expand Live Trend", "Развернуть график"],
    ["拖动调整高度 · 双击复位", "Drag to resize · Double-click to reset", "Тяните для изменения · Двойной клик — сброс"],
    ["拖动调整左右宽度 · 双击复位", "Drag to resize width · Double-click to reset", "Тяните ширину · Двойной клик — сброс"],
    ["折叠右侧状态栏", "Collapse Status Panel", "Свернуть панель состояния"],
    ["展开右侧状态栏", "Expand Status Panel", "Развернуть панель состояния"],
    ["折叠上方耗时表（明细全屏）", "Collapse Latency Table (Expand Details)", "Свернуть таблицу задержек"],
    ["折叠下方明细区（耗时表全屏）", "Collapse Details (Expand Latency Table)", "Свернуть детали"],
    ["转发指令入库", "Command Writes", "Запись команд"],
    ["显示状态写库", "Runtime Flush", "Сброс состояний"],
    ["Modbus 调度", "Modbus Queue", "Очередь Modbus"],
    ["已设为采样点（右键可管理或删除）", "Active sampling point (right-click to manage)", "Точка выборки активна (ПКМ для управления)"],
    ["接口文档", "API Docs", "Документация API"],
    ["通道、RTU 与四遥数据 API", "Channel, RTU & Four-Remote Data API", "API каналов, RTU и точек ТМ/ТУ"],

    // ==================== 顶栏、标题与模块名 ====================
    ["运行工作台", "Operations Workbench", "Рабочая панель"],
    ["协议服务", "Protocol Service", "Служба протоколов"],
    ["四遥转发", "Telemetry Forwarding", "Ретрансляция"],
    ["设备量测", "Device Measurements", "Измерения устройств"],
    ["SCADA · 运行工作台", "SCADA · Operations Workbench", "SCADA · Рабочая панель"],
    ["SCADA - 运行工作台", "SCADA - Operations Workbench", "SCADA — Рабочая панель"],
    ["SCADA · 进程监控", "SCADA · Process Monitor", "SCADA · Монитор процессов"],
    ["SCADA · 通信监控", "SCADA · Comm Monitor", "SCADA · Монитор связи"],
    ["SCADA · 通信测试", "SCADA · Comm Test", "SCADA · Тест связи"],
    ["SCADA · 运行诊断", "SCADA · Diagnostics", "SCADA · Диагностика"],
    ["SCADA · 指令追踪", "SCADA · Command Traces", "SCADA · Трасса команд"],
    ["SCADA · 数据 API", "SCADA · Data API", "SCADA · API данных"],
    ["SCADA · 通信 API", "SCADA · Comm API", "SCADA · API связи"],
    ["SCADA 首页", "SCADA Home", "Главная SCADA"],
    ["SCADA 进程监控", "SCADA Processes", "Процессы SCADA"],
    ["SCADA 通信监控", "SCADA Communications", "Связь SCADA"],
    ["SCADA 通信测试", "SCADA Comm Test", "Тест связи SCADA"],
    ["scdfe_c · 运行工作台", "scdfe_c · Operations", "scdfe_c · Панель"],
    ["scdfe_s · 协议服务", "scdfe_s · Protocols", "scdfe_s · Протоколы"],
    ["scdfe_s · 通信测试", "scdfe_s · Comm Test", "scdfe_s · Тест связи"],
    ["scdfe_s · 通道数据", "scdfe_s · Channels", "scdfe_s · Каналы"],
    ["scdfe_s · 四遥数据", "scdfe_s · Points", "scdfe_s · Точки ТМ/ТУ"],
    ["scdfe_s · 日志报文", "scdfe_s · Logs & Packets", "scdfe_s · Журналы"],
    ["scdfe_t · 四遥转发", "scdfe_t · Forwarding", "scdfe_t · Ретрансляция"],
    ["scdfe_t 四遥转发", "scdfe_t Forwarding", "scdfe_t Ретрансляция"],
    ["scdfe_t · 进程监控", "scdfe_t · Processes", "scdfe_t · Процессы"],
    ["scdfe_t · 通信监控", "scdfe_t · Communications", "scdfe_t · Связь"],
    ["scdfe_t · 通道数据", "scdfe_t · Channels", "scdfe_t · Каналы"],
    ["scdfe_t · 四遥数据", "scdfe_t · Points", "scdfe_t · Точки ТМ/ТУ"],
    ["scdfe_t · 日志报文", "scdfe_t · Logs & Packets", "scdfe_t · Журналы"],
    ["scdfe_d · 设备量测", "scdfe_d · Devices", "scdfe_d · Устройства"],
    ["运行诊断 · SCADA", "Diagnostics · SCADA", "Диагностика · SCADA"],
    ["指令追踪 · SCADA", "Command Traces · SCADA", "Трасса команд · SCADA"],
    ["采样管理 · SCADA", "Trend Manager · SCADA", "Тренды · SCADA"],
    ["客户端连接 · SCADA", "Connections · SCADA", "Подключения · SCADA"],
    ["IEC104 · SOE 事件", "IEC104 · SOE Events", "IEC104 · События SOE"],
    ["接口文档 ↗", "API Docs ↗", "API ↗"],
    ["API 文档", "API Docs", "Документация API"],
    ["返回工作台", "Back to Workbench", "На панель"],
    ["通信首页", "Comm Home", "Главная связи"],
    ["← 连接列表", "← Connections", "← Список подключений"],
    ["连接检查中…", "Checking…", "Проверка…"],
    ["连接检查中", "Checking…", "Проверка…"],
    ["Web 已连接", "Web Connected", "Подключено"],
    ["Web 连接失败", "Disconnected", "Нет связи"],
    ["连接失败", "Disconnected", "Нет связи"],
    ["5 秒刷新", "5s Auto", "Авто 5с"],
    ["实时刷新（2秒）", "Live (2s)", "Онлайн (2с)"],
    ["等待更新", "Pending", "Ожидание"],
    ["等待刷新", "Waiting…", "Ожидание…"],
    ["跳到页面内容", "Skip to content", "К содержимому"],
    ["选择数据库", "Database", "База данных"],
    ["切换数据库", "Switch Database", "Сменить БД"],
    ["当前数据库", "Current Database", "Текущая БД"],
    ["当前数据库路径", "Current DB Path", "Путь к БД"],
    ["数据库目录", "Database Directory", "Каталог БД"],
    ["数据库相对路径", "Relative DB Path", "Относит. путь БД"],
    ["工作区根目录", "Workspace Root", "Корень рабочей области"],
    ["运行态", "Runtime", "Среда"],
    ["运行态名称", "Runtime Name", "Имя среды"],
    ["场景", "Scene", "Сценарий"],
    ["场景名称", "Scene Name", "Имя сценария"],
    ["无运行态", "No Runtime", "Без среды"],
    ["无场景", "No Scene", "Без сценария"],
    ["当前目录", "Current Folder", "Текущий каталог"],
    ["上一级", "Up", "Вверх"],
    ["浏览", "Browse", "Обзор"],
    ["展开数据库目录", "Expand DB Tree", "Развернуть дерево БД"],
    ["收起数据库目录", "Collapse DB Tree", "Свернуть дерево БД"],
    ["新数据库文件名", "New DB Filename", "Имя файла БД"],
    ["新目录名称", "New Folder Name", "Имя папки"],
    ["选择源数据库", "Select Source DB", "Выбрать исходную БД"],
    ["选择设备模型数据库", "Select Model DB", "Выбрать БД модели"],
    ["独立模拟库", "Standalone Sim DB", "Автономная БД"],
    ["C/T 共享业务库", "Shared C/T DB", "Общая БД C/T"],
    ["模拟数据库", "Simulator DB", "БД симулятора"],
    ["源数据库", "Source DB", "Исходная БД"],
    ["源库不可用", "Source DB Unavailable", "Исходная БД недоступна"],

    // ==================== 侧边栏导航 ====================
    ["运行管理", "Operations", "Управление"],
    ["数据与定义", "Data & Schema", "Данные и схемы"],
    ["通信测试", "Comm Test", "Тест связи"],
    ["系统首页", "Overview", "Главная"],
    ["潮流图", "Power Flow", "Схема потоков"],
    ["设备图", "Devices", "Оборудование"],
    ["通信图", "Telemetry", "Связь и ТМ"],
    ["南极秦岭站综合能量管理系统", "Antarctic Qinling Station EMS", "ЭМС станции Циньлин"],
    ["站内一次接线与实时潮流图", "Primary Single-Line & Live Power Flow", "Однолинейная схема и потоки мощности"],
    ["设备日曲线", "24h Device Trend", "Суточный график"],
    ["在潮流图定位", "Locate in Diagram", "Найти на схеме"],
    ["查看设备日曲线", "View 24h Trend", "Суточный график"],
    ["运行总天数", "Operating Days", "Дней работы"],
    ["负荷总用电", "Total Load", "Потребление"],
    ["柴油总发电", "Diesel Gen", "Дизель-ген."],
    ["新能源总发电", "Renewable Gen", "ВИЭ генерация"],
    ["柴油总节约量", "Diesel Saved", "Экономия дизеля"],
    ["新能源", "Renewables", "ВИЭ"],
    ["风机总发电", "Wind Gen", "Ветрогенерация"],
    ["光伏总发电", "PV Gen", "Солнечная ген."],
    ["储能总发电", "Storage Output", "Накопители"],
    ["氢能总发电", "Hydrogen Gen", "Водород"],
    ["新能源占比", "Renewable Share", "Доля ВИЭ"],
    ["光伏", "PV", "СЭС"],
    ["风机", "Wind", "ВЭС"],
    ["制氢", "H2 Prod", "Электролиз"],
    ["氢电", "H2 Gen", "ТЭ H2"],
    ["柴发", "Diesel", "ДГУ"],
    ["电储", "BESS", "СНЭ"],
    ["负荷", "Load", "Нагрузка"],
    ["在线 RTU", "Online RTUs", "RTU в сети"],
    ["有效量测", "Valid Points", "Валидные точки"],
    ["总发电功率", "Total Gen", "Сумм. генерация"],
    ["总用电负荷", "Total Load", "Сумм. нагрузка"],
    ["RTU 状态", "RTU Status", "Статус RTU"],
    ["通信通道", "Channels", "Каналы связи"],
    ["实时在线", "Live Online", "В сети"],
    ["导出日志", "Export Logs", "Экспорт логов"],
    ["时间窗", "Window", "Интервал"],
    ["近 1 小时", "Last 1h", "За 1 ч"],
    ["近 6 小时", "Last 6h", "За 6 ч"],
    ["近 12 小时", "Last 12h", "За 12 ч"],
    ["近 24 小时", "Last 24h", "За 24 ч"],
    ["进程监控", "Processes", "Процессы"],
    ["通信监控", "Communications", "Каналы связи"],
    ["运行日志", "Runtime Logs", "Журнал работы"],
    ["日志报文", "Logs & Packets", "Журналы и кадры"],
    ["报文日志", "Packet Logs", "Кадры обмена"],
    ["SOE 事件", "SOE Events", "События SOE"],
    ["运行诊断", "Diagnostics", "Диагностика"],
    ["指令追踪", "Command Traces", "Трасса команд"],
    ["采样管理", "Trend Manager", "Тренды"],
    ["客户端连接", "Connections", "Подключения"],
    ["通道数据", "Channels", "Каналы"],
    ["四遥数据", "Four-Remote", "Точки ТМ/ТУ"],
    ["通道定义", "Channels", "Каналы"],
    ["RTU 定义", "RTUs", "Устройства RTU"],
    ["四遥定义", "Points", "Точки ТМ/ТУ"],
    ["协议参数", "Protocol Params", "Параметры"],
    ["规约参数", "Protocol Params", "Параметры"],
    ["辅助表", "Aux Tables", "Доп. таблицы"],
    ["通道配置", "Channel Config", "Параметры канала"],
    ["连接信息", "Connections", "Соединения"],
    ["连接详情", "Details", "Детали"],
    ["连接监控", "Conn Monitor", "Монитор связей"],
    ["连接记录", "Connections", "Подключения"],
    ["连接日志", "Conn Logs", "Журнал связей"],
    ["客户端连接详情", "Connection Details", "Детали подключения"],
    ["设备采集", "Collector", "Сбор данных"],
    ["设备量测采集", "Device Collector", "Сбор измерений"],
    ["采集与修改记录", "Audit & History", "История и аудит"],
    ["量测定义", "Mappings", "Привязки"],
    ["量测查询", "Live Values", "Текущие значения"],
    ["参数配置", "Parameters", "Настройки"],
    ["数据与定义子目录", "Data & Schema Menu", "Меню данных и схем"],

    // ==================== 通用按钮与筛选 ====================
    ["刷新", "Refresh", "Обновить"],
    ["刷新日志", "Refresh Logs", "Обновить"],
    ["刷新最新", "Latest", "Новые"],
    ["刷新并检查配置", "Refresh & Check", "Проверить"],
    ["查询", "Query", "Поиск"],
    ["查找", "Find", "Найти"],
    ["检索", "Search", "Поиск"],
    ["筛选", "Filter", "Фильтр"],
    ["清空", "Clear", "Сброс"],
    ["清空条件", "Reset", "Сбросить"],
    ["清除筛选", "Reset Filters", "Сбросить"],
    ["新增", "Add", "Добавить"],
    ["新建", "New", "Создать"],
    ["创建", "Create", "Создать"],
    ["编辑", "Edit", "Изменить"],
    ["删除", "Delete", "Удалить"],
    ["移除", "Remove", "Удалить"],
    ["保存", "Save", "Сохранить"],
    ["保存修改", "Save Changes", "Сохранить"],
    ["保存配置", "Save Config", "Сохранить"],
    ["保存配置并按需重载", "Save & Reload", "Сохранить и перезагрузить"],
    ["保存服务端配置", "Save Server Config", "Сохранить сервер"],
    ["保存规约参数", "Save Params", "Сохранить параметры"],
    ["删除规约参数", "Delete Params", "Удалить параметры"],
    ["保存测点", "Save Point", "Сохранить точку"],
    ["更新测点", "Update Point", "Обновить точку"],
    ["保存映射", "Save Mapping", "Сохранить связь"],
    ["保存关联", "Save Rule", "Сохранить правило"],
    ["保存保留时间", "Save Retention", "Сохранить срок"],
    ["保存间隔", "Save Interval", "Сохранить интервал"],
    ["取消", "Cancel", "Отмена"],
    ["关闭", "Close", "Закрыть"],
    ["确认", "Confirm", "Подтвердить"],
    ["确认操作", "Confirm Action", "Подтвердить"],
    ["确认删除", "Confirm Delete", "Удалить"],
    ["确认导入", "Confirm Import", "Импортировать"],
    ["确认下发", "Send Command", "Отправить"],
    ["确认结案", "Confirm Resolve", "Закрыть"],
    ["完成", "Done", "Готово"],
    ["应用", "Apply", "Применить"],
    ["启动", "Start", "Пуск"],
    ["停止", "Stop", "Стоп"],
    ["启动服务", "Start Service", "Запустить"],
    ["停止服务", "Stop Service", "Остановить"],
    ["启动服务端", "Start Server", "Запустить сервер"],
    ["停止服务端", "Stop Server", "Остановить сервер"],
    ["启动主站", "Start Master", "Запустить мастер"],
    ["停止主站", "Stop Master", "Остановить мастер"],
    ["启动总线", "Start Bus", "Запустить шину"],
    ["停止总线", "Stop Bus", "Остановить шину"],
    ["启动规约服务", "Start Protocol", "Запустить службу"],
    ["停止规约服务", "Stop Protocol", "Остановить службу"],
    ["一键启动全部", "Start All", "Запустить все"],
    ["一键停止全部", "Stop All", "Остановить все"],
    ["进程启停", "Start / Stop", "Пуск / Стоп"],
    ["启动调试", "Start Debug", "Отладка"],
    ["导出本批", "Export Batch", "Экспорт"],
    ["导出当前显示", "Export View", "Экспорт"],
    ["导出 CSV", "Export CSV", "Экспорт CSV"],
    ["导出 E 文件", "Export E-File", "Экспорт E-файла"],
    ["导入 E 文件", "Import E-File", "Импорт E-файла"],
    ["保存文件", "Save File", "Сохранить файл"],
    ["文件名", "File Name", "Имя файла"],
    ["E 文件", "E-File", "E-файл"],
    ["冲突策略", "Conflict Strategy", "При конфликте"],
    ["自动分配新编号（保留现有配置）", "Auto-assign new IDs (keep existing)", "Назначить новые ID (сохранить текущие)"],
    ["按原编号覆盖已有通道", "Overwrite existing IDs", "Перезаписать существующие ID"],
    ["目标服务端主机（可选）", "Target Server Host (Optional)", "Хост сервера (необязательно)"],
    ["留空则使用 E 文件中的地址", "Leave blank to use address in E-file", "Пусто — использовать адрес из E-файла"],
    ["确认覆盖导入", "Confirm Overwrite Import", "Подтвердить перезапись"],
    ["确认覆盖", "Overwrite", "Перезаписать"],
    ["上一页", "Prev", "Назад"],
    ["下一页", "Next", "Далее"],
    ["更早记录", "Older", "Ранее"],
    ["加载更早记录", "Load Older", "Загрузить ранее"],
    ["较早一批", "Older Batch", "Ранее"],
    ["较新一批", "Newer Batch", "Новее"],
    ["查看最新", "Latest", "Последние"],
    ["从最早读取", "From Oldest", "С начала"],
    ["继续读取", "Load More", "Ещё"],
    ["回到最新", "Back to Latest", "К новым"],
    ["显示筛选", "Show Filters", "Фильтры"],
    ["隐藏筛选", "Hide Filters", "Скрыть фильтры"],
    ["跟随最新", "Follow Live", "Следить"],
    ["自动刷新", "Auto Refresh", "Автообновление"],
    ["自动换行", "Wrap Lines", "Перенос строк"],
    ["全部", "All", "Все"],
    ["全部 RTU", "All RTUs", "Все RTU"],
    ["全部通道", "All Channels", "Все каналы"],
    ["全部通信通道", "All Comm Channels", "Все каналы связи"],
    ["全部转发通道", "All Forward Channels", "Все каналы ретр."],
    ["全部文件", "All Files", "Все файлы"],
    ["全部方向", "All Directions", "Все направления"],
    ["全部类型", "All Types", "Все типы"],
    ["全部状态", "All States", "Все статусы"],
    ["全部进程", "All Processes", "Все процессы"],
    ["全部字段", "All Fields", "Все поля"],
    ["常用字段", "Common Fields", "Основные поля"],
    ["当前文件", "Current File", "Текущий файл"],
    ["全部匹配文件", "All Matching", "Все файлы"],
    ["新增测点", "Add Point", "Добавить точку"],
    ["新增记录", "Add Record", "Добавить запись"],
    ["＋ 新增记录", "+ Add Record", "+ Добавить запись"],
    ["＋ 新建 RTU", "+ New RTU", "+ Новый RTU"],
    ["＋ 新建通道", "+ New Channel", "+ Новый канал"],
    ["新建通道", "New Channel", "Новый канал"],
    ["编辑通道", "Edit Channel", "Изменить канал"],
    ["编辑记录", "Edit Record", "Изменить запись"],
    ["编辑四遥点", "Edit Point", "Изменить точку"],
    ["编辑规约参数", "Edit Protocol Params", "Параметры протокола"],
    ["批量加入", "Batch Add", "Пакетно"],
    ["批量新增点映射", "Batch Add Mappings", "Пакетное добавление"],
    ["新增设备", "Add Device", "Добавить устр."],
    ["导入设备模型", "Import Model", "Импорт модели"],
    ["确认导入设备模型", "Confirm Model Import", "Подтвердить импорт"],
    ["新增属性映射", "Add Mapping", "Добавить связь"],
    ["新增量测关联", "Add Mapping", "Добавить связь"],
    ["编辑量测关联", "Edit Mapping", "Изменить связь"],
    ["确认删除量测映射", "Delete Mapping", "Удалить связь"],
    ["采集一次", "Collect Once", "Опрос"],
    ["立即采集", "Collect Now", "Опросить"],
    ["读取一次", "Read Once", "Прочитать"],
    ["启动采集", "Start Collector", "Пуск сбора"],
    ["停止采集", "Stop Collector", "Стоп сбора"],
    ["保存周期", "Save Interval", "Сохранить"],
    ["保存参数", "Save Params", "Сохранить"],
    ["撤销修改", "Discard", "Отменить"],
    ["批量设置", "Bulk Edit", "Пакетно"],
    ["批量设置量测参数", "Bulk Edit Measurement Params", "Пакетная настройка"],
    ["加入待保存修改", "Stage Changes", "В черновик"],
    ["配置回采关联", "Feedback Rules", "Правила ОС"],
    ["命令回采关联", "Command Feedback Rules", "Правила обратной связи"],
    ["回采关联", "Feedback", "Обратная связь"],
    ["查看", "View", "Открыть"],
    ["查看与配置", "View & Config", "Просмотр и настройка"],
    ["打开测试", "Open Test", "Открыть тест"],
    ["详情", "Details", "Детали"],
    ["完整记录", "Full Record", "Полная запись"],
    ["查看完整记录", "View Full Record", "Полная запись"],
    ["查看通道", "Channels", "Каналы"],
    ["表结构", "Schema", "Схема"],
    ["下发", "Send", "Отправить"],
    ["下发指令", "Send Command", "Отправить команду"],
    ["更新", "Update", "Обновить"],
    ["发送", "Send", "Отправить"],
    ["发送帧", "Send Frame", "Отправить кадр"],
    ["发送 CAN 报文", "Send CAN Frame", "Отправить кадр CAN"],
    ["全量更新", "Full Refresh", "Полное обновление"],
    ["人工核对", "Manual Review", "Ручная проверка"],
    ["记录人工核对", "Record Review", "Записать проверку"],
    ["详情 / 结案", "Details / Resolve", "Детали / Закрыть"],
    ["人工结案", "Manual Resolve", "Ручное закрытие"],
    ["指令详情与结案", "Command Details & Resolve", "Детали и закрытие"],
    ["运行参数与日志保留", "Runtime & Log Retention", "Параметры и хранение логов"],
    ["线程启动间隔和日志文件保留时间", "Startup Interval & Log Retention", "Интервал запуска и хранение логов"],
    ["TCP 指令服务配置", "TCP Command Server Config", "Настройка TCP-сервера команд"],
    ["查找测点", "Find Point", "Найти точку"],
    ["查找命令点", "Find Command Point", "Найти точку команды"],
    ["查找回采点", "Find Feedback Point", "Найти точку ОС"],
    ["选择", "Select", "Выбрать"],
    ["请选择", "Select…", "Выберите…"],
    ["请选择 RTU", "Select RTU", "Выберите RTU"],
    ["请选择通道", "Select Channel", "Выберите канал"],
    ["请选择日志文件", "Select Log File", "Выберите файл журнала"],
    ["关联日志 ↗", "Logs ↗", "Журнал ↗"],
    ["日志↗", "Logs ↗", "Лог ↗"],
    ["日志", "Logs", "Журнал"],
    ["四遥", "Points", "Точки"],
    ["RTU参数", "RTU Params", "Параметры RTU"],
    ["通信", "Comm", "Связь"],
    ["进程", "Process", "Процесс"],
    ["绑定测点后配置", "Bind Point First", "Сначала привяжите точку"],
    ["查看量测 →", "View Values →", "Измерения →"],

    // ==================== 进程监控 / 通信监控 KPI 与表头 ====================
    ["主进程", "Main Process", "Главный процесс"],
    ["数据库写进程", "DB Writer Process", "Процесс записи БД"],
    ["TCP指令服务进程", "TCP Command Server", "TCP-сервер команд"],
    ["登记进程", "Registered", "Всего процессов"],
    ["进程在线", "Online", "В сети"],
    ["进程异常", "Faulted", "Сбои"],
    ["已选调试", "Debug", "Отладка"],
    ["RTU 总数", "Total RTUs", "Всего RTU"],
    ["线程运行中", "Running Threads", "Активных потоков"],
    ["线程错误", "Thread Errors", "Ошибок потоков"],
    ["参与自动通信", "Auto Comm", "Автосвязь"],
    ["独立调试 RTU", "Debug RTU", "Отладка RTU"],
    ["参与一键启动", "Include in Start All", "В общий запуск"],
    ["运行进程", "Processes", "Процессы"],
    ["RTU 通信线程", "RTU Comm Threads", "Потоки связи RTU"],
    ["进程 / 一键启动", "Process / Auto-Start", "Процесс / Автозапуск"],
    ["状态 / 心跳", "Status / Heartbeat", "Статус / Пульс"],
    ["PID / 父 PID", "PID / Parent PID", "PID / Родит. PID"],
    ["启动 / 停止", "Start / Stop", "Запуск / Стоп"],
    ["运行信息", "Runtime Info", "Сведения"],
    ["操作", "Actions", "Действия"],
    ["RTU / 名称", "RTU / Name", "RTU / Имя"],
    ["通道 / 规约", "Channel / Protocol", "Канал / Протокол"],
    ["IP 地址及端口", "IP Address & Port", "IP-адрес и порт"],
    ["自动通信", "Auto Comm", "Автосвязь"],
    ["线程状态", "Thread State", "Поток"],
    ["连接状态", "Connection", "Связь"],
    ["线程 ID", "Thread ID", "ID потока"],
    ["收 / 发 / 错", "RX / TX / Err", "Пр / Прд / Ош"],
    ["收 / 发 / 错 / 重连", "RX / TX / Err / Reconn", "Пр / Прд / Ош / Переп"],
    ["收/发/错/重连", "RX / TX / Err / Reconn", "Пр / Прд / Ош / Переп"],
    ["收 / 发报文", "RX / TX Frames", "Кадры Пр / Прд"],
    ["收 / 发字节", "RX / TX Bytes", "Байты Пр / Прд"],
    ["接收 / 发送", "RX / TX", "Приём / Передача"],
    ["最近心跳 / 采样", "Heartbeat / Sample", "Пульс / Опрос"],
    ["心跳 / 采样", "Heartbeat / Sample", "Пульс / Опрос"],
    ["诊断 / 错误信息", "Diagnostics / Error", "Диагностика / Ошибка"],
    ["诊断 / 最近错误", "Diagnostics / Last Error", "Диагностика / Ошибка"],
    ["诊断 / 错误", "Diagnostics / Error", "Диагностика / Ошибка"],
    ["最近错误", "Last Error", "Последняя ошибка"],
    ["诊断", "Diagnostics", "Диагностика"],
    ["心跳", "Heartbeat", "Пульс"],
    ["启动时间", "Started", "Запуск"],
    ["停止时间", "Stopped", "Стоп"],
    ["刷新时间", "Refreshed", "Обновлено"],
    ["采样时间", "Sampled", "Время опроса"],
    ["采集时间", "Collected", "Время сбора"],
    ["源时间", "Source Time", "Время источника"],
    ["源时间 UTC", "Source Time (UTC)", "Время ист. (UTC)"],
    ["接收/记录时间 UTC", "Received / Logged (UTC)", "Приём / Запись (UTC)"],
    ["确认保存时间 UTC", "Confirmed (UTC)", "Подтверждено (UTC)"],
    ["指令接收时间", "Received At", "Время приёма"],
    ["接入 / 断开时间", "Opened / Closed", "Открыто / Закрыто"],
    ["接入时间", "Opened", "Открыто"],
    ["断开时间", "Closed", "Закрыто"],
    ["完成时间", "Completed", "Завершено"],
    ["开始时间", "Start Time", "Начало"],
    ["进程号", "PID", "PID"],
    ["退出码", "Exit Code", "Код выхода"],
    ["启动", "Start", "Пуск"],
    ["线程启动间隔", "Thread Start Interval", "Интервал запуска"],
    ["日志保留", "Log Retention", "Хранение логов"],
    ["日志保留小时", "Log Retention (h)", "Хранение логов (ч)"],
    ["小时", "hours", "ч"],
    ["秒", "s", "с"],
    ["监听地址", "Bind Host", "Адрес прослуш."],
    ["监听端口", "Bind Port", "Порт прослуш."],
    ["服务监听地址", "Service Listen Address", "Адрес службы"],
    ["绑定地址", "Bind Address", "Адрес привязки"],
    ["认证令牌", "Auth Token", "Токен"],
    ["远端主机", "Remote Host", "Удалённый хост"],
    ["远端端口", "Remote Port", "Удалённый порт"],
    ["端口", "Port", "Порт"],
    ["UDP 端口", "UDP Port", "Порт UDP"],
    ["最大连接数", "Max Connections", "Макс. подключений"],
    ["连接数", "Connections", "Подключения"],
    ["连接超时", "Connect Timeout", "Таймаут соед."],
    ["响应超时", "Response Timeout", "Таймаут ответа"],
    ["接收超时", "Receive Timeout", "Таймаут приёма"],
    ["扫描周期", "Scan Interval", "Период опроса"],
    ["周期（毫秒）", "Interval (ms)", "Период (мс)"],
    ["采集周期（秒）", "Poll Interval (s)", "Период сбора (с)"],
    ["最长留存（分钟）", "Max Retention (min)", "Хранение (мин)"],
    ["超时阈值（秒）", "Timeout (s)", "Таймаут (с)"],
    ["重试次数", "Retries", "Повторы"],
    ["保持连接", "Keep-Alive", "Удерживать связь"],
    ["保存报文", "Record Packets", "Запись кадров"],
    ["报文保存", "Packet Recording", "Запись кадров"],
    ["报文测试", "Packet Test", "Тест кадров"],
    ["模拟模式", "Simulation Mode", "Режим симуляции"],
    ["工作模式", "Work Mode", "Режим работы"],
    ["通信通道进程", "Comm Channel Processes", "Процессы каналов связи"],
    ["转发通道进程", "Forwarding Processes", "Процессы ретрансляции"],
    ["通道列表", "Channels", "Список каналов"],
    ["通道详情", "Channel Details", "Детали канала"],
    ["通道与测点", "Channels & Points", "Каналы и точки"],
    ["通道参数", "Channel Params", "Параметры канала"],
    ["通道号", "Channel ID", "№ канала"],
    ["通道编号", "Channel ID", "№ канала"],
    ["通道类型", "Channel Type", "Тип канала"],
    ["通道标识", "Channel Key", "Идент. канала"],
    ["通道", "Channel", "Канал"],
    ["通信通道", "Comm Channel", "Канал связи"],
    ["转发通道", "Forward Channel", "Канал ретрансляции"],
    ["转发通道名称", "Forward Channel Name", "Имя канала ретр."],
    ["当前通道", "Current Channel", "Текущий канал"],
    ["当前关联通道", "Linked Channel", "Связанный канал"],
    ["通道 / RTU", "Channel / RTU", "Канал / RTU"],
    ["通道 / RTU 名称", "Channel / RTU Name", "Канал / Имя RTU"],
    ["通道 / 快照", "Channel / Snapshot", "Канал / Снимок"],
    ["服务 / 规约", "Service / Protocol", "Служба / Протокол"],
    ["服务 / 通道", "Service / Channel", "Служба / Канал"],
    ["服务状态", "Service Status", "Статус службы"],
    ["规约服务状态", "Protocol Services", "Статус служб"],
    ["规约", "Protocol", "Протокол"],
    ["规约类型", "Protocol Type", "Тип протокола"],
    ["规约阶段", "Protocol Stage", "Этап протокола"],
    ["通信状态", "Comm Status", "Статус связи"],
    ["运行状态", "Runtime State", "Состояние"],
    ["服务", "Service", "Служба"],
    ["主站", "Master", "Мастер"],
    ["服务端", "Server", "Сервер"],
    ["主站测试", "Master Test", "Тест мастера"],
    ["服务端测试", "Server Test", "Тест сервера"],
    ["服务端监听配置", "Server Listen Config", "Настройка сервера"],
    ["服务端串口配置", "Server Serial Config", "Настройка COM-порта"],
    ["CAN 主站", "CAN Master", "CAN Мастер"],
    ["CAN 总线主站", "CAN Bus Master", "CAN-шина Мастер"],
    ["CAN 服务端", "CAN Server", "CAN Сервер"],
    ["CAN 通信", "CAN Comm", "CAN Связь"],
    ["CAN 通信主站", "CAN Master", "CAN Мастер"],
    ["CAN 通道配置", "CAN Channel Config", "Настройка канала CAN"],
    ["CAN 服务端配置", "CAN Server Config", "Настройка сервера CAN"],
    ["IEC104 通道配置", "IEC104 Channel Config", "Настройка канала IEC104"],
    ["IEC104 四遥", "IEC104 Points", "Точки IEC104"],
    ["IEC104 遥信事件", "IEC104 Binary Events", "События ТС IEC104"],
    ["IEC 60870-5-104 测试服务", "IEC 60870-5-104 Test Service", "Тестовый сервис IEC 60870-5-104"],
    ["Modbus-TCP 主站", "Modbus-TCP Master", "Modbus-TCP Мастер"],
    ["Modbus-TCP 服务端", "Modbus-TCP Server", "Modbus-TCP Сервер"],
    ["Modbus-TCP 通信", "Modbus-TCP Comm", "Modbus-TCP Связь"],
    ["Modbus-TCP 通道配置", "Modbus-TCP Channel Config", "Канал Modbus-TCP"],
    ["Modbus-RTU 主站", "Modbus-RTU Master", "Modbus-RTU Мастер"],
    ["Modbus-RTU 服务端", "Modbus-RTU Server", "Modbus-RTU Сервер"],
    ["Modbus-RTU 通信", "Modbus-RTU Comm", "Modbus-RTU Связь"],
    ["Modbus-RTU 通道配置", "Modbus-RTU Channel Config", "Канал Modbus-RTU"],
    ["Modbus RTU 主站", "Modbus RTU Master", "Modbus RTU Мастер"],
    ["Modbus RTU 从站", "Modbus RTU Slave", "Modbus RTU Слейв"],
    ["Modbus TCP 主站", "Modbus TCP Master", "Modbus TCP Мастер"],
    ["Modbus TCP 从站", "Modbus TCP Slave", "Modbus TCP Слейв"],
    ["BACnet/IP 服务端", "BACnet/IP Server", "BACnet/IP Сервер"],
    ["BACnet/IP 服务端配置", "BACnet/IP Server Config", "Настройка BACnet/IP"],
    ["BACnet 服务端", "BACnet Server", "BACnet Сервер"],
    ["OPC UA 服务端", "OPC UA Server", "OPC UA Сервер"],
    ["OPC UA 服务端配置", "OPC UA Server Config", "Настройка OPC UA"],
    ["串口", "Serial Port", "COM-порт"],
    ["波特率", "Baud Rate", "Скорость (бод)"],
    ["数据位", "Data Bits", "Биты данных"],
    ["停止位", "Stop Bits", "Стоп-биты"],
    ["校验", "Parity", "Чётность"],
    ["校验方式", "Parity", "Чётность"],
    ["从站地址", "Slave Address", "Адрес ведомого"],
    ["接口", "Interface", "Интерфейс"],
    ["接口类型", "Interface Type", "Тип интерфейса"],
    ["仲裁波特率", "Arbitration Bitrate", "Битрейт арбитража"],
    ["仲裁段波特率", "Arbitration Bitrate", "Битрейт арбитража"],
    ["数据波特率", "Data Bitrate", "Битрейт данных"],
    ["数据段波特率", "Data Bitrate", "Битрейт данных"],
    ["仲裁 ID", "Arbitration ID", "ID арбитража"],
    ["帧 ID", "Frame ID", "ID кадра"],
    ["请求帧 ID", "Request Frame ID", "ID кадра запроса"],
    ["应答帧 ID", "Response Frame ID", "ID кадра ответа"],
    ["应答数据", "Response Data", "Данные ответа"],
    ["扩展帧", "Extended Frame", "Расширенный кадр"],
    ["默认扩展帧", "Default Extended Frame", "Расш. кадр по умолч."],
    ["接收自身报文", "Receive Own Messages", "Приём своих кадров"],
    ["数据 HEX", "Data (HEX)", "Данные HEX"],
    ["数据类型", "Data Type", "Тип данных"],
    ["字节序", "Byte Order", "Порядок байт"],
    ["字序", "Word Order", "Порядок слов"],
    ["命名空间 URI", "Namespace URI", "URI пространства имён"],
    ["Endpoint 路径", "Endpoint Path", "Путь Endpoint"],
    ["信息体地址（IOA）", "Information Object Addr (IOA)", "Адрес IOA"],
    ["类型标识", "Type ID", "Идентификатор типа"],
    ["IEC 类型号", "IEC Type ID", "Тип IEC"],
    ["地址基准", "Address Base", "База адреса"],
    ["发送窗口 k", "Send Window k", "Окно передачи k"],
    ["接收确认窗口 w", "Ack Window w", "Окно подтв. w"],
    ["确认超时 t1(s)", "Ack Timeout t1 (s)", "Таймаут подтв. t1 (с)"],
    ["延迟确认 t2(s)", "Delayed Ack t2 (s)", "Задержка подтв. t2 (с)"],
    ["空闲测试 t3(s)", "Idle Test t3 (s)", "Тест простоя t3 (с)"],
    ["全量上送周期(ms)", "Full Sync Period (ms)", "Цикл передачи (мс)"],
    ["读取周期(ms)", "Read Period (ms)", "Период чтения (мс)"],
    ["ASDU 长度阈值", "ASDU Length Limit", "Макс. длина ASDU"],

    // ==================== 四遥、设备量测与实时曲线 ====================
    ["遥测 YC", "Analog (YC)", "ТИ (YC)"],
    ["遥信 YX", "Binary (YX)", "ТС (YX)"],
    ["遥控 YK", "Control (YK)", "ТУ (YK)"],
    ["遥调 YT", "Setpoint (YT)", "ТР (YT)"],
    ["遥测（YC）", "Analog (YC)", "ТИ (YC)"],
    ["遥信（YX）", "Binary (YX)", "ТС (YX)"],
    ["遥控（YK）", "Control (YK)", "ТУ (YK)"],
    ["遥调（YT）", "Setpoint (YT)", "ТР (YT)"],
    ["遥测", "Analog", "ТИ"],
    ["遥信", "Binary", "ТС"],
    ["遥控", "Control", "ТУ"],
    ["遥调", "Setpoint", "ТР"],
    ["遥测点表", "Analog Points", "Таблица ТИ"],
    ["遥信点表", "Binary Points", "Таблица ТС"],
    ["遥控点表", "Control Points", "Таблица ТУ"],
    ["遥调点表", "Setpoint Points", "Таблица ТР"],
    ["遥控下发", "Send Control (YK)", "Команда ТУ"],
    ["遥调下发", "Send Setpoint (YT)", "Уставка ТР"],
    ["遥调设定值", "Setpoint Value", "Значение уставки"],
    ["遥控 → 遥信", "Control → Binary", "ТУ → ТС"],
    ["遥调 → 遥测", "Setpoint → Analog", "ТР → ТИ"],
    ["四遥定义与实时值", "Points & Live Values", "Точки ТМ/ТУ и значения"],
    ["四遥点定义与实时值", "Points & Live Values", "Точки ТМ/ТУ и значения"],
    ["四遥测点映射", "Point Mappings", "Привязка точек ТМ/ТУ"],
    ["四遥点数", "Point Count", "Кол-во точек"],
    ["点表映射", "Point Mappings", "Привязка точек"],
    ["点映射", "Point Mapping", "Привязка точки"],
    ["数据库点定义", "DB Point Definitions", "Определения точек БД"],
    ["RTU 编号", "RTU ID", "№ RTU"],
    ["RTU 名称", "RTU Name", "Имя RTU"],
    ["RTU / 点号", "RTU / Point", "RTU / Точка"],
    ["RTU / 点", "RTU / Point", "RTU / Точка"],
    ["RTU / 点名", "RTU / Point Name", "RTU / Имя точки"],
    ["RTU / 通道", "RTU / Channel", "RTU / Канал"],
    ["RTU 与通道", "RTU & Channel", "RTU и канал"],
    ["RTU 参数", "RTU Params", "Параметры RTU"],
    ["点号", "Point No", "№ точки"],
    ["点数", "Points", "Точек"],
    ["测点", "Point", "Точка"],
    ["测点名称", "Point Name", "Имя точки"],
    ["点类型", "Point Kind", "Тип точки"],
    ["转发点号", "Forward Point", "Точка ретр."],
    ["转发类型", "Forward Kind", "Тип ретр."],
    ["源 RTU", "Source RTU", "Исх. RTU"],
    ["源点号", "Source Point", "Исх. точка"],
    ["源 SCADA 测点", "Source SCADA Point", "Исходная точка SCADA"],
    ["名称", "Name", "Имя"],
    ["中文名称", "Display Name", "Название"],
    ["编号", "ID", "№"],
    ["序号", "Seq", "№"],
    ["类型", "Type", "Тип"],
    ["状态", "Status", "Статус"],
    ["值", "Value", "Значение"],
    ["当前值", "Current Value", "Текущее"],
    ["实时值", "Live Value", "Текущее"],
    ["最近值", "Latest Value", "Последнее"],
    ["前次值", "Previous Value", "Предыдущее"],
    ["下发值", "Command Value", "Значение команды"],
    ["目标值", "Target Value", "Целевое значение"],
    ["新值", "New Value", "Новое значение"],
    ["命令值", "Command Value", "Значение"],
    ["品质", "Quality", "Качество"],
    ["质量", "Quality", "Качество"],
    ["原始状态", "Raw Status", "Исх. статус"],
    ["单位", "Unit", "Ед. изм."],
    ["比例", "Scale", "Коэфф."],
    ["偏移", "Offset", "Смещение"],
    ["权重", "Weight", "Вес"],
    ["保持原值", "Keep Existing", "Оставить прежнее"],
    ["曲线", "Trend", "График"],
    ["实时曲线", "Live Trends", "Графики реального времени"],
    ["查看曲线", "View Trend", "График"],
    ["实时曲线菜单", "Trend Menu", "Меню графика"],
    ["已选曲线图例", "Selected Series Legend", "Легенда кривых"],
    ["已选测点实时值双轴曲线", "Dual-Axis Live Point Trend", "Двухосевой график"],
    ["刷新周期（秒）", "Refresh (s)", "Период (с)"],
    ["采样周期（秒）", "Sample Interval (s)", "Период выборки (с)"],
    ["后台留存（点数）", "Retention (pts)", "Хранение (точек)"],
    ["后台保留（分钟）", "Retention (min)", "Хранение (мин)"],
    ["后台采样周期", "Sample Interval", "Период опроса"],
    ["时间区间", "Time Range", "Интервал"],
    ["窗口（分钟）", "Window (min)", "Окно (мин)"],
    ["对齐步长", "Align Step", "Шаг сетки"],
    ["纵轴设置", "Y-Axis Settings", "Оси Y"],
    ["纵轴设置…", "Y-Axis Settings…", "Оси Y…"],
    ["曲线设置", "Series Settings", "Настройка кривых"],
    ["曲线设置…", "Series Settings…", "Настройка кривых…"],
    ["重置视图", "Reset View", "Сбросить вид"],
    ["放大", "Zoom In", "Увеличить"],
    ["缩小", "Zoom Out", "Уменьшить"],
    ["重置", "Reset", "Сброс"],
    ["放大时间窗口", "Zoom In Time Window", "Увеличить масштаб"],
    ["缩小时间窗口", "Zoom Out Time Window", "Уменьшить масштаб"],
    ["重置为跟随当前 10 分钟窗口", "Reset to 10-min Rolling Window", "Сброс к 10-мин окну"],
    ["导出当前时段采样数据为 CSV", "Export Current Range as CSV", "Экспорт в CSV"],
    ["跟随当前时刻", "Rolling", "Скользящее окно"],
    ["固定起止", "Fixed Range", "Интервал"],
    ["自动范围", "Auto", "Авто"],
    ["自定义范围", "Manual", "Вручную"],
    ["自动", "Auto", "Авто"],
    ["自定义", "Custom", "Вручную"],
    ["左轴", "Left Axis", "Левая ось"],
    ["右轴", "Right Axis", "Правая ось"],
    ["左", "Left", "Лев."],
    ["右", "Right", "Прав."],
    ["下限", "Min", "Мин"],
    ["上限", "Max", "Макс"],
    ["最小", "Min", "Мин"],
    ["最大", "Max", "Макс"],
    ["刻度间隔", "Tick Step", "Шаг делений"],
    ["坐标轴", "Axis", "Ось"],
    ["颜色", "Color", "Цвет"],
    ["线宽", "Width", "Толщина"],
    ["隐藏此曲线", "Hide Series", "Скрыть кривую"],
    ["显示此曲线", "Show Series", "Показать кривую"],
    ["移除此曲线", "Remove Series", "Удалить кривую"],
    ["已隐藏", "Hidden", "Скрыто"],
    ["显示", "Visible", "Видно"],
    ["当前采样点", "Active Trend Points", "Активные точки тренда"],
    ["采样点列表", "Trend Points", "Список точек тренда"],
    ["设备", "Device", "Устройство"],
    ["设备 ID", "Device ID", "ID устройства"],
    ["设备名", "Device Name", "Имя устройства"],
    ["设备名称", "Device Name", "Имя устройства"],
    ["设备类型", "Device Type", "Тип устройства"],
    ["设备表", "Device Table", "Таблица устройств"],
    ["设备数", "Devices", "Устройств"],
    ["全部设备", "All Devices", "Все устройства"],
    ["全部设备表", "All Tables", "Все таблицы"],
    ["量测", "Measurement", "Измерение"],
    ["量测属性", "Measurement", "Атрибут"],
    ["属性名", "Attribute", "Атрибут"],
    ["属性名称", "Attribute Name", "Имя атрибута"],
    ["属性数", "Attributes", "Атрибутов"],
    ["属性映射", "Mappings", "Привязки"],
    ["采集映射", "Mappings", "Привязки"],
    ["采集批次", "Batches", "Пакеты сбора"],
    ["定义修改", "Config Edits", "Изменения"],
    ["可用量测", "Valid Values", "Доступные"],
    ["可用数", "Valid", "Доступно"],
    ["量测可用状态", "Availability", "Доступность"],
    ["目标设备属性", "Target Device Attribute", "Целевой атрибут"],
    ["目标设备量测", "Target Measurement", "Целевое измерение"],
    ["启用采集映射", "Enable Mapping", "Включить привязку"],
    ["采集映射启用", "Mapping Enabled", "Привязка вкл."],
    ["映射状态", "Mapping State", "Статус привязки"],
    ["映射停用", "Mapping Disabled", "Привязка откл."],
    ["禁用量测", "Disable Measurement", "Отключить измерение"],
    ["禁用量测（notuse）", "Disable (notuse)", "Отключить (notuse)"],
    ["量测禁用", "Disabled", "Отключено"],
    ["量测权重", "Weight", "Вес"],
    ["仅不可用", "Unavailable Only", "Только недоступные"],
    ["仅看待保存", "Unsaved Only", "Только изменённые"],
    ["全选当前显示的可配置量测", "Select All Visible Configurable", "Выбрать все видимые"],
    ["选择当前显示的可配置量测", "Select Visible Configurable", "Выбрать видимые"],

    // ==================== 日志浏览器、SOE、诊断与指令追踪 ====================
    ["文件过滤", "File Filter", "Фильтр файлов"],
    ["日志文件目录", "Log Files", "Файлы журналов"],
    ["日志文件列表", "Log Files", "Список журналов"],
    ["年 / 月 / 日 / 小时", "Y / M / D / H", "Г / М / Д / Ч"],
    ["范围", "Scope", "Область"],
    ["方向", "Direction", "Направление"],
    ["类型", "Level", "Тип"],
    ["报文通道号", "Channel", "Канал"],
    ["关键字", "Keyword", "Поиск"],
    ["开始", "Start", "Начало"],
    ["结束", "End", "Конец"],
    ["起始", "Start", "Начало"],
    ["每批", "Batch", "Порция"],
    ["每页", "Page Size", "На стр."],
    ["日志进程", "Log Process", "Процесс"],
    ["文件名", "Filename", "Имя файла"],
    ["内容", "Content", "Содержимое"],
    ["原始报文", "Raw Packet", "Исходный кадр"],
    ["报文", "Packet", "Кадр"],
    ["交互", "Interaction", "Событие"],
    ["交互内容", "Payload", "Содержимое"],
    ["接收 RX", "RX", "Приём RX"],
    ["发送 TX", "TX", "Передача TX"],
    ["从左侧目录选择文件，或检索全部文件", "Select a file on the left or search across all files", "Выберите файл слева или выполните поиск по всем"],
    ["已到起点", "Reached Start", "Начало файла"],
    ["还有更早日志", "Older Logs Available", "Есть более ранние"],
    ["跨文件检索 · 按文件小时由新到旧分批读取", "Cross-file search · newest to oldest by hour", "Поиск по файлам · от новых к старым"],
    ["本批无匹配日志", "No matching entries in this batch", "В этой порции совпадений нет"],
    ["本批扫描范围内没有匹配记录，可继续查看较早一批", "No match in this batch; load an older batch to continue", "Совпадений нет; загрузите более раннюю порцию"],
    ["SOE 事件顺序记录", "SOE Sequence of Events", "Журнал событий SOE"],
    ["SOE 连接进度", "SOE Consumer Progress", "Прогресс потребителей SOE"],
    ["消费者", "Consumer", "Потребитель"],
    ["已确认事件序号", "Acked Event Seq", "Подтв. № события"],
    ["已确认的转发进度", "Confirmed Progress", "Подтверждённый прогресс"],
    ["最后转发", "Last Forwarded", "Последняя отправка"],
    ["时间 / RTU", "Time / RTU", "Время / RTU"],
    ["时间 / 通道 / 点", "Time / Channel / Point", "Время / Канал / Точка"],
    ["类型 / COT", "Type / COT", "Тип / COT"],
    ["时间标志", "Time Flags", "Флаги времени"],
    ["时间标签", "Time Tag", "Метка времени"],
    ["CP56 原始字节", "Raw CP56 Bytes", "Сырые байты CP56"],
    ["无 CP56 标签", "No CP56 Tag", "Без метки CP56"],
    ["夏令时", "DST", "Летнее время"],
    ["分段耗时与配置检查", "Stage Latency & Config Checks", "Задержки этапов и проверки"],
    ["配置检查", "Config Checks", "Проверка конфигурации"],
    ["数据库等待与 WAL", "DB Wait & WAL", "Ожидание БД и WAL"],
    ["慢写库批次", "Slow Write Batches", "Медленные пакеты записи"],
    ["转发显示状态写库", "Forwarding Display Writes", "Запись статусов ретр."],
    ["转发指令入库诊断", "Forwarding Command Writes", "Запись команд ретр."],
    ["Modbus 请求调度", "Modbus Request Scheduler", "Планировщик Modbus"],
    ["阶段", "Stage", "Этап"],
    ["阶段 / 确认范围", "Stage / Scope", "Этап / Область"],
    ["累计样本 / 窗口", "Samples / Window", "Выборок / Окно"],
    ["最新 ms", "Last (ms)", "Посл. (мс)"],
    ["总耗时 ms", "Total (ms)", "Всего (мс)"],
    ["排队 ms", "Queue (ms)", "Очередь (мс)"],
    ["执行 ms", "Exec (ms)", "Выполн. (мс)"],
    ["连接 ms", "Conn (ms)", "Соед. (мс)"],
    ["写锁 ms", "Lock (ms)", "Блок. (мс)"],
    ["提交 ms", "Commit (ms)", "Коммит (мс)"],
    ["写锁 / 退避 ms", "Lock / Backoff (ms)", "Блок. / Ожид. (мс)"],
    ["SQL / 提交 ms", "SQL / Commit (ms)", "SQL / Коммит (мс)"],
    ["校验 / 更新 ms", "Valid / Apply (ms)", "Пров. / Прим. (мс)"],
    ["入队至提交 ms", "Enqueue→Commit (ms)", "Очередь→Коммит (мс)"],
    ["回执日志 ms", "Receipt Log (ms)", "Журнал квит. (мс)"],
    ["响应提交 ms", "Resp Submit (ms)", "Отправка отв. (мс)"],
    ["消息 / 更新", "Msgs / Updates", "Сообщ. / Обновл."],
    ["待保存点 / 等待 ms", "Pending / Wait (ms)", "Ожидает / Зад. (мс)"],
    ["已保存批次 / 点次", "Saved Batches / Rows", "Пакеты / Строки"],
    ["失败 / 锁冲突", "Failures / Locks", "Сбои / Блокировки"],
    ["未确认日志条数", "Unconfirmed Logs", "Неподтв. записи"],
    ["事务号 / 站号 / 功能码", "TxID / Unit / Func", "Транз. / Узел / Функц."],
    ["异常 / 连接", "Exception / Conn", "Искл. / Связь"],
    ["快照", "Snapshot", "Снимок"],
    ["遥控 / 遥调指令追踪", "Control / Setpoint Command Traces", "Трассировка команд ТУ / ТР"],
    ["命令编号", "Command ID", "ID команды"],
    ["命令类型", "Kind", "Тип команды"],
    ["命令 RTU", "Cmd RTU", "RTU команды"],
    ["命令点", "Cmd Point", "Точка команды"],
    ["命令方式", "Cmd Mode", "Режим команды"],
    ["回采 RTU", "Feedback RTU", "RTU ОС"],
    ["回采点", "Feedback Point", "Точка ОС"],
    ["回采状态", "Feedback", "Обратная связь"],
    ["回采 / 质量", "Feedback / Quality", "ОС / Качество"],
    ["回采 / 核对 / 结案", "Feedback / Review / Resolve", "ОС / Проверка / Закрытие"],
    ["回采超时（毫秒）", "Feedback Timeout (ms)", "Таймаут ОС (мс)"],
    ["允许绝对偏差", "Allowed Tolerance", "Допуск отклонения"],
    ["预期 / 偏差", "Expected / Diff", "Ожид. / Откл."],
    ["入库 / 接收", "Queued / Received", "В БД / Принято"],
    ["结果 / 失败阶段", "Result / Failed Stage", "Итог / Этап сбоя"],
    ["核对结论", "Review Result", "Итог проверки"],
    ["核对依据", "Review Notes", "Основание"],
    ["操作人", "Operator", "Оператор"],
    ["操作人声明", "Operator Confirmation", "Подтверждение оператора"],
    ["现场核实和结案依据", "Field Verification & Resolution Notes", "Основание закрытия"],
    ["现场状态符合预期", "Field State Matches Expected", "Соответствует ожидаемому"],
    ["现场状态不符合预期", "Field State Does Not Match", "Не соответствует ожидаемому"],
    ["人工结案，释放旧命令槽位", "Manual Resolve (Release Slot)", "Ручное закрытие слота"],
    ["我已核实现场，确认只结案并释放槽位，不重发旧命令", "I verified the field state; resolve slot only without resending", "Состояние проверено; закрыть слот без повторной отправки"],
    ["客户端 IP：源端口", "Client IP : Port", "IP клиента : порт"],
    ["最近收发", "Last Activity", "Активность"],
    ["最近交互", "Last Interaction", "Посл. обмен"],
    ["最近批次", "Last Batch", "Посл. пакет"],
    ["批次", "Batch", "Пакет"],
    ["连接编号", "Conn ID", "ID связи"],
    ["连接/客户端", "Conn / Client", "Связь / Клиент"],
    ["修改内容", "Changes", "Изменения"],
    ["协议字段", "Protocol Fields", "Поля протокола"],
    ["字段", "Field", "Поле"],
    ["直接执行", "Direct Execute", "Прямое выполнение"],
    ["选择并执行", "Select & Execute", "Выбор и выполнение"],
    ["选择后执行", "Select & Execute", "Выбор и выполнение"],
    ["关闭 / 0", "Off / 0", "Выкл / 0"],
    ["开启 / 1", "On / 1", "Вкл / 1"],
    ["分/关", "Open / Off", "Откл / 0"],
    ["合/开", "Close / On", "Вкл / 1"],
    ["全局写库", "Global DB Writer", "Глобальная запись БД"],

    // ==================== 状态与提示文案 ====================
    ["运行中", "Running", "Работает"],
    ["正在运行", "Running", "Работает"],
    ["运行", "Running", "Работает"],
    ["已停止", "Stopped", "Остановлен"],
    ["停止", "Stopped", "Стоп"],
    ["未启动", "Not Started", "Не запущен"],
    ["等待启动", "Waiting", "Ожидание"],
    ["启动中", "Starting", "Запуск"],
    ["停止中", "Stopping", "Остановка"],
    ["处理中", "Processing", "Обработка"],
    ["采集中", "Collecting", "Сбор…"],
    ["连接中…", "Connecting…", "Подключение…"],
    ["错误", "Error", "Ошибка"],
    ["异常", "Fault", "Сбой"],
    ["警告", "Warning", "Предупреждение"],
    ["降级", "Degraded", "Сбой"],
    ["未知", "Unknown", "Неизвестно"],
    ["正常", "OK", "Норма"],
    ["可用", "Valid", "Норма"],
    ["不可用", "Unavailable", "Недоступно"],
    ["无效", "Invalid", "Недостоверно"],
    ["（无效）", "(Invalid)", "(Недост.)"],
    ["未绑定", "Unbound", "Не привязан"],
    ["已绑定", "Bound", "Привязан"],
    ["未选择", "None", "Не выбрано"],
    ["未配置", "Unconfigured", "Не настроено"],
    ["未命名", "Unnamed", "Без имени"],
    ["未命名 RTU", "Unnamed RTU", "RTU без имени"],
    ["未命名测点", "Unnamed Point", "Точка без имени"],
    ["未标识", "Unlabeled", "Без метки"],
    ["启用", "Enabled", "Вкл"],
    ["停用", "Disabled", "Откл"],
    ["禁用", "Disabled", "Откл"],
    ["开启", "On", "Вкл"],
    ["不保存", "Off", "Не писать"],
    ["自动运行", "Auto", "Авто"],
    ["不适用", "N/A", "Н/Д"],
    ["不限", "Any", "Любой"],
    ["无", "None", "Нет"],
    ["是", "Yes", "Да"],
    ["否", "No", "Нет"],
    ["已连接", "Connected", "Подключено"],
    ["已接入", "Connected", "Подключено"],
    ["已断开", "Disconnected", "Отключено"],
    ["客户端已断开", "Client Disconnected", "Клиент отключён"],
    ["无活动连接", "No active connections", "Нет активных связей"],
    ["已保存", "Saved", "Сохранено"],
    ["未保存", "Unsaved", "Не сохранено"],
    ["已下发", "Sent", "Отправлено"],
    ["待下发", "Pending", "Ожидает"],
    ["未下发", "Not Sent", "Не отправлено"],
    ["已提交", "Submitted", "Отправлено"],
    ["未提交", "Uncommitted", "Не записано"],
    ["已提交待处理", "Committed (Pending)", "Записано (ожидает)"],
    ["已入库待派发", "Queued", "В очереди"],
    ["已开始派发", "Dispatching", "Отправка"],
    ["已开始派发，结果待确认", "Dispatched, awaiting confirmation", "Отправлено, ждёт подтв."],
    ["执行请求已提交", "Execute Sent", "Команда отправлена"],
    ["执行请求已提交，结果待确认", "Execute sent, awaiting confirmation", "Команда отправлена, ждёт подтв."],
    ["选择请求已提交", "Select Sent", "Выбор отправлен"],
    ["选择已确认", "Select Confirmed", "Выбор подтверждён"],
    ["规约已确认", "Protocol Confirmed", "Подтверждено"],
    ["驱动调用完成", "Driver Completed", "Драйвер завершил"],
    ["驱动返回", "Driver Returned", "Ответ драйвера"],
    ["响应已提交", "Response Submitted", "Ответ отправлен"],
    ["未提交响应", "No Response Submitted", "Ответ не отправлен"],
    ["S 数据库已接收", "Received by S DB", "Принято БД S"],
    ["仿真模型已接受指令", "Accepted by Simulator", "Принято симулятором"],
    ["失败或未确认", "Failed / Unconfirmed", "Сбой / Не подтв."],
    ["仍待核对", "Pending Review", "Ждёт проверки"],
    ["未确认", "Unconfirmed", "Не подтв."],
    ["未结案", "Open", "Не закрыто"],
    ["未结束", "Active", "Активно"],
    ["状态待确认", "Awaiting Confirm", "Ждёт подтверждения"],
    ["未关联回采验证", "No Feedback Rule", "Без проверки ОС"],
    ["快照已过期", "Snapshot Stale", "Снимок устарел"],
    ["数据无效", "Invalid Quality", "Недостоверные данные"],
    ["数据超时", "Data Stale", "Устаревшие данные"],
    ["源质量不合格", "Bad Source Quality", "Плохое качество ист."],
    ["源时标超前", "Source Time Ahead", "Метка времени впереди"],
    ["无源时标", "No Source Time", "Нет метки времени"],
    ["无时标", "No Timestamp", "Без метки времени"],
    ["测点缺失", "Point Missing", "Точка отсутствует"],
    ["定义已修改，等待采集", "Config changed, waiting poll", "Изменено, ждёт опроса"],
    ["独立后台采集中", "Background Collecting", "Фоновый сбор"],
    ["尚未执行采集", "Not collected yet", "Сбор ещё не выполнялся"],
    ["尚未采集", "Not collected", "Не опрошено"],
    ["采集完成", "Collection Complete", "Сбор завершён"],
    ["采集已完成", "Collection Complete", "Сбор завершён"],
    ["配置已保存", "Configuration saved", "Настройки сохранены"],
    ["测点已保存", "Point saved", "Точка сохранена"],
    ["测点已新增", "Point added", "Точка добавлена"],
    ["点值已更新", "Value updated", "Значение обновлено"],
    ["通道已保存", "Channel saved", "Канал сохранён"],
    ["通道已新增", "Channel added", "Канал добавлен"],
    ["记录已创建", "Record created", "Запись создана"],
    ["记录已更新", "Record updated", "Запись обновлена"],
    ["记录已删除", "Record deleted", "Запись удалена"],
    ["规约参数已保存", "Protocol params saved", "Параметры сохранены"],
    ["规约参数已删除", "Protocol params deleted", "Параметры удалены"],
    ["规约服务已启动", "Protocol service started", "Служба запущена"],
    ["规约服务已停止", "Protocol service stopped", "Служба остановлена"],
    ["服务已启动", "Service started", "Служба запущена"],
    ["服务已停止", "Service stopped", "Служба остановлена"],
    ["服务不可用", "Service unavailable", "Служба недоступна"],
    ["服务异常", "Service fault", "Сбой службы"],
    ["启动请求已发送", "Start requested", "Запрос запуска отправлен"],
    ["停止请求已发送", "Stop requested", "Запрос остановки отправлен"],
    ["指令已提交", "Command submitted", "Команда отправлена"],
    ["指令已发送", "Command sent", "Команда передана"],
    ["指令已提交，等待通信线程下发", "Command queued for comm thread", "Команда в очереди потока"],
    ["操作已受理", "Request accepted", "Запрос принят"],
    ["操作已完成", "Completed", "Выполнено"],
    ["TCP 指令服务配置已保存", "TCP command server config saved", "Настройки TCP-сервера сохранены"],
    ["已执行全量更新", "Full refresh executed", "Полное обновление выполнено"],
    ["已结案，旧命令未重发。", "Resolved without resending.", "Закрыто без повторной отправки."],
    ["CAN 帧已发送", "CAN frame sent", "Кадр CAN отправлен"],
    ["进程已退出，未收到正常退出状态", "Process exited unexpectedly", "Процесс завершился аварийно"],
    ["写库已停用，自动通信暂停", "DB writer stopped; auto-comm paused", "Запись БД остановлена; автосвязь на паузе"],
    ["未连接，请先 connect()", "Not connected; call connect() first", "Нет соединения; выполните connect()"],
    ["参数已配置", "Configured", "Настроено"],
    ["该规约尚未配置参数", "No protocol params configured", "Параметры не заданы"],
    ["通道异常", "Channel Fault", "Сбой канала"],
    ["监听", "Listen", "Сервер"],
    ["远端", "Remote", "Клиент"],
    ["正在读取…", "Loading…", "Загрузка…"],
    ["正在加载通道…", "Loading channels…", "Загрузка каналов…"],
    ["正在加载名称…", "Loading names…", "Загрузка имён…"],
    ["正在加载源点…", "Loading source points…", "Загрузка исходных точек…"],
    ["正在读取日志文件列表…", "Loading log files…", "Загрузка списка файлов…"],
    ["正在读取文件目录…", "Loading directory…", "Загрузка каталога…"],
    ["正在读取采样点…", "Loading trend points…", "Загрузка точек тренда…"],
    ["正在读取测点…", "Loading points…", "Загрузка точек…"],
    ["正在读取点映射…", "Loading mappings…", "Загрузка привязок…"],
    ["正在检索…", "Searching…", "Поиск…"],
    ["正在检索日志…", "Searching logs…", "Поиск в журналах…"],
    ["正在按条件检索…", "Filtering…", "Фильтрация…"],
    ["正在校验数据库…", "Validating database…", "Проверка БД…"],
    ["正在检查…", "Checking…", "Проверка…"],
    ["正在保存…", "Saving…", "Сохранение…"],
    ["正在刷新…", "Refreshing…", "Обновление…"],
    ["暂无数据", "No data", "Нет данных"],
    ["暂无记录", "No records", "Нет записей"],
    ["暂无定义", "No definitions", "Нет определений"],
    ["暂无测点", "No points", "Нет точек"],
    ["暂无点映射", "No point mappings", "Нет привязок"],
    ["暂无日志", "No logs", "Нет журналов"],
    ["暂无连接", "No connections", "Нет подключений"],
    ["暂无诊断", "No diagnostics", "Нет диагностики"],
    ["暂无 SOE 事件", "No SOE events", "Нет событий SOE"],
    ["暂无 SOE 转发样本", "No SOE samples", "Нет данных SOE"],
    ["暂无写库等待样本", "No DB wait samples", "Нет данных ожидания БД"],
    ["暂无指令入库样本", "No command write samples", "Нет данных записи команд"],
    ["暂无 Modbus 请求样本", "No Modbus request samples", "Нет данных Modbus"],
    ["暂无匹配文件", "No matching files", "Нет подходящих файлов"],
    ["暂无匹配日志", "No matching logs", "Нет подходящих записей"],
    ["暂无匹配的日志文件", "No matching log files", "Нет подходящих файлов"],
    ["当前目录没有匹配文件", "No matching files in directory", "В каталоге нет файлов"],
    ["此目录没有数据库或子目录", "No databases or folders here", "Нет баз данных или папок"],
    ["没有匹配的记录", "No matching records", "Нет совпадений"],
    ["没有匹配的测点", "No matching points", "Нет подходящих точек"],
    ["没有匹配的量测", "No matching measurements", "Нет подходящих измерений"],
    ["没有匹配的日志", "No matching logs", "Нет подходящих записей"],
    ["没有符合筛选条件的记录", "No records match the filter", "Нет записей по фильтру"],
    ["（没有匹配内容）", "(No matching entries)", "(Совпадений нет)"],
    ["暂无通道，请先在“通道定义”中创建通道。", "No channels yet. Create one in Channels first.", "Каналов нет. Сначала создайте канал."],
    ["暂无采样点。可在测点表格中右键加入实时曲线。", "No trend points yet. Check or right-click points to add.", "Нет точек тренда. Выберите точки в таблице."],
    ["勾选表格中的测点开始采样", "Select points in the table to start plotting", "Выберите точки в таблице для графика"],
    ["右键表格中的测点行设为采样点", "Right-click a table row to add as sampling point", "Нажмите ПКМ на строку таблицы для добавления точки"],
    ["时间", "Time", "Время"],
    ["当前时段暂无样本", "No samples in this time range", "Нет данных за выбранный период"],
    ["当前曲线均已隐藏", "All series are currently hidden", "Все кривые скрыты"],
    ["请在测点页面加入实时曲线", "Add points from a points table first", "Добавьте точки со страницы точек"],
    ["绑定量测后可绘制曲线", "Bind a measurement to plot", "Привяжите измерение для графика"],
    ["请求失败", "Request failed", "Ошибка запроса"],
    ["读取失败", "Read failed", "Ошибка чтения"],
    ["刷新失败", "Refresh failed", "Ошибка обновления"],
    ["筛选失败", "Filter failed", "Ошибка фильтрации"],
    ["下发失败", "Send failed", "Ошибка отправки"],
    ["读取日志失败", "Failed to load logs", "Ошибка чтения журнала"],
    ["数据库操作失败", "Database operation failed", "Ошибка операции с БД"],
    ["没有需要保存的修改", "No unsaved changes", "Нет несохранённых изменений"],
    ["请先选择通道", "Please select a channel first", "Сначала выберите канал"],
    ["请先新增设备", "Please add a device first", "Сначала добавьте устройство"],
    ["请先选择日志文件或输入完整日期时间", "Select a log file or enter a full date/time range", "Выберите файл или укажите интервал времени"],
    ["配置请到“数据与定义 → 通道数据”修改", "Edit configs under Data & Schema → Channels", "Настройки изменяются в разделе «Данные и схемы → Каналы»"],
    ["主站与服务端独立测试入口", "Standalone Master & Server Test Entry", "Тестирование мастера и сервера"],
    ["连接、测点和通道实时状态", "Live Connections, Points & Channels", "Статус связей, точек и каналов"],
    ["退出自动通信", "Disable Auto-Comm", "Выкл. автосвязь"],
    ["（左键显隐，右键设置）", "(Click to toggle, right-click for menu)", "(ЛКМ — скрыть, ПКМ — меню)"],

    // ==================== 表单 placeholder / title / aria-label ====================
    ["搜索 RTU、名称或规约", "Search RTU, name or protocol", "Поиск RTU, имени или протокола"],
    ["输入 RTU 编号", "Enter RTU ID", "Введите № RTU"],
    ["筛选进程、RTU 或 PID", "Filter process, RTU or PID", "Фильтр: процесс, RTU или PID"],
    ["协议、HEX 或日志内容", "Protocol, HEX or log text", "Протокол, HEX или текст лога"],
    ["按属性名称过滤", "Filter by attribute name", "Фильтр по атрибуту"],
    ["按名称过滤测点", "Filter points by name", "Фильтр точек по имени"],
    ["按名称或点号过滤", "Filter by name or point no", "Фильтр по имени или №"],
    ["按测点名称查找", "Search by point name", "Поиск по имени точки"],
    ["按源测点名称查找", "Search by source point name", "Поиск по исх. точке"],
    ["搜索名称、编码或主键…", "Search name, code or key…", "Поиск имени, кода или ключа…"],
    ["查找字段名称…", "Find field name…", "Поиск имени поля…"],
    ["输入文件名关键字", "Filename keyword", "Имя файла"],
    ["搜索记录", "Search records", "Поиск записей"],
    ["搜索通信线程", "Search comm threads", "Поиск потоков связи"],
    ["搜索编辑字段", "Search fields", "Поиск полей"],
    ["输入下发值", "Enter command value", "Введите значение команды"],
    ["输入新值", "Enter new value", "Введите новое значение"],
    ["自动选择", "Auto", "Авто"],
    ["RTU 名称或点名", "RTU or point name", "Имя RTU или точки"],
    ["预期值 = 命令值 × 比例 + 偏移", "Expected = Command × Scale + Offset", "Ожид. = Команда × Коэфф. + Смещ."],
    ["相对于工作区根目录，使用 / 分隔", "Relative to workspace root, use /", "Относительно корня, разделитель /"],
    ["按进程或当前 RTU 通道过滤日志文件", "Filter logs by process or RTU channel", "Фильтр логов по процессу или каналу"],
    ["按报文保存时的通道号筛选", "Filter by packet channel ID", "Фильтр по каналу кадров"],
    ["按转发通道筛选", "Filter by forwarding channel", "Фильтр по каналу ретрансляции"],
    ["筛选 RTU 编号", "Filter by RTU ID", "Фильтр по № RTU"],
    ["筛选协议参数通道", "Filter protocol channel", "Фильтр канала протокола"],
    ["筛选进程", "Filter process", "Фильтр процесса"],
    ["查看运行日志", "View runtime logs", "Открыть журнал работы"],
    ["查看通道日志", "View channel logs", "Открыть журнал канала"],
    ["显示日志筛选条件", "Show log filters", "Показать фильтры логов"],
    ["隐藏日志筛选条件，扩大内容显示区域", "Hide log filters to expand view", "Скрыть фильтры для расширения области"],
    ["上一页文件", "Previous file page", "Предыдущая страница файлов"],
    ["下一页文件", "Next file page", "Следующая страница файлов"],
    ["关闭详情", "Close details", "Закрыть детали"],
    ["关闭表结构", "Close schema", "Закрыть схему"],
    ["关闭编辑窗口", "Close editor", "Закрыть редактор"],
    ["关闭映射编辑", "Close mapping editor", "Закрыть редактор привязок"],
    ["关闭下发窗口", "Close command dialog", "Закрыть окно команды"],
    ["关闭运行参数配置", "Close runtime settings", "Закрыть параметры"],
    ["关闭 TCP 指令服务配置", "Close TCP server config", "Закрыть настройки TCP"],
    ["数据库记录概览", "Database overview", "Обзор записей БД"],
    ["通信线程状态", "Comm thread status", "Статус потоков связи"],
    ["采集概况", "Collector overview", "Обзор сбора"],
    ["每页记录数", "Rows per page", "Строк на странице"],
    ["读取日志行数", "Batch line limit", "Лимит строк"],
    ["显示字段", "Visible columns", "Отображаемые поля"],
    ["记录类别", "Log category", "Категория журнала"],
    ["日志类型", "Log level", "Уровень лога"],
    ["日志方向", "Packet direction", "Направление"],
    ["日志检索范围", "Search scope", "Область поиска"],
    ["日志关键词", "Log keyword", "Ключевое слово"],
    ["四遥类型", "Point kind", "Тип точки"],
    ["量测类型", "Measurement type", "Тип измерения"],
    ["RX=服务接收，TX=服务发送", "RX = Received, TX = Transmitted", "RX = Приём, TX = Передача"],

    // ==================== 协议/枚举下拉常用项 ====================
    ["0 · 否", "0 · No", "0 · Нет"],
    ["1 · 是", "1 · Yes", "1 · Да"],
    ["0 · 自动默认", "0 · Auto Default", "0 · Авто"],
    ["0 · 大端", "0 · Big Endian", "0 · Big Endian"],
    ["1 · 小端", "1 · Little Endian", "1 · Little Endian"],
    ["0 · 请求后断开", "0 · Close After Request", "0 · Закрывать после запроса"],
    ["1 · 保持连接", "1 · Keep Alive", "1 · Удерживать"],
    ["1 · 选择执行", "1 · Select & Execute", "1 · Выбор и выполнение"],
    ["2 · 直接执行", "2 · Direct Execute", "2 · Прямое выполнение"],
    ["N · 无校验", "N · None", "N · Без чётности"],
    ["E · 偶校验", "E · Even", "E · Чётный"],
    ["O · 奇校验", "O · Odd", "O · Нечётный"],
    ["yc · 遥测", "yc · Analog", "yc · ТИ"],
    ["yx · 遥信", "yx · Binary", "yx · ТС"],
    ["yk · 遥控", "yk · Control", "yk · ТУ"],
    ["yt · 遥调", "yt · Setpoint", "yt · ТР"],
    ["big · 大端", "big · Big Endian", "big · Big Endian"],
    ["little · 小端", "little · Little Endian", "little · Little Endian"],
    ["bool · 布尔", "bool · Boolean", "bool · Логический"],
    ["int · 有符号整数", "int · Signed Int", "int · Целое со знаком"],
    ["uint · 无符号整数", "uint · Unsigned Int", "uint · Беззнаковое целое"],
    ["float · 浮点数", "float · Float", "float · Вещественное"],
    ["str · 字符串", "str · String", "str · Строка"],
    ["1 · 线圈", "1 · Coils", "1 · Катушки"],
    ["2 · 离散输入", "2 · Discrete Inputs", "2 · Дискретные входы"],
    ["3 · 保持寄存器", "3 · Holding Registers", "3 · Регистры хранения"],
    ["4 · 输入寄存器", "4 · Input Registers", "4 · Входные регистры"],
    ["5 · 单线圈", "5 · Single Coil", "5 · Одна катушка"],
    ["6 · 单寄存器", "6 · Single Register", "6 · Один регистр"],
    ["16 · 多寄存器", "16 · Multiple Registers", "16 · Несколько регистров"],
  ];

  for (const [zh, en, ru] of PHRASES) {
    I18N_DICT["zh-CN"][zh] = zh;
    I18N_DICT["en-US"][zh] = en;
    I18N_DICT["ru-RU"][zh] = ru;
  }

  // 参数化动态句式规则：按顺序匹配含数字、时间、ID 或动态片段的文案
  const PATTERN_RULES = [
    [
      /^更新于\s+(.+)$/,
      { "en-US": (_, v) => `Updated ${v}`, "ru-RU": (_, v) => `Обновлено ${v}` },
    ],
    [
      /^已刷新\s+(.+)$/,
      { "en-US": (_, v) => `Refreshed ${v}`, "ru-RU": (_, v) => `Обновлено ${v}` },
    ],
    [
      /^检查于\s+(.+)$/,
      { "en-US": (_, v) => `Checked ${v}`, "ru-RU": (_, v) => `Проверено ${v}` },
    ],
    [
      /^匹配\s*(\d+)\s*\/\s*全部\s*(\d+)$/,
      { "en-US": (_, a, b) => `Matched ${a} / ${b}`, "ru-RU": (_, a, b) => `Найдено ${a} / ${b}` },
    ],
    [
      /^匹配\s*(\d+)\s*个\s*RTU$/,
      { "en-US": (_, n) => `Matched ${n} RTU`, "ru-RU": (_, n) => `Найдено RTU: ${n}` },
    ],
    [
      /^运行\s*(\d+)\s*\/\s*总数\s*(\d+)$/,
      { "en-US": (_, a, b) => `Running ${a} / ${b}`, "ru-RU": (_, a, b) => `Работает ${a} / ${b}` },
    ],
    [
      /^共\s*(\d+)\s*项量测$/,
      { "en-US": (_, n) => `${n} measurements`, "ru-RU": (_, n) => `Всего измерений: ${n}` },
    ],
    [
      /^共\s*(\d+)\s*项可配置量测$/,
      { "en-US": (_, n) => `${n} configurable`, "ru-RU": (_, n) => `Настраиваемых: ${n}` },
    ],
    [
      /^共\s*(\d+)\s*个(?:通信)?通道$/,
      { "en-US": (_, n) => `${n} channels`, "ru-RU": (_, n) => `Всего каналов: ${n}` },
    ],
    [
      /^共\s*(\d+)\s*条\s*·\s*当前\s*(.+?)\s*条$/,
      { "en-US": (_, total, cur) => `${total} total · showing ${cur}`, "ru-RU": (_, total, cur) => `Всего ${total} · показано ${cur}` },
    ],
    [
      /^共\s*(\d+)\s*条$/,
      { "en-US": (_, n) => `${n} items`, "ru-RU": (_, n) => `Всего: ${n}` },
    ],
    [
      /^已选\s*(\d+)\s*条$/,
      { "en-US": (_, n) => `${n} selected`, "ru-RU": (_, n) => `Выбрано: ${n}` },
    ],
    [
      /^本批匹配\s*(\d+)\s*条$/,
      { "en-US": (_, n) => `${n} matches in batch`, "ru-RU": (_, n) => `Совпадений в порции: ${n}` },
    ],
    [
      /^第\s*(\d+)\s*批\s*·\s*(\d+)\s*个文件$/,
      { "en-US": (_, b, f) => `Batch ${b} · ${f} file(s)`, "ru-RU": (_, b, f) => `Порция ${b} · файлов: ${f}` },
    ],
    [
      /^第\s*(\d+)\s*\/\s*(\d+)\s*页[，,·\s]*共\s*(\d+)\s*[点条]$/,
      { "en-US": (_, p, t, n) => `Page ${p}/${t} · ${n} total`, "ru-RU": (_, p, t, n) => `Стр. ${p}/${t} · всего ${n}` },
    ],
    [
      /^第\s*(\d+)\s*\/\s*(\d+)\s*页$/,
      { "en-US": (_, p, t) => `Page ${p} / ${t}`, "ru-RU": (_, p, t) => `Стр. ${p} / ${t}` },
    ],
    [
      /^(\d+)\s*个文件$/,
      { "en-US": (_, n) => `${n} files`, "ru-RU": (_, n) => `Файлов: ${n}` },
    ],
    [
      /^(\d+)\s*条$/,
      { "en-US": (_, n) => `${n}`, "ru-RU": (_, n) => `${n}` },
    ],
    [
      /^(\d+)\s*点$/,
      { "en-US": (_, n) => `${n} pts`, "ru-RU": (_, n) => `${n} т.` },
    ],
    [
      /^(\d+)\s*项$/,
      { "en-US": (_, n) => `${n} items`, "ru-RU": (_, n) => `${n} поз.` },
    ],
    [
      /^(\d+)\s*秒$/,
      { "en-US": (_, n) => `${n}s`, "ru-RU": (_, n) => `${n}с` },
    ],
    [
      /^当前周期\s*(\d+)\s*秒$/,
      { "en-US": (_, n) => `Interval: ${n}s`, "ru-RU": (_, n) => `Период: ${n}с` },
    ],
    [
      /^(\d{4})\s*年$/,
      { "en-US": (_, y) => `${y}`, "ru-RU": (_, y) => `${y} г.` },
    ],
    [
      /^(\d{1,2})\s*月$/,
      { "en-US": (_, m) => `M${m}`, "ru-RU": (_, m) => `${m} мес.` },
    ],
    [
      /^(\d{1,2})\s*日$/,
      { "en-US": (_, d) => `D${d}`, "ru-RU": (_, d) => `${d} д.` },
    ],
    [
      /^(\d{1,2})\s*时$/,
      { "en-US": (_, h) => `${h}:00`, "ru-RU": (_, h) => `${h}:00` },
    ],
    [
      /^主进程\s*·\s*PID\s*(.+)$/,
      { "en-US": (_, pid) => `Main · PID ${pid}`, "ru-RU": (_, pid) => `Главный · PID ${pid}` },
    ],
    [
      /^退出码\s*(-?\d+)\s*·\s*期望运行$/,
      { "en-US": (_, c) => `Exit ${c} · Expected running`, "ru-RU": (_, c) => `Код ${c} · Ожидался запуск` },
    ],
    [
      /^退出码\s*(-?\d+)$/,
      { "en-US": (_, c) => `Exit ${c}`, "ru-RU": (_, c) => `Код ${c}` },
    ],
    [
      /^心跳\s+(.+)$/,
      { "en-US": (_, v) => `HB ${v}`, "ru-RU": (_, v) => `Пульс ${v}` },
    ],
    [
      /^采样\s+(.+)$/,
      { "en-US": (_, v) => `Sample ${v}`, "ru-RU": (_, v) => `Опрос ${v}` },
    ],
    [
      /^父\s+(.+)$/,
      { "en-US": (_, v) => `PPID ${v}`, "ru-RU": (_, v) => `Род. ${v}` },
    ],
    [
      /^启\s+(.+)$/,
      { "en-US": (_, v) => `Start ${v}`, "ru-RU": (_, v) => `Пуск ${v}` },
    ],
    [
      /^停\s+(.+)$/,
      { "en-US": (_, v) => `Stop ${v}`, "ru-RU": (_, v) => `Стоп ${v}` },
    ],
    [
      /^远端\s*·\s*(.+)$/,
      { "en-US": (_, v) => `Remote · ${v}`, "ru-RU": (_, v) => `Клиент · ${v}` },
    ],
    [
      /^监听\s*·\s*(.+)$/,
      { "en-US": (_, v) => `Listen · ${v}`, "ru-RU": (_, v) => `Сервер · ${v}` },
    ],
    [
      /^通道\s*(\S+)$/,
      { "en-US": (_, id) => `Channel ${id}`, "ru-RU": (_, id) => `Канал ${id}` },
    ],
    [
      /^RTU\s*(\d+)\s*·\s*自动通信$/,
      { "en-US": (_, id) => `RTU ${id} · Auto Comm`, "ru-RU": (_, id) => `RTU ${id} · Автосвязь` },
    ],
    [
      /^RTU\s*(\d+)\s*·\s*人工调试$/,
      { "en-US": (_, id) => `RTU ${id} · Manual Debug`, "ru-RU": (_, id) => `RTU ${id} · Отладка` },
    ],
    [
      /^运行日志目录：(.*)$/,
      { "en-US": (_, p) => `Runtime logs: ${p}`, "ru-RU": (_, p) => `Каталог логов: ${p}` },
    ],
    [
      /^报文目录：(.*)$/,
      { "en-US": (_, p) => `Packet logs: ${p}`, "ru-RU": (_, p) => `Каталог кадров: ${p}` },
    ],
    [
      /^最近采样\s*(.+?)\s*·\s*后台保留\s*(\d+)\s*分钟\s*·\s*虚线表示无效品质$/,
      {
        "en-US": (_, ts, m) => `Last ${ts} · Retention ${m}m · Dashed = invalid quality`,
        "ru-RU": (_, ts, m) => `Опрос ${ts} · Хранение ${m} мин · Пунктир = недост.`,
      },
    ],
    [
      /^最近采样\s*(.+?)\s*·\s*后台保留\s*(\d+)\s*分钟$/,
      {
        "en-US": (_, ts, m) => `Last ${ts} · Retention ${m}m`,
        "ru-RU": (_, ts, m) => `Опрос ${ts} · Хранение ${m} мин`,
      },
    ],
    [
      /^后台独立采样\s*·\s*最快\s*(\d+)\s*秒$/,
      { "en-US": (_, s) => `Per-point sampling · fastest ${s}s`, "ru-RU": (_, s) => `Раздельный опрос · мин. ${s} с` },
    ],
    [
      /^后台保留\s*(\d+)\s*分钟$/,
      { "en-US": (_, m) => `Retention ${m}m`, "ru-RU": (_, m) => `Хранение ${m} мин` },
    ],
    [
      /^切换至(左轴|右轴)$/,
      {
        "en-US": (_, ax) => `Move to ${ax === "左轴" ? "Left Axis" : "Right Axis"}`,
        "ru-RU": (_, ax) => `На ${ax === "左轴" ? "левую ось" : "правую ось"}`,
      },
    ],
    [
      /^移除曲线\s*·\s*(.+)$/,
      { "en-US": (_, name) => `Remove · ${name}`, "ru-RU": (_, name) => `Удалить · ${name}` },
    ],
    [
      /^加入实时曲线\s*·\s*(.+)$/,
      { "en-US": (_, name) => `Add to Trend · ${name}`, "ru-RU": (_, name) => `На график · ${name}` },
    ],
    [
      /^新增(.*)测点$/,
      { "en-US": (_, k) => `Add ${k ? k + " " : ""}Point`, "ru-RU": (_, k) => `Добавить точку${k ? " " + k : ""}` },
    ],
    [
      /^编辑(.*)测点$/,
      { "en-US": (_, k) => `Edit ${k ? k + " " : ""}Point`, "ru-RU": (_, k) => `Изменить точку${k ? " " + k : ""}` },
    ],
    [
      /^诊断(?:字段)?\s*(\d+)\s*项$/,
      { "en-US": (_, n) => `${n} diag fields`, "ru-RU": (_, n) => `Диагн. полей: ${n}` },
    ],
  ];

  let currentTheme = "minimalist-light";
  let currentLang = "zh-CN";
  let isTranslating = false;
  const originalTexts = new WeakMap();
  const originalAttrs = new WeakMap();
  let originalDocTitle = null;

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

  function translateString(raw) {
    if (typeof raw !== "string") return raw;
    const trimmed = raw.trim();
    if (!trimmed) return raw;
    if (currentLang === "zh-CN") return raw;
    const table = I18N_DICT[currentLang];
    if (!table) return raw;
    let translated = null;
    if (Object.prototype.hasOwnProperty.call(table, trimmed)) {
      translated = table[trimmed];
    } else if (/[\u4e00-\u9fff]/.test(trimmed)) {
      for (const [regex, builders] of PATTERN_RULES) {
        const match = trimmed.match(regex);
        if (match && builders[currentLang]) {
          translated = builders[currentLang](...match);
          break;
        }
      }
    }
    if (translated === null || translated === trimmed) return raw;
    const idx = raw.indexOf(trimmed);
    return raw.slice(0, idx) + translated + raw.slice(idx + trimmed.length);
  }

  function isTranslatableString(trimmed) {
    if (!trimmed) return false;
    if (Object.prototype.hasOwnProperty.call(I18N_DICT["en-US"], trimmed)) return true;
    if (/[\u4e00-\u9fff]/.test(trimmed)) {
      for (const [regex] of PATTERN_RULES) {
        if (regex.test(trimmed)) return true;
      }
    }
    return false;
  }

  function translateTextNode(node) {
    if (!node || node.nodeType !== Node.TEXT_NODE) return;
    const parent = node.parentElement;
    if (!parent || parent.closest("script, style, code, pre, .lb-content, .log-content, #status-json, #schema-json, #ui-lang-select, #ui-theme-select")) {
      return;
    }
    const raw = node.nodeValue || "";
    const trimmed = raw.trim();
    if (!trimmed) return;
    let entry = originalTexts.get(node);
    // 若业务脚本改写了该文本节点为新中文，刷新原始缓存
    if (!entry || (entry.lastApplied !== undefined && raw !== entry.lastApplied)) {
      if (!isTranslatableString(trimmed)) return;
      entry = { raw, lastApplied: raw };
      originalTexts.set(node, entry);
    }
    const nextValue = currentLang === "zh-CN" ? entry.raw : translateString(entry.raw);
    entry.lastApplied = nextValue;
    if (node.nodeValue !== nextValue) node.nodeValue = nextValue;
    if (currentLang !== "zh-CN" && parent.matches(".nav b, button, th") && !parent.title) {
      parent.title = nextValue.trim();
    }
  }

  function translateElementAttrs(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return;
    if (el.id === "ui-lang-select" || el.id === "ui-theme-select") return;
    let map = originalAttrs.get(el);
    for (const attr of TRANSLATED_ATTRS) {
      if (attr === "title" && el.hasAttribute("data-i18n-title")) continue;
      if (!el.hasAttribute(attr)) continue;
      const val = el.getAttribute(attr) || "";
      const trimmed = val.trim();
      if (!trimmed) continue;
      let rec = map?.[attr];
      if (!rec || (rec.lastApplied !== undefined && val !== rec.lastApplied)) {
        // 仅当新值包含中文或未曾记录过时更新原始值，避免切语时非中文 title 覆写原始中文。
        if (!isTranslatableString(trimmed) || (rec && !/[\u4e00-\u9fff]/.test(trimmed))) continue;
        if (!map) {
          map = {};
          originalAttrs.set(el, map);
        }
        rec = { raw: val, lastApplied: val };
        map[attr] = rec;
      }
      const nextVal = currentLang === "zh-CN" ? rec.raw : translateString(rec.raw);
      rec.lastApplied = nextVal;
      if (val !== nextVal) el.setAttribute(attr, nextVal);
    }
  }

  function translateDocumentTitle() {
    if (typeof document === "undefined" || !document.title) return;
    if (originalDocTitle === null || (originalDocTitle.lastApplied !== document.title && isTranslatableString(document.title.trim()))) {
      originalDocTitle = { raw: document.title, lastApplied: document.title };
    }
    if (!originalDocTitle) return;
    const nextTitle = currentLang === "zh-CN" ? originalDocTitle.raw : translateString(originalDocTitle.raw);
    originalDocTitle.lastApplied = nextTitle;
    if (document.title !== nextTitle) document.title = nextTitle;
  }

  function translateRoot(root = document.body) {
    if (!root) return;
    isTranslating = true;
    try {
      if (root === document.body) translateDocumentTitle();
      if (root.nodeType === Node.TEXT_NODE) {
        translateTextNode(root);
        return;
      }
      if (root.nodeType === Node.ELEMENT_NODE) {
        translateElementAttrs(root);
      }
      if (root.querySelectorAll) {
        root.querySelectorAll("[data-i18n]").forEach(el => {
          const key = el.getAttribute("data-i18n");
          if (key) el.textContent = t(key, el.textContent);
        });
        root.querySelectorAll("[data-i18n-title]").forEach(el => {
          const key = el.getAttribute("data-i18n-title");
          if (key) el.title = t(key, el.title);
        });
        root.querySelectorAll("[placeholder], [title], [aria-label]").forEach(translateElementAttrs);
      }
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) translateTextNode(node);
    } finally {
      isTranslating = false;
    }
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
    if (document.getElementById("ui-global-controls")) return;
    let tools = document.querySelector(".header-tools");
    if (!tools) {
      const header = document.querySelector("body > header");
      if (!header) return;
      tools = document.createElement("div");
      tools.className = "header-tools standalone-header-tools";
      header.append(tools);
    }
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
    if (isTranslating || currentLang === "zh-CN" || pendingTranslate) return;
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
      new MutationObserver(scheduleTranslate).observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: TRANSLATED_ATTRS,
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  /**
   * 全站共享模态确认框：用于高危/不可逆操作，默认聚焦安全“取消”按钮，确认按钮按危险程度视觉区分。
   */
  function scadaConfirm(message, options = {}) {
    const title = options.title || "请确认操作";
    const confirmText = options.confirmText || "确认";
    const cancelText = options.cancelText || "取消";
    const danger = options.danger !== false;
    if (typeof document === "undefined" || !document.body || typeof document.createElement !== "function") {
      return Promise.resolve(false);
    }
    let dialog = document.getElementById("scada-global-confirm");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "scada-global-confirm";
      dialog.className = "scada-confirm-dialog";
      dialog.innerHTML =
        '<form method="dialog" class="scada-confirm-form">' +
        '<h3 class="scada-confirm-title"></h3>' +
        '<p class="scada-confirm-body"></p>' +
        '<div class="scada-confirm-actions">' +
        '<button type="button" class="scada-confirm-cancel"></button>' +
        '<button type="submit" class="scada-confirm-ok"></button>' +
        "</div></form>";
      document.body.append(dialog);
    }
    if (typeof dialog.showModal !== "function") {
      return Promise.resolve(false);
    }
    const titleEl = dialog.querySelector(".scada-confirm-title");
    const bodyEl = dialog.querySelector(".scada-confirm-body");
    const cancelBtn = dialog.querySelector(".scada-confirm-cancel");
    const okBtn = dialog.querySelector(".scada-confirm-ok");
    const tr = text => (currentLang === "zh-CN" ? String(text) : translateString(String(text)));
    titleEl.textContent = tr(title);
    bodyEl.textContent = tr(message);
    cancelBtn.textContent = tr(cancelText);
    okBtn.textContent = tr(confirmText);
    okBtn.className = "scada-confirm-ok " + (danger ? "danger" : "primary");

    return new Promise(resolve => {
      let settled = false;
      const done = value => {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        resolve(Boolean(value));
      };
      cancelBtn.onclick = () => done(false);
      dialog.oncancel = ev => {
        ev.preventDefault();
        done(false);
      };
      dialog.querySelector(".scada-confirm-form").onsubmit = ev => {
        ev.preventDefault();
        done(true);
      };
      dialog.showModal();
      cancelBtn.focus();
    });
  }

  window.scadaConfirm = scadaConfirm;
  window.scadaI18n = {
    t,
    translateText: translateString,
    translateRoot,
    confirm: scadaConfirm,
    themes: THEMES.slice(),
    langs: LANGS.slice(),
    isDarkTheme,
    getLang: () => currentLang,
    setLang: lang => applyLang(lang, true),
    getTheme: () => currentTheme,
    setTheme: theme => applyTheme(theme, true),
    dict: I18N_DICT,
  };
})();
