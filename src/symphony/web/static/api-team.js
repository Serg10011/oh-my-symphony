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
    board_process_interrupted: 'Служба прервала процесс команды. Подробности сохранены в журнале службы.',
    board_execution_failed: 'Процесс команды прерван. Автоматического платного повтора нет.',
    api_assignment_changed: 'Задача или настройки изменились после подтверждения; платный запуск отменён.',
    execution_preflight_failed: 'Проверка изоляции не прошла; модели не запускались.',
    workspace_command_outside_scope: 'Команда записала файл вне разрешённых путей. Её изменения отклонены.',
  };
  const number = value => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'не указан';
  const model = value => value ? `${value.model} · усилие ${value.effort}` : 'не назначен';
  // The detailed report text already carries the amounts. The summary that
  // reads them lives in api-team-costs.js so the card and the report cannot
  // drift apart into two different price calculations.
  function spendSummary(text, session, profiles) {
    const costs = root.SymphonyApiTeamCosts;
    return costs ? costs.summary(text, session, profiles) : null;
  }

  function mount({ el, identifier, request, isActive, onTransition, onProfiles, schedule = setInterval, unschedule = clearInterval }) {
    let token = null, busy = false, timer = null, lastStatus = null;
    let selection = null, pickerKey = '', releaseChoice = null, lastLane = null;
    let nextCheckRead = 0;
    const status = el('p', { 'aria-live': 'polite' }, 'Загружаю назначение…');
    const error = el('p', { class: 'api-team-error', role: 'alert' });
    const spendLead = el('dd');
    const spendWorker = el('dd');
    const spendTotal = el('dd');
    const spendList = el('dl', { class: 'api-team-assignment' }, [
      el('dt', {}, 'Ведущий'), spendLead, el('dt', {}, 'Исполнитель'), spendWorker,
      el('dt', {}, 'Итого'), spendTotal,
    ]);
    const spendNote = el('p', { class: 'form-hint' });
    const spend = el('div', { class: 'api-team-spend', role: 'group', 'aria-label': 'Расход команды' },
      [el('strong', {}, 'Расход команды'), spendList, spendNote]);
    spend.hidden = true;
    const assignment = el('div');
    const route = el('p', { class: 'form-hint' });
    route.hidden = true;
    const selector = el('fieldset', { class: 'api-team-model-picker', 'aria-label': 'Команда карточки' });
    selector.hidden = true;
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
    const prepare = button('Посмотреть назначение', () => act('prepare', pickerKey ? { team: selection } : {}));
    const confirm = button('Подтвердить платный запуск', () => act('confirm', { confirmation_token: token }));
    const stop = button('Остановить', () => act('stop'));
    const refresh = button('Обновить', () => load());
    const review = button('Посмотреть изменения файлов', () => loadReview(0));
    const reviewOriginal = button('Изменения исходной версии', () => loadReview(0, 'original'));
    const releaseStatus = el('p', { role: 'status' });
    const releaseFiles = el('pre', { class: 'api-team-text' });
    const releaseList = el('details', {}, [el('summary', {}, 'Файлы этой версии'), releaseFiles]);
    const releaseButton = button('', () => {
      if (releaseChoice) act(releaseChoice.stage, { offer: releaseChoice.offer });
    });
    const releaseLink = el('a', { target: '_blank', rel: 'noopener noreferrer' }, 'Открыть PR');
    const releaseBox = el('div', { role: 'group', 'aria-label': 'Передача результата' },
      [releaseStatus, releaseList, reviewOriginal, releaseButton, releaseLink]);
    releaseBox.hidden = true;
    const cloudStatus = el('p', { role: 'status' });
    const workflowLink = el('a', { target: '_blank', rel: 'noopener noreferrer' }, 'Открыть попытку на GitHub');
    const cloud = el('div', { role: 'group', 'aria-label': 'Проверка файлов на Linux' },
      [el('strong', {}, 'Проверка файлов на Linux'), cloudStatus, workflowLink]);
    cloud.hidden = true;
    [prepare, confirm, stop, review, reportDetails, resultDetails].forEach(node => { node.hidden = true; });
    const actions = el('div', { class: 'api-team-actions' }, [prepare, confirm, stop, refresh, review]);
    const section = el('section', { class: 'api-team-panel', 'aria-label': 'API-команда' }, [
      el('div', { class: 'section-heading' }, 'API-команда'), status, route, error, selector, spend, assignment, actions,
      el('p', { class: 'form-hint' }, 'Сначала проверь назначение. Только отдельное подтверждение ставит платную работу в очередь. После остановки автоматического повтора нет.'),
      el('p', { class: 'form-hint' }, 'После проверки файлов решение о приёмке принимаешь ты.'),
      cloud, releaseBox, resultDetails, reportDetails, files,
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
      route.hidden = !data.board_stages?.length;
      if (!route.hidden) setText(route, 'Пройденные колонки: ' + data.board_stages.join(' → '));
      status.textContent = statuses[data.status] || 'Неизвестное состояние команды';
      if (data.status === 'result' && data.candidate?.status === 'blocked')
        status.textContent = 'Нужна помощь: проверка отклонила файлы команды.';
      if (data.status === 'result' && data.candidate?.status === 'no_changes')
        status.textContent = 'Нужна помощь: команда завершила работу без изменений файлов.';
      const release = data.release;
      releaseBox.hidden = !release || ['checking', 'waiting'].includes(release.stage);
      releaseChoice = release?.offer ? release : null;
      releaseButton.hidden = !releaseChoice;
      releaseList.hidden = !releaseChoice;
      reviewOriginal.hidden = !releaseChoice?.original;
      releaseLink.hidden = true;
      if (!releaseBox.hidden) {
        if (releaseChoice) {
          const publishing = release.stage === 'publish-check';
          const destination = `${release.repo} · ${release.branch}`;
          setText(releaseStatus, `${release.rebuilt ? 'Версия на обновлённой базе' : 'Версия'} ${release.commit.slice(0, 12)}. Изменённых файлов — ${release.file_count}. ` +
            (publishing ? `Проверка опубликует эти файлы в ${destination}.` :
              `После приёмки откроется PR из ${destination} в ${release.base}.`) +
            (publishing && release.original ? ` В историю войдёт также исходная версия ${release.original.commit.slice(0, 12)}.` : ''));
          const names = group => group.files.join('\n') + (group.files.length < group.file_count ? '\n…' : '');
          setText(releaseFiles, names(release) + (release.original ? '\n\nИсходная версия\n' + names(release.original) : ''));
          releaseButton.textContent = publishing ? 'Опубликовать и проверить на GitHub' :
            (release.accepted ? 'Продолжить создание PR' : 'Принять версию и открыть PR');
        } else if (release.stage === 'delivered') {
          setText(releaseStatus, `Версия ${release.commit.slice(0, 12)} принята · PR №${release.pr.number}`);
          releaseLink.href = `https://github.com/Serg10011/oh-my-symphony/pull/${release.pr.number}`;
          releaseLink.hidden = false;
        } else {
          setText(releaseStatus, release.message || 'Для доставки требуется внимание.');
        }
      }
      if (data.accepted) status.textContent = data.delivered ? 'Версия принята, PR открыт' : 'Версия принята, ожидает создания PR';
      if (data.stop_requested && data.status === 'running') status.textContent += '. Остановка запрошена; текущий ответ ещё может оплачиваться.';
      if (data.reason) status.textContent += ' ' + (reasons[data.reason] || 'Причина остановки указана в отчёте.');
      if (data.session?.execution?.review_source === 'before_command')
        status.textContent += ' Можно посмотреть правки, сохранённые до этой команды.';
      if (data.status !== 'prepared') token = null;
      prepare.hidden = !['unassigned', 'prepared'].includes(data.status);
      confirm.hidden = data.status !== 'prepared' || !token;
      const choices = data.team_picker;
      selector.hidden = !choices || !['unassigned','prepared'].includes(data.status);
      if (!selector.hidden && window.SymphonyApiModels) {
        const key = JSON.stringify(choices);
        if (key !== pickerKey) {
          pickerKey = key; selection = choices.override;
          const flag = el('input', { type: 'checkbox', 'aria-label': 'Другая команда для этой карточки' });
          flag.checked = Boolean(selection);
          const invalidate = () => { token = null; confirm.hidden = true; };
          const make = current => window.SymphonyApiModels.picker(el, choices, current, pair => {
            selection = pair; invalidate();
          });
          let inputs = make(selection || choices.default);
          inputs.disable(!flag.checked);
          const checkbox = el('label', {}, [flag, ' Другая команда для этой карточки']);
          selector.replaceChildren(checkbox, inputs.row);
          flag.addEventListener('change', () => {
            invalidate();
            if (!flag.checked) { selection = null; inputs = make(choices.default); selector.replaceChildren(checkbox, inputs.row); }
            else selection = inputs.value();
            inputs.disable(!flag.checked);
          });
        }
      } else { pickerKey = ''; selection = null; }
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
          queued: 'GitHub поставил проверку в очередь.',
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
      const spendData = data.session ? spendSummary(data.report_text, data.session, data.profiles) : null;
      spend.hidden = !spendData;
      if (spendData) {
        const markSpend = (node, incomplete) => {
          node.dataset.incomplete = incomplete ? 'true' : 'false';
        };
        setText(spendLead, spendData.lead.value);
        setText(spendWorker, spendData.worker.value);
        setText(spendTotal, spendData.total.value);
        setText(spendNote, spendData.note);
        markSpend(spendLead, spendData.lead.incomplete);
        markSpend(spendWorker, spendData.worker.incomplete);
        markSpend(spendTotal, spendData.total.incomplete);
        markSpend(spendNote, spendData.total.incomplete);
        spend.classList.toggle('api-team-spend-incomplete', spendData.total.incomplete);
      }
      reportDetails.hidden = !data.report_text;
      resultDetails.hidden = !data.session?.result;
      setText(report, data.report_text || '');
      setText(result, data.session?.result || '');
      if (data.profiles) {
        onProfiles?.(data.profiles);
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
      const lane = data.board_stages?.at(-1);
      if (lastStatus !== null && (lastStatus !== data.status || lastLane !== lane)) onTransition?.();
      lastStatus = data.status;
      lastLane = lane;
    }
    async function operation(fn) {
      if (busy || !active()) return;
      busy = true;
      selector.disabled = true;
      buttons.forEach(node => { node.disabled = true; });
      error.textContent = '';
      try { await fn(); }
      catch (err) { if (active()) error.textContent = err.message || 'Не удалось получить ответ доски'; }
      finally { busy = false; selector.disabled = false; buttons.forEach(node => { node.disabled = false; }); }
    }
    async function act(action, payload = {}) {
      return operation(async () => {
        const data = await request(identifier, action, payload);
        if (!active()) return;
        if (action === 'prepare') window.SymphonyApiModels?.invalidate();
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
    async function loadReview(offset, version = 'checked') {
      return operation(async () => {
        const data = await request(identifier, 'review', version === 'original' ? { offset, version } : { offset });
        if (!active()) return;
        const previous = files.querySelector('button');
        if (previous) previous.remove();
        if (offset === 0) {
          files.replaceChildren(el('p', {}, `Изменённых файлов: ${number(data.total)}`));
          if (data.source === 'delivery_target') files.prepend(el('p', {},
            `Изменения версии ${data.commit.slice(0, 12)} относительно обновлённой базы ${data.base_commit.slice(0, 12)}.`));
          if (data.source === 'original_candidate') files.prepend(el('p', {}, `Исходная версия ${data.commit.slice(0, 12)}.`));
          if (data.source === 'before_command') files.prepend(el('p', {},
            'Черновик до остановившей работу команды. Её изменения здесь не показаны; задача не сдана.'));
        }
        for (const file of data.files || []) {
          const change = { added: 'добавлен', modified: 'изменён', deleted: 'удалён' }[file.change] || file.change;
          files.appendChild(el('details', {}, [el('summary', {}, `${file.path} — ${change}`),
            el('pre', { class: 'api-team-text', tabindex: '0' }, file.binary ? 'Двоичный файл; текстового сравнения нет.' : file.diff || 'Нет текстовых изменений')]));
        }
        if (data.next_offset !== null) files.appendChild(button('Следующие файлы', () => loadReview(data.next_offset, version)));
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
  root.SymphonyApiTeam = { mount, spendSummary };
})(globalThis);
