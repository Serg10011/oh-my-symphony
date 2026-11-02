'use strict';
(() => {
  const projects = document.getElementById('projects');
  const error = document.getElementById('error');
  const updated = document.getElementById('updated');
  const refresh = document.getElementById('refresh');
  const expanded = new Map();
  let loading = false;
  let savedScroll = 0;
  let failed = false;
  const states = {running: 'Работает', unavailable: 'Нет связи', error: 'Ошибка данных', mismatch: 'Другой проект'};
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function remember() {
    for (const node of projects.querySelectorAll('details[data-key]')) expanded.set(node.dataset.key, node.open);
    if (!failed) savedScroll = window.scrollY;
    const focused = document.activeElement;
    return focused?.tagName === 'SUMMARY' ? focused.parentElement.dataset.key : null;
  }
  function details(key, initial) {
    const node = element('details');
    node.dataset.key = key;
    node.open = expanded.has(key) ? expanded.get(key) : initial;
    return node;
  }
  function card(row) {
    const line = element('div', undefined, 'row');
    if (row.kind === 'issue') line.append(element('span', row.identifier, 'identifier'));
    line.append(element('span', row.title, 'title'));
    if (row.kind === 'issue') line.append(element('span', row.state_label, 'state ' + row.bucket));
    return line;
  }
  function tree(project, value) {
    if (value.rows.length === 1) {
      const block = element('div', undefined, 'task');
      block.append(card(value.rows[0]));
      return block;
    }
    const block = details(project.id + '/tree/' + value.key, !value.completed);
    block.className = 'task';
    const summary = element('summary');
    summary.append(card(value.rows[0]));
    block.append(summary);
    for (const row of value.rows.slice(1)) {
      const line = card(row);
      line.classList.add('card-row');
      line.style.marginInlineStart = Math.min(row.depth, 5) * 16 + 'px';
      block.append(line);
    }
    return block;
  }
  function projectView(project) {
    const block = element('article', undefined, 'project');
    block.dataset.project = project.id;
    const head = element('div', undefined, 'project-head');
    const title = element('div', undefined, 'project-title');
    title.append(element('h2', project.name.toUpperCase()));
    title.append(element('span', states[project.service_state] || 'Ошибка данных', 'status ' + project.service_state));
    head.append(title);
    if (project.board_url) {
      const link = element('a', 'Открыть доску', 'button');
      link.href = project.board_url;
      head.append(link);
    }
    block.append(head);
    const metrics = element('div', undefined, 'metrics');
    for (const [key, label] of [['working', 'В работе'], ['review', 'Ждёт тебя'], ['help', 'Нужна помощь']]) {
      const metric = element('div', undefined, 'metric');
      metric.dataset.metric = key;
      metric.append(element('strong', project.summary ? project.summary[key] : '—'));
      metric.append(element('span', label));
      metrics.append(metric);
    }
    block.append(metrics);
    if (project.message) block.append(element('p', project.message, 'project-message'));
    if (!project.summary) return block;
    if (project.runtime?.dispatch_held) block.append(element('p', 'Выдача задач приостановлена — подробности на доске', 'warning'));
    for (const text of project.warnings) block.append(element('p', text, 'warning'));
    if (project.summary.other) block.append(element('p', 'В других состояниях: ' + project.summary.other, 'runtime'));
    const active = project.trees.filter(value => !value.completed);
    if (!active.length) block.append(element('p', 'Активных карточек нет', 'empty'));
    for (const value of active) block.append(tree(project, value));
    const done = project.trees.filter(value => value.completed);
    if (done.length) {
      const history = details(project.id + '/history', false);
      history.className = 'history';
      const count = done.reduce((total, value) => total + value.rows.filter(row => row.kind === 'issue').length, 0);
      history.append(element('summary', 'Завершённые и архивные · ' + count));
      for (const value of done) history.append(tree(project, value));
      block.append(history);
    }
    return block;
  }
  async function load() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    try {
      const response = await fetch('/api/projects', {cache: 'no-store'});
      if (!response.ok) throw new Error('Снимок недоступен');
      const data = await response.json();
      const focused = remember();
      const fragment = document.createDocumentFragment();
      for (const project of data.projects) fragment.append(projectView(project));
      if (!data.projects.length) fragment.append(element('p', 'Проекты пока не найдены', 'empty'));
      projects.replaceChildren(fragment);
      error.hidden = true;
      updated.textContent = 'Обновлено ' + new Date(data.generated_at).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
      failed = false;
      if (focused) {
        const node = [...projects.querySelectorAll('details[data-key]')].find(item => item.dataset.key === focused);
        node?.querySelector('summary')?.focus({preventScroll: true});
      }
      window.scrollTo({top: savedScroll, behavior: 'instant'});
    } catch (_) {
      remember();
      projects.replaceChildren();
      error.textContent = 'Обзор недоступен. Проверь, что страница наблюдения работает; обновление повторится автоматически.';
      error.hidden = false;
      updated.textContent = 'Нет свежего снимка';
      failed = true;
    } finally {
      loading = false;
      refresh.disabled = false;
    }
  }
  refresh.addEventListener('click', load);
  setInterval(load, 10000);
  load();
})();
