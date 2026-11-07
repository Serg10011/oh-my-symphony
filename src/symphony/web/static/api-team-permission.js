/* Owner-controlled board spending permission. Provider credentials stay on the host. */
(function (root) {
  'use strict';
  const states = {
    not_configured: 'Каждая карточка требует отдельного подтверждения.',
    active: 'Работа новых карточек разрешена в показанных границах.',
    revoked: 'Работа API-команды доски остановлена.',
    changed: 'Условия изменились. Разрешение нужно проверить заново.',
    invalid: 'Разрешение не удалось прочитать. Нужна проверка оператором.',
    needs_confirmation: 'Проверь условия перед выдачей разрешения.',
  };
  function mount({ el, kind, request }) {
    let token = null, busy = false;
    const status = el('p', { role: 'status' }, 'Читаю разрешение…');
    const terms = el('dl', { class: 'api-team-assignment' });
    const history = el('div');
    const error = el('p', { role: 'alert', class: 'api-team-error' });
    const inspect = el('button', { type: 'button', class: 'btn btn-ghost' }, 'Проверить условия доски');
    const approve = el('button', { type: 'button', class: 'btn btn-primary' }, 'Разрешить работу новых карточек');
    const stop = el('button', { type: 'button', class: 'btn btn-ghost' }, 'Остановить API-команду доски');
    approve.hidden = true;
    const element = el('section', { class: 'card-panel settings-card', 'aria-label': 'Разрешение на работу доски' }, [
      el('h3', {}, 'Разрешение на работу доски'), status, error, terms,
      el('p', { class: 'form-hint' }, 'После разрешения архитектор сможет передавать новые карточки в платную работу. Создание черновика само по себе ничего не запускает. Разрешение действует до отзыва или изменения условий.'),
      el('div', { class: 'api-team-actions' }, [inspect, approve, stop]),
      el('p', { class: 'form-hint' }, 'Остановка блокирует новые обращения. Уже допущенный к отправке запрос может завершиться и оплачиваться. Прерванная задача автоматически не повторяется.'),
      el('details', {}, [el('summary', {}, 'История разрешений'), history]),
    ]);
    function render(data) {
      status.textContent = states[data.status] || 'Состояние разрешения неизвестно.';
      token = data.confirmation_token || null;
      approve.hidden = !token;
      stop.hidden = data.status === 'revoked';
      const t = data.terms;
      terms.replaceChildren();
      if (t) {
        const profile = p => p ? `${root.SymphonyApiModels?.name(p.model) || p.model} · усилие ${p.effort}` : 'Без исполнителя';
        const number = value => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'не указан';
        const rows = [
          ['Ведущий', profile(t.profiles?.lead)], ['Исполнитель', profile(t.profiles?.worker)],
          ['Режим', t.mode === 'productivity' ? 'Продуктивность' : t.mode === 'economy' ? 'Экономия' : 'По настройкам доски'],
          ['Ветка проекта', t.base_branch || 'HEAD'], ['Доступные файлы', (t.paths || []).join(', ')],
          ['Денежный потолок на одну задачу', t.budget ? `${t.budget.max_usd} USD` : 'Без отдельного денежного потолка'],
          ['Аварийный предел запросов на задачу', number(t.limits?.max_calls)],
          ['Все токены ответов на задачу', number(t.limits?.output_tokens_total)],
        ];
        rows.forEach(([label, value]) => terms.append(el('dt', {}, label), el('dd', {}, value)));
      }
      if (data.history) history.replaceChildren(...data.history.slice().reverse().map(item =>
        el('p', {}, `${item.at} — ${item.action === 'authorized' ? 'Разрешение выдано' : 'Разрешение отозвано'}`)));
    }
    async function act(action, payload = {}) {
      if (busy) return;
      busy = true; error.textContent = '';
      [inspect, approve, stop].forEach(b => { b.disabled = true; });
      if (action === 'authorize') status.textContent = 'Проверяю доступ к ключам. macOS может запросить разрешение.';
      try {
        render(await request(kind, action, payload));
        root.SymphonyApiModels?.invalidate(kind);
      } catch (e) {
        token = null; approve.hidden = true;
        error.textContent = e.message || 'Не удалось изменить разрешение.';
      } finally {
        busy = false;
        [inspect, approve, stop].forEach(b => { b.disabled = false; });
      }
    }
    inspect.addEventListener('click', () => act('prepare'));
    approve.addEventListener('click', () => { if (token) act('authorize', { confirmation_token: token }); });
    stop.addEventListener('click', () => act('revoke'));
    const reload = () => act('');
    reload();
    return { element, reload };
  }
  root.SymphonyApiPermission = { mount };
})(globalThis);
