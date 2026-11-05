/* Optional API-team controls for the existing issue drawer. No credentials in the browser. */
(function (root) {
  'use strict';
  const statuses = {
    unassigned: 'Назначение ещё не подготовлено', prepared: 'Ожидает твоего подтверждения',
    queued: 'Подтверждено, ожидает свободного места', running: 'Команда работает',
    result: 'Команда вернула результат; приёмка ещё не выполнена', stopped: 'Запуск остановлен',
  };
  const reasons = {
    operator_stopped: 'Остановлено оператором или после потери связи с доской.',
    board_execution_failed: 'Процесс команды прерван. Автоматического платного повтора нет.',
    api_assignment_changed: 'Задача или настройки изменились после подтверждения; платный запуск отменён.',
    execution_preflight_failed: 'Проверка изоляции не прошла; модели не запускались.',
  };
  const number = value => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'не указан';
  const model = value => value ? `${value.model} · усилие ${value.effort}` : 'не назначен';

  function mount({ el, identifier, request, isActive, onTransition, schedule = setInterval, unschedule = clearInterval }) {
    let token = null, busy = false, timer = null, lastStatus = null;
    let nextCheckRead = 0;
    const status = el('p', { 'aria-live': 'polite' }, 'Загружаю назначение…');
    const error = el('p', { class: 'api-team-error', role: 'alert' });
    const assignment = el('div');
    const report = el('pre', { class: 'api-team-text', tabindex: '0' });
    const reportDetails = el('details', {}, [el('summary', {}, 'Расходы и работа участников'), report]);
    const result = el('pre', { class: 'api-team-text', tabindex: '0' });
    const resultDetails = el('details', {}, [el('summary', {}, 'Ответ ведущего'), result]);
    const files = el('div', { class: 'api-team-files' });
    const buttons = [];
    const button = (label, fn) => {
      const node = el('button', { type: 'button', class: 'btn btn-ghost', onClick: fn }, label);
      buttons.push(node);
      return node;
    };
    const prepare = button('Посмотреть назначение', () => act('prepare'));
    const confirm = button('Подтвердить платный запуск', () => act('confirm', { confirmation_token: token }));
    const stop = button('Остановить', () => act('stop'));
    const refresh = button('Обновить', () => load());
    const review = button('Посмотреть изменения файлов', () => loadReview(0));
    const cloudStatus = el('p', { role: 'status' });
    const workflowLink = el('a', { target: '_blank', rel: 'noopener noreferrer' }, 'Открыть попытку на GitHub');
    const cloud = el('div', { role: 'group', 'aria-label': 'Проверка файлов на Linux' },
      [el('strong', {}, 'Проверка файлов на Linux'), cloudStatus, workflowLink]);
    cloud.hidden = true;
    [prepare, confirm, stop, review, reportDetails, resultDetails].forEach(node => { node.hidden = true; });
    const actions = el('div', { class: 'api-team-actions' }, [prepare, confirm, stop, refresh, review]);
    const section = el('section', { class: 'api-team-panel', 'aria-label': 'API-команда' }, [
      el('div', { class: 'section-heading' }, 'API-команда'), status, error, assignment, actions,
      el('p', { class: 'form-hint' }, 'Сначала проверь назначение. Только отдельное подтверждение ставит платную работу в очередь. После остановки автоматического повтора нет.'),
      el('p', { class: 'form-hint' }, 'После проверки файлов решение о приёмке принимаешь ты.'),
      cloud, resultDetails, reportDetails, files,
    ]);
    function active() { return isActive() && section.isConnected; }
    function setText(node, text) {
      // Retain scroll and expanded details while the report updates.
      if (node.textContent !== text) {
        const top = node.scrollTop, left = node.scrollLeft;
        node.textContent = text;
        node.scrollTop = top;
        node.scrollLeft = left;
      }
    }
    function render(data) {
      status.textContent = statuses[data.status] || 'Неизвестное состояние команды';
      if (data.stop_requested && data.status === 'running') status.textContent += '. Остановка запрошена; текущий ответ ещё может оплачиваться.';
      if (data.reason) status.textContent += ' ' + (reasons[data.reason] || 'Причина остановки указана в отчёте.');
      if (data.status !== 'prepared') token = null;
      prepare.hidden = !['unassigned', 'prepared'].includes(data.status);
      confirm.hidden = data.status !== 'prepared' || !token;
      stop.hidden = !['prepared', 'queued', 'running'].includes(data.status) || data.stop_requested;
      review.hidden = !data.session?.execution || !['result', 'stopped'].includes(data.status);
      const check = data.cloud_check, receipt = check?.phase ? null : check?.receipt;
      cloud.hidden = !check;
      workflowLink.hidden = !receipt;
      nextCheckRead = check?.status === 'ready' ? Date.now() + (receipt ? 60000 : 20000) : 0;
      if (receipt) {
        const outcome = receipt.status === 'passed' ? 'прошла' : 'завершилась с ошибкой';
        const base = receipt.comparison === 'success' ? 'прошла' : 'завершилась с ошибкой';
        setText(cloudStatus, `Сохранённая попытка №${receipt.attempt}. Проверка коммита ${check.commit.slice(0, 12)} ${outcome}.` +
          ` Контрольная версия ${base}.`);
        workflowLink.href = `https://github.com/Serg10011/oh-my-symphony/actions/runs/${receipt.run}/attempts/${receipt.attempt}`;
      } else if (check) {
        const phases = {
          running: 'GitHub выполняет проверку.',
          waiting: 'Ждём ответ GitHub. Конвейер повторит чтение автоматически.',
          interrupted: 'Проверка прервана. Конвейер увидит следующую попытку автоматически.',
          unverified: 'GitHub не подтвердил проверку этих файлов. Конвейер ожидает другую попытку.',
        };
        setText(cloudStatus, check.status === 'ready'
          ? `Версия ${check.commit.slice(0, 12)}. ${phases[check.phase] || 'Когда будет готов прогон GitHub, результат появится здесь автоматически.'}`
          : 'Карточка, файлы, база или настройки изменились. Прежняя проверка утратила силу.');
        workflowLink.removeAttribute('href');
      }
      reportDetails.hidden = !data.report_text;
      resultDetails.hidden = !data.session?.result;
      setText(report, data.report_text || '');
      setText(result, data.session?.result || '');
      if (data.profiles) {
        const limits = data.limits || {}, project = data.project || {};
        const rows = [
          ['Ведущий', model(data.profiles.lead)], ['Исполнитель', model(data.profiles.worker)],
          ['Денежный потолок', data.budget ? `${data.budget.max_usd} USD` : 'без отдельного денежного потолка'],
          ['Аварийный предел запросов', number(limits.max_calls)],
          ['Токенов на ответ / на все ответы', `${number(limits.output_tokens_per_call)} / ${number(limits.output_tokens_total)}`],
          ['Время на запуск', `${number(data.process_timeout_seconds / 60)} мин`],
          ['Версия проекта', project.commit || 'не указана'], ['Доступные файлы', (project.paths || []).join(', ')],
        ];
        const key = JSON.stringify(rows) + data.task;
        if (assignment.dataset.content !== key) {
          assignment.dataset.content = key;
          const list = el('dl', { class: 'api-team-assignment' });
          rows.forEach(([label, value]) => list.append(el('dt', {}, label), el('dd', {}, value)));
          const task = el('details', {}, [el('summary', {}, 'Задача, которую подтверждаешь'),
            el('pre', { class: 'api-team-text', tabindex: '0' }, data.task || '')]);
          const access = el('details', {}, [el('summary', {}, 'Среда выполнения'),
            el('p', {}, 'Команды работают в отдельной копии, без сети. Системные компоненты доступны только для чтения:'),
            el('pre', { class: 'api-team-text' }, (data.execution?.read_roots || []).join('\n'))]);
          assignment.replaceChildren(list, task, access);
        }
      }
      if (lastStatus !== null && lastStatus !== data.status) onTransition?.();
      lastStatus = data.status;
    }
    async function operation(fn) {
      if (busy || !active()) return;
      busy = true;
      buttons.forEach(node => { node.disabled = true; });
      error.textContent = '';
      try { await fn(); }
      catch (err) { if (active()) error.textContent = err.message || 'Не удалось получить ответ доски'; }
      finally { busy = false; buttons.forEach(node => { node.disabled = false; }); }
    }
    async function act(action, payload = {}) {
      return operation(async () => {
        const data = await request(identifier, action, payload);
        if (!active()) return;
        token = data.confirmation_token || (action === 'prepare' ? null : token);
        render(data);
      });
    }
    async function load() {
      return operation(async () => {
        const data = await request(identifier);
        if (active()) render(data);
      });
    }
    async function loadReview(offset) {
      return operation(async () => {
        const data = await request(identifier, 'review', { offset });
        if (!active()) return;
        const previous = files.querySelector('button');
        if (previous) previous.remove();
        if (offset === 0) files.replaceChildren(el('p', {}, `Изменённых файлов: ${number(data.total)}`));
        for (const file of data.files || []) {
          const change = { added: 'добавлен', modified: 'изменён', deleted: 'удалён' }[file.change] || file.change;
          files.appendChild(el('details', {}, [el('summary', {}, `${file.path} — ${change}`),
            el('pre', { class: 'api-team-text', tabindex: '0' }, file.binary ? 'Двоичный файл; текстового сравнения нет.' : file.diff || 'Нет текстовых изменений')]));
        }
        if (data.next_offset !== null) files.appendChild(button('Следующие файлы', () => loadReview(data.next_offset)));
      });
    }
    // Initial load starts after the caller attaches the drawer to the document.
    Promise.resolve().then(load);
    timer = schedule(() => {
      if (!active()) { unschedule(timer); return; }
      if (['queued', 'running'].includes(lastStatus)) load();
      else if (nextCheckRead && Date.now() >= nextCheckRead) {
        nextCheckRead = Date.now() + 20000;
        load();
      }
    }, 5000);
    return section;
  }
  root.SymphonyApiTeam = { mount };
})(globalThis);
