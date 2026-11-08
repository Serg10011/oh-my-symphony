/* Names come from the saved assignment; choices are limited by the board catalog. */
(function (root) {
  'use strict';
  const names = { 'deepseek-flash': 'DeepSeek Flash 4.1', 'gpt-6.1-sol': 'Sol 6.1', 'gpt-6-luna': 'Luna' };
  const name = id => names[id] || id;
  const cache = new Map();
  const invalidate = kind => { if (kind) cache.delete(kind); else cache.clear(); };
  function label(kind, pair, models) {
    if (!pair || !models?.[pair.lead_profile]) return 'API-команда';
    const lead = name(models[pair.lead_profile]);
    return pair.worker_profile && models[pair.worker_profile]
      ? `${kind} — ${lead} + ${name(models[pair.worker_profile])}` : `${kind} — ${lead}`;
  }
  async function decorate(board, get) {
    board.teamDirectory = {};
    await Promise.all((board.board.api_team_model_kinds || []).map(async kind => {
      const ids = board.issues.filter(i => i.agent_kind === kind).map(i => i.identifier).sort();
      const key = JSON.stringify(ids), old = cache.get(kind);
      if (old && old.key === key && Date.now() < old.until) {
        board.teamDirectory[kind] = await old.promise;
        return;
      }
      const promise = (async () => {
        let result;
        for (let i = 0; i < Math.max(ids.length, 1); i += 40) {
          const data = await get(kind, ids.slice(i, i + 40));
          if (!data.models || !data.pairs || !data.default) return null;
          if (!result) result = { ...data, assignments: {} };
          Object.assign(result.assignments, data.assignments);
        }
        return result;
      })().catch(() => null);
      cache.set(kind, { key, until: Date.now() + 30000, promise });
      board.teamDirectory[kind] = await promise;
    }));
    return board;
  }
  function kindLabel(board, kind, identifier) {
    if (!board?.board.api_team_kinds?.includes(kind)) return kind;
    const info = board.teamDirectory?.[kind];
    const assigned = identifier && info?.assignments[identifier];
    if (identifier && info && Object.hasOwn(info.assignments, identifier) && !assigned)
      return kind + ' — состав не удалось прочитать';
    const text = label(kind, assigned || info?.default, info?.models);
    return identifier && info && !assigned ? text + ' (по умолчанию)' : text;
  }
  function picker(el, data, current, onChange) {
    const lead = el('select', { class: 'select', 'aria-label': 'Ведущий' });
    const worker = el('select', { class: 'select', 'aria-label': 'Исполнитель' });
    const leaders = [...new Set(data.pairs.map(p => p.lead_profile))];
    const option = (id, selected) => el('option', { value: id || '', selected }, id ? name(data.models[id]) : 'Без исполнителя');
    leaders.forEach(id => lead.appendChild(option(id, id === current.lead_profile)));
    function workers(preferred) {
      const ids = data.pairs.filter(p => p.lead_profile === lead.value).map(p => p.worker_profile);
      const selected = ids.includes(preferred) ? preferred : ids[0];
      worker.replaceChildren(...ids.map(id => option(id, id === selected)));
    }
    workers(current.worker_profile);
    const value = () => ({ lead_profile: lead.value, worker_profile: worker.value || null });
    lead.addEventListener('change', () => { workers(worker.value || null); onChange(value()); });
    worker.addEventListener('change', () => onChange(value()));
    const row = el('div', { class: 'api-team-actions' }, [
      el('label', {}, ['Ведущий', lead]), el('label', {}, ['Исполнитель', worker]),
    ]);
    return { row, value, disable(flag) { lead.disabled = worker.disabled = flag; } };
  }
  const modeName = mode => mode === 'productivity' ? 'Продуктивность' : mode === 'economy' ? 'Экономия' : 'По конфигурации доски';
  const pathName = path => path === '.' ? 'Весь репозиторий в пределах правил доступа' : path;
  function workSettings(el, kind, current, request) {
    const modes = current.mode === null ? [null, 'economy', 'productivity'] : ['economy', 'productivity'];
    const mode = el('select', { class: 'select', 'aria-label': 'Режим работы' }, modes.map(value =>
      el('option', { value: value || '', selected: current.mode === value }, modeName(value))));
    const paths = el('textarea', { class: 'input', rows: 4, 'aria-label': 'Доступные файлы и папки' });
    paths.value = current.paths.join('\n');
    const saved = el('p', { class: 'form-hint' });
    const details = el('dl', { class: 'api-team-assignment' });
    const notice = el('p', { 'aria-live': 'polite', class: 'form-hint' });
    const check = el('button', { type: 'button', class: 'btn btn-ghost' }, 'Проверить изменения');
    const apply = el('button', { type: 'button', class: 'btn btn-primary' }, 'Сохранить настройки');
    let pending = null;
    function showCurrent() {
      saved.textContent = 'Для новых карточек сейчас выбран режим «' + modeName(current.mode) + '». Доступ: '
        + current.paths.map(pathName).join(', ') + '.';
    }
    function reset() {
      pending = null;
      apply.hidden = true;
      details.replaceChildren();
      notice.textContent = '';
    }
    function row(label, value) {
      details.append(el('dt', {}, label), el('dd', {}, value));
    }
    async function edit(action) {
      const choice = action === 'save' ? pending : {
        mode: mode.value || null, paths: paths.value.split(/\r?\n/).filter(Boolean),
      };
      reset();
      [check, apply, mode, paths].forEach(field => { field.disabled = true; });
      try {
        const result = await request(kind, action, choice);
        if (action === 'save') {
          current = result;
          mode.value = current.mode || '';
          if (current.mode !== null) mode.querySelector('option[value=""]')?.remove();
          paths.value = current.paths.join('\n');
          showCurrent();
          notice.textContent = result.permission_status === 'changed'
            ? 'Настройки сохранены. Подтверди новые условия в разделе «Разрешение на работу доски».'
            : 'Настройки сохранены для новых карточек.';
        } else if (result.changed) {
          const { current: before, proposed: after } = result;
          const added = after.paths.filter(path => !before.paths.includes(path));
          const removed = before.paths.filter(path => !after.paths.includes(path));
          if (before.mode !== after.mode) row('Режим', modeName(before.mode) + ' → ' + modeName(after.mode));
          if (before.profiles.lead.effort !== after.profiles.lead.effort)
            row('Усилие ведущего', before.profiles.lead.effort + ' → ' + after.profiles.lead.effort);
          if (added.length) row('Добавятся', added.map(pathName).join(', '));
          if (removed.length) row('Уберутся', removed.map(pathName).join(', '));
          notice.textContent = result.permission_status === 'changed'
            ? 'Для платной передачи потребуется подтвердить новые условия.'
            : 'Изменения будут применены к новым карточкам. Платная передача требует отдельного разрешения.';
          pending = { mode: after.mode, paths: after.paths, expected: result.expected };
          apply.hidden = false;
        } else {
          notice.textContent = 'Настройки уже совпадают.';
        }
      } catch (error) {
        notice.textContent = error.message || 'Не удалось изменить настройки доски.';
      } finally {
        [check, apply, mode, paths].forEach(field => { field.disabled = false; });
      }
    }
    mode.addEventListener('change', reset);
    paths.addEventListener('input', reset);
    check.addEventListener('click', () => edit('preview'));
    apply.addEventListener('click', () => { if (pending) edit('save'); });
    showCurrent();
    reset();
    return el('section', { 'aria-label': 'Режим и доступ к файлам' }, [
      el('h4', {}, 'Режим и доступ к файлам'),
      saved,
      el('label', { class: 'form-group' }, [el('span', { class: 'form-label' }, 'Режим работы'), mode]),
      el('label', { class: 'form-group' }, [el('span', { class: 'form-label' }, 'Доступные файлы и папки'), paths]),
      el('p', { class: 'form-hint' }, 'Укажи по одному пути от корня репозитория на строку. Точка означает весь репозиторий в пределах правил доступа.'),
      details, notice, el('div', { class: 'api-team-actions' }, [check, apply]),
    ]);
  }
  function settings(el, kind, data, save, configure) {
    const hint = el('p', { role: 'status', class: 'form-hint' });
    const button = el('button', { type: 'button', class: 'btn btn-primary' }, 'Сохранить команду доски');
    let base = data.default;
    const same = (a,b) => a.lead_profile === b.lead_profile && a.worker_profile === b.worker_profile;
    const p = picker(el, data, base, v => { button.disabled = same(v,base); hint.textContent = ''; });
    button.disabled = true;
    button.addEventListener('click', async () => {
      const chosen = p.value(); button.disabled = true; p.disable(true);
      try {
        const saved = await save(kind, chosen); data.default = base = saved.default; invalidate(kind);
        hint.textContent = 'Сохранено для новых назначений. Подтверждённые карточки сохранили свою команду.';
      } catch (e) { hint.textContent = e.message || 'Не удалось сохранить команду'; }
      finally { p.disable(false); button.disabled = same(p.value(),base); }
    });
    const element = el('section', { class: 'card-panel settings-card', 'aria-label': 'Команда по умолчанию' }, [
      el('h3', {}, 'Команда по умолчанию · ' + kind),
      el('p', {}, 'Эта пара подставляется в новые назначения. В карточке можно выбрать другую до подтверждения.'),
      p.row, button, hint,
    ]);
    if (data.work_settings && configure) element.appendChild(workSettings(el, kind, data.work_settings, configure));
    return element;
  }
  function confirmedLabel(kind, profiles) {
    return kind + ' — ' + [profiles.lead?.model, profiles.worker?.model].filter(Boolean).map(name).join(' + ');
  }
  root.SymphonyApiModels = { name, decorate, invalidate, kindLabel, confirmedLabel, picker, settings };
})(globalThis);
