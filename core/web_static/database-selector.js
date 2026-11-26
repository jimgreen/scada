(() => {
  'use strict';
  async function api(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || '数据库操作失败');
    return data;
  }
  async function boot() {
    const current = await (window.scadaDatabaseReady || api('/api/database'));
    if (!current?.database) return;
    const brand = document.querySelector('.brand > strong, .workbench-brand > strong');
    const host = brand || document.querySelector('.header-tools');
    if (!host) return;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'database-select-button';
    button.textContent = '选择数据库'; button.id = 'select-database';
    const entry = document.createElement('span'); entry.id = 'database-entry';
    const databasePath = document.createElement('code');
    const path = (current.relative_database || current.database).replace(/\\/g, '/');
    const separator = path.lastIndexOf('/') + 1;
    const directory = document.createElement('span'); directory.className = 'database-directory';
    directory.textContent = path.slice(0, separator);
    const filename = document.createElement('span'); filename.className = 'database-filename';
    filename.textContent = path.slice(separator);
    databasePath.setAttribute('aria-label', '当前数据库路径');
    databasePath.title = path;
    databasePath.append(directory, filename);
    entry.append(button, databasePath);
    if (brand) { brand.classList.add('database-brand'); brand.insertBefore(entry, brand.querySelector('span')); }
    else host.append(entry);
    const dialog = document.createElement('dialog');
    dialog.className = 'database-selector'; dialog.setAttribute('aria-labelledby', 'database-selector-title');
    dialog.innerHTML = '<form id="database-selector-form">' +
      '<h2 id="database-selector-title"></h2>' +
      '<code class="database-current" aria-label="当前数据库"></code>' +
      '<div class="database-context-grid" hidden>' +
      '<label class="database-context-field"><span>运行态</span><select id="database-runtime" aria-label="运行态名称"></select></label>' +
      '<label class="database-context-field"><span>场景</span><select id="database-scene" aria-label="场景名称"></select></label>' +
      '</div>' +
      '<div class="database-chooser"><div class="database-path-row">' +
      '<input id="database-input" aria-label="数据库相对路径" autocomplete="off" required spellcheck="false" role="combobox" aria-expanded="false" aria-controls="database-tree" aria-haspopup="tree">' +
      '<button type="button" id="database-browse" aria-label="展开数据库目录">▾</button></div>' +
      '<div class="database-popup" hidden><div class="database-tree-toolbar">' +
      '<button type="button" id="database-parent">上一级</button><button type="button" id="database-root">工作区根目录</button>' +
      '<button type="button" id="database-mkdir">新建目录</button><button type="button" id="database-new">新建数据库</button></div>' +
      '<p class="database-folder"></p><p class="database-error database-folder-error" role="alert" hidden></p>' +
      '<ul id="database-tree" role="tree" aria-label="数据库目录"></ul>' +
      '<div class="database-create" hidden><p class="database-create-directory"></p>' +
      '<label for="database-new-name">名称</label><div class="database-path-row"><input id="database-new-name" autocomplete="off" spellcheck="false">' +
      '<button type="button" id="database-create-confirm">创建</button><button type="button" id="database-create-cancel">取消</button></div>' +
      '<p class="database-create-error" role="alert" hidden></p></div></div></div>' +
      '<p class="database-validation" role="status"></p><p class="database-error" role="alert" hidden></p>' +
      '<div class="database-actions"><button type="button" id="database-cancel">取消</button>' +
      '<button type="submit" id="database-apply" disabled>使用此数据库</button></div></form>';
    document.body.append(dialog);
    const $ = selector => dialog.querySelector(selector);
    $('#database-selector-title').textContent = '启动数据库 · scdfe_' + current.role;
    const form = $('#database-selector-form'), input = $('#database-input'), apply = $('#database-apply');
    const contextGrid = $('.database-context-grid'), runtimeSelect = $('#database-runtime'), sceneSelect = $('#database-scene');
    const error = form.querySelector(':scope > .database-error'), folderError = $('.database-folder-error');
    const popup = $('.database-popup'), tree = $('#database-tree'), status = $('.database-validation');
    const createPanel = $('.database-create'), createError = $('.database-create-error');
    const defaultRoleFile = current.role_db_filename || ('scdfe_' + (current.role === 't' ? 'c' : current.role) + '.db');
    let validatedPath = '', validationSequence = 0, treeSequence = 0, contextSequence = 0, timer;
    const databaseFolder = current.directory || '.';
    const remembersFolders = typeof current.browse_directory === 'string';
    let selectedFolder = current.browse_directory || databaseFolder, parent = null, createKind = '';
    let folderSave = Promise.resolve();
    let folderReadError = '', folderSaveError = '';
    $('.database-current').textContent = current.relative_database || current.database;
    $('.database-current').title = '当前数据库';
    input.title = '相对于工作区根目录，使用 / 分隔';
    input.placeholder = 'realtime/qinling/sqldb/scada/' + defaultRoleFile;

    function fillSelect(select, values, selected, emptyLabel) {
      select.replaceChildren();
      if (!values.length) {
        const opt = document.createElement('option');
        opt.value = ''; opt.textContent = emptyLabel;
        select.append(opt); select.disabled = true;
        return '';
      }
      select.disabled = false;
      for (const value of values) {
        const opt = document.createElement('option');
        opt.value = value; opt.textContent = value;
        select.append(opt);
      }
      const chosen = values.includes(selected) ? selected : values[0];
      select.value = chosen;
      return chosen;
    }

    function applyContextSelection() {
      const rt = runtimeSelect.value, sc = sceneSelect.value;
      if (!rt || !sc) return;
      const folder = rt + '/' + sc + '/sqldb/scada';
      selectFolder(folder);
      input.value = folder + '/' + defaultRoleFile;
      setPopup(false);
      validate();
    }

    async function refreshContexts(preferredRuntime, preferredScene, {applyPath = false} = {}) {
      const seq = ++contextSequence;
      try {
        const query = preferredRuntime ? '?runtime=' + encodeURIComponent(preferredRuntime) : '';
        const ctx = await api('/api/database/contexts' + query);
        if (seq !== contextSequence) return;
        const runtimes = Array.isArray(ctx.runtimes) ? ctx.runtimes : [];
        contextGrid.hidden = runtimes.length === 0;
        if (!runtimes.length) return;
        const rt = fillSelect(runtimeSelect, runtimes, preferredRuntime || current.runtime_name || ctx.runtime_name, '无运行态');
        let scenes = Array.isArray(ctx.scenes) ? ctx.scenes : [];
        if (rt && rt !== ctx.runtime_name) {
          const next = await api('/api/database/contexts?runtime=' + encodeURIComponent(rt));
          if (seq !== contextSequence) return;
          scenes = Array.isArray(next.scenes) ? next.scenes : [];
        }
        fillSelect(sceneSelect, scenes, preferredScene || current.scene_name, '无场景');
        if (applyPath && runtimeSelect.value && sceneSelect.value) applyContextSelection();
      } catch (_) {
        contextGrid.hidden = true;
      }
    }

    runtimeSelect.addEventListener('change', () => {
      refreshContexts(runtimeSelect.value, '', {applyPath: true});
    });
    sceneSelect.addEventListener('change', applyContextSelection);
    function showError(message) { error.textContent = message; error.hidden = !message; }
    function showFolderError() {
      folderError.textContent = folderReadError || folderSaveError;
      folderError.hidden = !folderError.textContent;
    }
    function rememberFolder(directory) {
      if (!remembersFolders) return;
      folderSave = folderSave.then(async () => {
        try {
          const path = await directory;
          if (!path) return;
          const result = await api('/api/database/directory', {
            method: 'PUT', keepalive: true, headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({directory: path})
          });
          if (selectedFolder === path && typeof result.browse_directory === 'string' && result.browse_directory !== path) {
            selectFolder(result.browse_directory);
          }
          folderSaveError = '';
        } catch (err) { folderSaveError = '保存目录失败：' + err.message; }
        showFolderError();
      });
    }
    function positionPopup() {
      if (popup.hidden || !dialog.open) return;
      const anchor = input.parentElement.getBoundingClientRect();
      const gap = 7, edge = 8;
      const below = window.innerHeight - anchor.bottom - gap - edge;
      const above = anchor.top - gap - edge;
      const openBelow = below >= 280 || below >= above;
      const width = Math.min(anchor.width, window.innerWidth - edge * 2);
      popup.style.width = Math.max(0, width) + 'px';
      popup.style.left = Math.max(edge, Math.min(anchor.left, window.innerWidth - width - edge)) + 'px';
      popup.style.maxHeight = Math.max(0, Math.min(320, openBelow ? below : above)) + 'px';
      popup.style.top = (openBelow ? anchor.bottom + gap : anchor.top - gap - popup.offsetHeight) + 'px';
    }
    function setPopup(open) {
      popup.hidden = !open; input.setAttribute('aria-expanded', String(open));
      $('#database-browse').setAttribute('aria-label', open ? '收起数据库目录' : '展开数据库目录');
      if (open) positionPopup();
    }
    const popupObserver = new ResizeObserver(positionPopup);
    popupObserver.observe(dialog); popupObserver.observe(popup);
    window.addEventListener('resize', positionPopup);
    window.addEventListener('scroll', positionPopup, true);
    dialog.addEventListener('cancel', event => {
      if (!popup.hidden) { event.preventDefault(); setPopup(false); input.focus({preventScroll:true}); }
    });
    async function validate() {
      const sequence = ++validationSequence, path = input.value.trim();
      validatedPath = ''; apply.disabled = true; showError('');
      status.textContent = path ? '正在校验数据库…' : '请选择数据库';
      if (!path) return;
      try {
        const result = await api('/api/database/validate?path=' + encodeURIComponent(path));
        if (sequence !== validationSequence || path !== input.value.trim()) return;
        if (!result.valid) { status.textContent = ''; showError(result.message); return; }
        validatedPath = path; apply.disabled = false;
        const c = result.counts;
        status.textContent = current.role === 'd'
          ? '校验通过 · 设备 ' + c.device + ' · 属性 ' + c.device_measurement + ' · 映射 ' + c.scada_point_mapping
          : '校验通过 · RTU ' + c.rtu + ' · 遥测 ' + c.yc + ' · 遥信 ' + c.yx + ' · 遥控 ' + c.yk + ' · 遥调 ' + c.yt;
      } catch (err) {
        if (sequence === validationSequence) { status.textContent = ''; showError(err.message); }
      }
    }
    function selectFolder(path) {
      selectedFolder = path;
      const separator = path.lastIndexOf('/');
      parent = path === '.' ? null : separator < 0 ? '.' : path.slice(0, separator);
      $('#database-parent').disabled = parent === null;
      $('.database-folder').textContent = '选中目录：' + path;
      for (const row of tree.querySelectorAll('button[data-directory]')) row.classList.toggle('selected', row.dataset.directory === path);
    }
    const SVG_FOLDER_CLOSED = '<span class="tree-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v6a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12V4.5Z"/></svg></span>';
    const SVG_FOLDER_OPEN = '<span class="tree-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v1H3.2L2 12V4.5Z"/><path d="M2 12l1.2-5h11.3l-1.2 5A1.5 1.5 0 0 1 11.8 13H3.5A1.5 1.5 0 0 1 2 12Z"/></svg></span>';
    const SVG_DB_FILE = '<span class="tree-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="8" cy="4" rx="5" ry="2"/><path d="M3 4v8c0 1.1 2.24 2 5 2s5-.9 5-2V4"/><path d="M3 8c0 1.1 2.24 2 5 2s5-.9 5-2"/></svg></span>';
    function setTreeRowLabel(row, iconHtml, name) {
      row.innerHTML = iconHtml;
      row.append(document.createTextNode(name));
    }
    function renderEntries(list, entries, directory) {
      list.replaceChildren();
      if (!entries.length) {
        const empty = document.createElement('li'); empty.textContent = '此目录没有数据库或子目录';
        empty.className = 'database-tree-empty'; empty.setAttribute('role', 'none'); list.append(empty);
      }
      for (const entry of entries) {
        const li = document.createElement('li'); li.setAttribute('role', 'none');
        const row = document.createElement('button'); row.type = 'button'; row.setAttribute('role', 'treeitem');
        setTreeRowLabel(row, entry.directory ? SVG_FOLDER_CLOSED : SVG_DB_FILE, entry.name);
        row.title = entry.path; li.append(row); list.append(li);
        if (entry.directory) {
          row.dataset.directory = entry.path; row.setAttribute('aria-expanded', 'false');
          const children = document.createElement('ul'); children.setAttribute('role', 'group'); children.hidden = true;
          li.append(children); let loaded = false;
          row.addEventListener('click', async () => {
            treeSequence++;
            selectFolder(entry.path); createPanel.hidden = true;
            folderReadError = ''; showFolderError();
            rememberFolder(entry.path);
            const open = row.getAttribute('aria-expanded') !== 'true';
            row.setAttribute('aria-expanded', String(open)); children.hidden = !open;
            setTreeRowLabel(row, open ? SVG_FOLDER_OPEN : SVG_FOLDER_CLOSED, entry.name);
            if (open && !loaded) {
              try {
                const data = await api('/api/database/tree?directory=' + encodeURIComponent(entry.path));
                renderEntries(children, data.entries, data.directory); loaded = true;
              } catch (err) {
                if (row.isConnected && selectedFolder === entry.path) {
                  folderReadError = err.message; showFolderError();
                }
              }
            }
          });
        } else {
          row.addEventListener('click', () => {
            treeSequence++;
            selectFolder(directory); rememberFolder(directory);
            input.value = entry.path; setPopup(false); validate();
          });
        }
      }
    }
    async function loadTree(path, {restore = false} = {}) {
      const sequence = ++treeSequence;
      const paths = restore ? new Set([path, databaseFolder, '.']) : [path];
      for (const candidate of paths) {
        try {
          const data = await api('/api/database/tree?directory=' + encodeURIComponent(candidate));
          if (sequence !== treeSequence) return;
          renderEntries(tree, data.entries, data.directory); selectFolder(data.directory);
          folderReadError = ''; showFolderError(); createPanel.hidden = true;
          return data.directory;
        } catch (err) {
          if (sequence !== treeSequence) return;
          folderReadError = err.message;
        }
      }
      showFolderError();
    }
    async function browse() {
      const open = popup.hidden; setPopup(open);
      if (open) await loadTree(selectedFolder, {restore: true});
    }
    $('#database-browse').addEventListener('click', browse);
    input.addEventListener('click', () => { if (popup.hidden) browse(); });
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); if (popup.hidden) browse(); }
      if (event.key === 'Escape' && !popup.hidden) { event.preventDefault(); event.stopPropagation(); setPopup(false); }
    });
    input.addEventListener('input', () => {
      clearTimeout(timer); validationSequence++; validatedPath = ''; apply.disabled = true;
      status.textContent = ''; showError(''); timer = setTimeout(validate, 300);
    });
    $('#database-parent').addEventListener('click', () => { if (parent !== null) rememberFolder(loadTree(parent)); });
    $('#database-root').addEventListener('click', () => rememberFolder(loadTree('.')));
    for (const [id, kind] of [['database-mkdir', 'mkdir'], ['database-new', 'create']]) {
      $('#' + id).addEventListener('click', () => {
        createKind = kind; createPanel.hidden = false; createError.hidden = true;
        $('.database-create-directory').textContent = '创建位置：' + selectedFolder;
        $('label[for="database-new-name"]').textContent = kind === 'mkdir' ? '新目录名称' : '新数据库文件名';
        $('#database-new-name').value = kind === 'mkdir' ? '' : defaultRoleFile;
        $('#database-new-name').focus(); createPanel.scrollIntoView({block: 'nearest'});
      });
    }
    $('#database-create-cancel').addEventListener('click', () => { createPanel.hidden = true; });
    async function createEntry() {
      const confirm = $('#database-create-confirm'); confirm.disabled = true; createError.hidden = true;
      const kind = createKind, destination = selectedFolder;
      try {
        const creation = api('/api/database/' + kind, {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({directory: destination, name: $('#database-new-name').value})});
        if (kind === 'mkdir') {
          const navigation = creation.then(data => loadTree(data.path));
          rememberFolder(navigation.catch(() => null));
          await navigation;
        } else {
          const data = await creation;
          input.value = data.path; setPopup(false); await validate();
        }
      } catch (err) { createError.textContent = err.message; createError.hidden = false; }
      finally { confirm.disabled = false; }
    }
    $('#database-create-confirm').addEventListener('click', createEntry);
    $('#database-new-name').addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); if (!$('#database-create-confirm').disabled) createEntry(); }
    });
    dialog.addEventListener('click', event => { if (!event.target.closest('.database-chooser')) setPopup(false); });
    button.addEventListener('click', () => {
      clearTimeout(timer);
      input.value = current.relative_database || current.database;
      setPopup(false); dialog.showModal();
      refreshContexts(current.runtime_name, current.scene_name, {applyPath: false});
      validate();
    });
    $('#database-cancel').addEventListener('click', () => dialog.close());
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (apply.disabled || validatedPath !== input.value.trim()) return;
      const database = validatedPath;
      apply.disabled = true; form.inert = true; showError('');
      try {
        await folderSave;
        await api('/api/database', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({database, expected_database: current.database})});
        location.reload();
      } catch (err) { validatedPath = ''; showError(err.message); }
      finally { form.inert = false; }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => boot().catch(console.error));
  else boot().catch(console.error);
})();
