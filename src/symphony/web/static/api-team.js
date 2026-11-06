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
  const amountPattern = /([0-9]+(?:\.[0-9]{1,6})?)\s*USD/;

  // The board already returns the detailed spend report as text. Read the
  // same amounts from it instead of inventing a second price calculation.
  function micros(value) {
    const match = typeof value === 'string' ? amountPattern.exec(value) : null;
    if (!match) return null;
    const [whole, fraction = ''] = match[1].split('.');
    return Number(whole) * 1000000 + Number((fraction + '000000').slice(0, 6));
  }

  function usd(value) {
    if (value === null || value === undefined) return 'н/д';
    return `${Math.floor(value / 1000000)}.${String(value % 1000000).padStart(6, '0')} USD`;
  }

  function count(value) {
    if (typeof value !== 'string') return null;
    const match = /^\s*([0-9][0-9\s\u00a0\u202f]*)/.exec(value);
    if (!match) return null;
    const digits = match[1].replace(/[\s\u00a0\u202f]/g, '');
    return digits ? Number(digits) : null;
  }

  function costLine(value) {
    const known = micros(value);
    return { known, complete: known !== null && value.includes('осторожная оценка по каталогу') };
  }

  function modelRows(lines) {
    const start = lines.findIndex(line => line.trim() === 'Итог по моделям');
    if (start < 0) return [];
    let end = lines.findIndex((line, index) => index > start && line.trim() === 'Токены и кеш за сессию');
    if (end < 0) end = lines.length;
    const rows = [];
    let current = null;
    for (let index = start + 1; index < end; index += 1) {
      const line = lines[index];
      const heading = /^  (.+): запросов (.*)$/.exec(line);
      if (heading) {
        current = { model: heading[1], calls: count(heading[2]), profiles: [], known: null, complete: false };
        rows.push(current);
        continue;
      }
      if (!current) continue;
      const profiles = /^    профили: (.*)$/.exec(line);
      if (profiles) {
        current.profiles = profiles[1].split(',').map(item => item.trim()).filter(Boolean);
        continue;
      }
      const cost = /^    стоимость: (.*)$/.exec(line);
      if (cost) Object.assign(current, costLine(cost[1]));
    }
    return rows;
  }

  function spendSummary(text, session, profiles) {
    if (typeof text !== 'string' || !text.trim()) return null;
    const lines = text.split(/\r?\n/);
    const sessionProfiles = session && typeof session === 'object' ? session.profiles : null;
    const boardProfiles = profiles && typeof profiles === 'object' ? profiles : null;
    const profile = role => (sessionProfiles && sessionProfiles[role])
      || (boardProfiles && boardProfiles[role]) || null;
    const lead = profile('lead'), worker = profile('worker');
    const leadName = (session && typeof session.lead_profile === 'string' && session.lead_profile)
      || (lead && lead.name) || null;
    const declaredWorker = session && Object.prototype.hasOwnProperty.call(session, 'worker_profile')
      ? session.worker_profile : undefined;
    const workerName = (typeof declaredWorker === 'string' && declaredWorker)
      || (worker && worker.name) || null;
    const leadModel = lead && lead.model ? lead.model : null;
    const workerModel = worker && worker.model ? worker.model : null;
    const rows = modelRows(lines);
    let totalLine = null, knownLine = null, missingLine = null;
    for (const line of lines) {
      if (totalLine === null && line.startsWith('  оценка за всю сессию:')) totalLine = line;
      if (knownLine === null && line.startsWith('  известная часть:')) knownLine = line;
      if (missingLine === null && line.startsWith('  запросов без оценки:')) missingLine = line;
    }
    if (!rows.length && totalLine === null && knownLine === null) return null;

    let totalKnown = null, totalComplete = false, knownTotal = null;
    let estimatedRequests = null, reportedCalls = null, missing = null;
    if (totalLine !== null) {
      if (!totalLine.includes('полной суммы нет')) {
        totalKnown = micros(totalLine);
        totalComplete = totalKnown !== null;
      }
    }
    if (knownLine !== null) {
      knownTotal = micros(knownLine);
      const match = /известная часть:.*? за\s+(.+?)\s+из\s+(.+?)\s+запросов/.exec(knownLine);
      if (match) {
        estimatedRequests = count(match[1]);
        reportedCalls = count(match[2]);
      }
    }
    if (missingLine !== null) {
      missing = count(missingLine.replace(/^  запросов без оценки:\s*/, ''));
    }
    if (totalLine === null && knownLine === null) {
      totalKnown = rows.reduce((sum, row) => sum + (row.known === null ? 0 : row.known), 0);
      knownTotal = totalKnown;
      totalComplete = rows.length > 0 && rows.every(row => row.complete && row.known !== null);
    }
    if (reportedCalls === null) {
      reportedCalls = rows.reduce((sum, row) => sum + (typeof row.calls === 'number' ? row.calls : 0), 0);
    }

    const roleState = () => ({ assigned: false, known: 0, knownSeen: false, complete: true,
      seen: false, calls: 0 });
    const roles = { lead: roleState(), worker: roleState() };
    roles.lead.assigned = Boolean(leadName || leadModel || lead);
    roles.worker.assigned = Boolean(workerName || workerModel || worker);
    const roleName = name => {
      if (leadName && name === leadName) return 'lead';
      if (workerName && name === workerName) return 'worker';
      if (!leadName && /-lead$/i.test(name)) return 'lead';
      if (!workerName && /-worker$/i.test(name)) return 'worker';
      return null;
    };
    let breakdownIncomplete = false, unallocatedKnown = 0;
    for (const row of rows) {
      const rowRoles = new Set();
      for (const name of row.profiles) {
        const role = roleName(name);
        if (role) {
          rowRoles.add(role);
          roles[role].assigned = true;
        }
      }
      if (!rowRoles.size) {
        if (leadModel && row.model === leadModel) rowRoles.add('lead');
        if (workerModel && row.model === workerModel) rowRoles.add('worker');
      }
      if (rowRoles.size === 1) {
        const state = roles[Array.from(rowRoles)[0]];
        state.seen = true;
        if (typeof row.calls === 'number') state.calls += row.calls;
        if (row.known !== null) {
          state.known += row.known;
          state.knownSeen = true;
        }
        if (!row.complete || row.known === null) state.complete = false;
      } else {
        breakdownIncomplete = true;
        if (row.known !== null) unallocatedKnown += row.known;
        for (const role of rowRoles) roles[role].complete = false;
      }
    }
    const usageCalls = name => {
      if (!name || !session || !session.usage) return null;
      const usage = session.usage[name];
      return usage && typeof usage === 'object' && typeof usage.calls === 'number' ? usage.calls : null;
    };
    const expected = { lead: usageCalls(leadName), worker: usageCalls(workerName) };
    for (const key of ['lead', 'worker']) {
      const state = roles[key];
      if (!state.assigned) continue;
      if (typeof expected[key] === 'number') {
        if (state.seen) {
          if (state.calls < expected[key]) state.complete = false;
        } else if (expected[key] > 0) {
          state.known = null;
          state.complete = false;
        }
      }
      if (!state.knownSeen && state.known !== null && state.seen) state.known = null;
      if (state.known === null) state.complete = false;
    }
    const roleValue = state => {
      if (!state.assigned) return 'не назначен';
      if (state.known === null) return 'н/д';
      return usd(state.known) + (state.complete ? '' : ' (неполный)');
    };

    let value = totalKnown !== null ? totalKnown : knownTotal;
    const totalIncomplete = !totalComplete || (typeof missing === 'number' && missing > 0);
    if (totalIncomplete && knownTotal !== null) value = knownTotal;
    let note;
    if (totalIncomplete) {
      note = value === null ? 'Сумма неизвестна; итог неполный.' : 'Показана известная часть; итог неполный';
      if (typeof missing === 'number' && missing > 0) {
        note += `: ${missing}${typeof reportedCalls === 'number' ? ` из ${reportedCalls}` : ''} запросов без оценки`;
      } else {
        note += ': часть запросов без оценки';
      }
      note += '.';
    } else if (breakdownIncomplete) {
      note = 'Оценка по каталогу; разбивка по участникам неполная.';
    } else {
      note = 'Оценка по каталогу; счёт провайдера не сверен.';
    }
    if (breakdownIncomplete && totalIncomplete) note += ' Часть расхода не разделена между участниками.';
    return {
      lead: { value: roleValue(roles.lead), incomplete: roles.lead.assigned && !roles.lead.complete },
      worker: { value: roleValue(roles.worker), incomplete: roles.worker.assigned && !roles.worker.complete },
      total: { value: value === null ? 'н/д' : usd(value), incomplete: totalIncomplete },
      note,
      breakdownIncomplete,
    };
  }

  function mount({ el, identifier, request, isActive, onTransition, onProfiles, schedule = setInterval, unschedule = clearInterval }) {
    let token = null, busy = false, timer = null, lastStatus = null;
    let selection = null, pickerKey = '';
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
      route.hidden = !data.board_stages?.length;
      if (!route.hidden) setText(route, 'Пройденные колонки: ' + data.board_stages.join(' → '));
      status.textContent = statuses[data.status] || 'Неизвестное состояние команды';
      if (data.status === 'result' && data.candidate?.status === 'blocked')
        status.textContent = 'Нужна помощь: проверка отклонила файлы команды.';
      if (data.status === 'result' && data.candidate?.status === 'no_changes')
        status.textContent = 'Нужна помощь: команда завершила работу без изменений файлов.';
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
      const spendData = spendSummary(data.report_text, data.session, data.profiles);
      spend.hidden = !spendData;
      if (spendData) {
        setText(spendLead, spendData.lead.value);
        setText(spendWorker, spendData.worker.value);
        setText(spendTotal, spendData.total.value);
        setText(spendNote, spendData.note);
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
      if (lastStatus !== null && lastStatus !== data.status) onTransition?.();
      lastStatus = data.status;
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
    async function loadReview(offset) {
      return operation(async () => {
        const data = await request(identifier, 'review', { offset });
        if (!active()) return;
        const previous = files.querySelector('button');
        if (previous) previous.remove();
        if (offset === 0) {
          files.replaceChildren(el('p', {}, `Изменённых файлов: ${number(data.total)}`));
          if (data.source === 'before_command') files.prepend(el('p', {},
            'Черновик до остановившей работу команды. Её изменения здесь не показаны; задача не сдана.'));
        }
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
  root.SymphonyApiTeam = { mount, spendSummary };
})(globalThis);
