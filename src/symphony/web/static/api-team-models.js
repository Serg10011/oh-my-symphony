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
  function settings(el, kind, data, save) {
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
    return el('section', { class: 'card-panel settings-card', 'aria-label': 'Команда по умолчанию' }, [
      el('h3', {}, 'Команда по умолчанию · ' + kind),
      el('p', {}, 'Эта пара подставляется в новые назначения. В карточке можно выбрать другую до подтверждения.'),
      p.row, button, hint,
    ]);
  }
  function confirmedLabel(kind, profiles) {
    return kind + ' — ' + [profiles.lead?.model, profiles.worker?.model].filter(Boolean).map(name).join(' + ');
  }
  root.SymphonyApiModels = { name, decorate, invalidate, kindLabel, confirmedLabel, picker, settings };
})(globalThis);
