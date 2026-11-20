/* Optional API-team controls for the existing issue drawer. No credentials in the browser. */
(function (root) {
  'use strict';
  const statuses = {
    unassigned: 'Назначение ещё не подготовлено', prepared: 'Ожидает твоего подтверждения',
    queued: 'Подтверждено, ожидает свободного места', running: 'Команда работает',
    result: 'Команда вернула результат; приёмка ещё не выполнена', stopped: 'Запуск остановлен.',
  };
  const reasons = {
    operator_stopped: 'Остановлено оператором или после потери связи с доской.',
    board_process_interrupted: 'Служба прервала процесс команды. Подробности сохранены в журнале службы.',
    board_execution_failed: 'Процесс команды прерван. Автоматического платного повтора нет.',
    api_assignment_changed: 'Задача или настройки изменились после подтверждения; платный запуск отменён.',
    api_permission_revoked: 'Разрешение доски отозвано или изменилось. Новые обращения к моделям остановлены.',
    execution_preflight_failed: 'Проверка изоляции не прошла; модели не запускались.',
    flash_cli_unavailable: 'На Mac недоступен Antigravity CLI для Flash.',
    flash_customizations_active: 'Активные расширения Antigravity мешают проверить ограничения Flash.',
    flash_worker_failed: 'Сбой исполнителя Flash. Сохранённый расход доступен в отчёте.',
    flash_gate_rejected: 'Ограничения доступа Flash не подтверждены.',
    flash_gate_unverified: 'Проверка ограничений Flash не прошла.',
    flash_capacity_unavailable: 'Antigravity сообщил об ограничении квоты или доступности Flash. Остаток можно обновить в настройках.',
    flash_region_unsupported: 'Google отклонил запрос Flash по региону подключения. Проверь, включён ли VPN, и соединение с Antigravity.',
    flash_quota_exhausted: 'По последнему снимку квота Gemini исчерпана.',
    flash_output_limit: 'Ответ Flash превысил порог выходных токенов.',
    workspace_command_outside_scope: 'Команда записала файл вне разрешённых путей. Её изменения отклонены.',
    api_plan_invalid: 'План отклонён. Причины указаны в отчёте.',
    api_plan_not_configured: 'Проверь колонки Plan и Subtasks и настройки проекта.',
    api_plan_staging_failed: 'Запись подзадач прервана. Сохранённый план можно восстановить.',
    api_plan_parent_changed: 'Исходная карточка или версия задачи изменились.',
    api_plan_subtask_changed: 'Номер подзадачи занят или черновик изменён.',
    api_subtask_check_failed: 'Целевая проверка подзадачи не пройдена.',
    api_subtask_check_changed_files: 'Проверка изменила файлы результата.',
    api_subtask_result_changed: 'Версия ранее проверенной подзадачи изменилась.',
    api_subtasks_budget_exhausted: 'Достигнут общий предел составной задачи.',
    api_subtasks_not_active: 'Выполнение составной задачи остановлено.',
    api_final_result_changed: 'Закреплённая версия, база или исходная карточка изменились.',
    api_final_settings_changed: 'Настройки итоговой проверки изменились.',
    api_final_test_interrupted: 'Итоговая проверка была прервана.',
    api_final_tests_unavailable: 'Команда проверки проекта недоступна в изоляции.',
    api_final_tests_failed: 'Общий результат не прошёл итоговую проверку.',
    api_final_test_copy_changed: 'Итоговая проверка изменила отслеживаемые файлы.',
    api_final_review_lane_required: 'На доске нужна колонка Human Review для передачи результата.',
  };
  const number = value => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'не указан';
  const model = value => value ? `${value.model} · усилие ${value.effort}` : 'не назначен';

  function leadCacheShare(session) {
    const lead = session.lead_profile || session.profiles?.lead?.name;
    const usage = typeof lead === 'string' && lead ? session.usage?.[lead] : null;
    const { input_tokens: input, cached_input_tokens: cached } = usage ?? {};
    if (!Number.isSafeInteger(input) || !Number.isSafeInteger(cached) ||
        input <= 0 || cached < 0 || cached > input) return 'Нет данных';

    // The saved input count already includes cache hits.
    const share = cached / input * 100;
    const percent = share > 0 && share < 0.01 ? '<0,01' : share.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
    return `${number(cached)} (${percent}%)`;
  }

  function delegationStats(session) {
    const noData = 'Нет данных';
    const parts = session?.parts;
    if (!parts || typeof parts !== 'object' || Array.isArray(parts)) {
      return { delegations: noData, returns: noData };
    }
    const entries = Object.entries(parts);
    if (!entries.length) return { delegations: number(0), returns: number(0) };

    let totalReturns = 0;
    let returnsValid = true;

    for (const [, record] of entries) {
      if (!record || typeof record !== 'object' || Array.isArray(record)) {
        return { delegations: noData, returns: noData };
      }
      const attempts = record.attempts;
      if (!Number.isSafeInteger(attempts) || attempts < 1) {
        returnsValid = false;
      } else {
        totalReturns += attempts - 1;
      }
    }

    return {
      delegations: number(entries.length),
      returns: returnsValid ? number(totalReturns) : noData,
    };
  }

  function profileCallStats(session) {
    const savedCalls = profile => {
      const calls = typeof profile === 'string' && profile ? session.usage?.[profile]?.calls : undefined;
      return Number.isSafeInteger(calls) && calls >= 0 ? number(calls) : 'Нет данных';
    };
    const lead = session.lead_profile || session.profiles?.lead?.name;
    const worker = session.worker_profile;
    const workerSnapshot = session.profiles?.worker;
    // Explicit null means solo, even if an older profile snapshot is present.
    const solo = worker === null || (worker === undefined && workerSnapshot == null);
    return {
      lead: savedCalls(lead),
      worker: solo ? 'не назначен' : savedCalls(worker === undefined ? workerSnapshot?.name : worker),
    };
  }

  // The detailed report text already carries the amounts. The summary that
  // reads them lives in api-team-costs.js so the card and the report cannot
  // drift apart into two different price calculations.
  function spendSummary(text, session, profiles) {
    const costs = root.SymphonyApiTeamCosts;
    return costs ? costs.summary(text, session, profiles) : null;
  }

  function mount({ el, identifier, request, isActive, onTransition, onProfiles, schedule = setInterval, unschedule = clearInterval }) {
    let token = null, planToken = null, busy = false, timer = null, lastStatus = null;
    let selection = null, pickerKey = '', releaseChoice = null, lastLane = null;
    let nextCheckRead = 0;
    let submission = null;
    const status = el('p', { 'aria-live': 'polite' }, 'Загружаю назначение…');
    const error = el('p', { class: 'api-team-error', role: 'alert' });
    const spendLead = el('dd');
    const spendLeadCalls = el('dd');
    const spendWorker = el('dd');
    const spendWorkerCalls = el('dd');
    const spendTotal = el('dd');
    const spendTotalLabel = el('dt', {}, 'Итого');
    const spendLeadCache = el('dd');
    const spendDelegations = el('dd');
    const spendReturns = el('dd');
    const spendList = el('dl', { class: 'api-team-assignment' }, [
      el('dt', {}, 'Ведущий'), spendLead,
      el('dt', {}, 'Запросы ведущего'), spendLeadCalls,
      el('dt', {}, 'Исполнитель'), spendWorker,
      el('dt', {}, 'Запросы исполнителя'), spendWorkerCalls,
      spendTotalLabel, spendTotal,
      el('dt', {}, 'Вход ведущего из кеша, токены'), spendLeadCache,
      el('dt', {}, 'Поручения исполнителю'), spendDelegations,
      el('dt', {}, 'Возвраты на доработку'), spendReturns,
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
    const submit = button('Передать в работу', () => { if (submission) act('submit', submission); });
    const planPrepare = button('Посмотреть запуск подзадач', () => act('prepare-subtasks'));
    const planStart = button('Подтвердить запуск подзадач', () => act('start-subtasks', { confirmation_token: planToken }));
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
    [prepare, confirm, submit, planPrepare, planStart, stop, review, reportDetails, resultDetails].forEach(node => { node.hidden = true; });
    const actions = el('div', { class: 'api-team-actions' }, [prepare, confirm, submit, planPrepare, planStart, stop, refresh, review]);
    const launchHint = el('p', { class: 'form-hint' });
    const section = el('section', { class: 'api-team-panel', 'aria-label': 'API-команда' }, [
      el('div', { class: 'section-heading' }, 'API-команда'), status, route, error, selector, spend, assignment, actions,
      launchHint,
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
      submission = data.submission || null;
      const permitted = data.execution_permission?.status === 'active';
      launchHint.textContent = permitted && submission
        ? 'Действует разрешение доски. Передай готовую задачу в работу; дополнительного подтверждения оплаты не требуется.'
        : data.submission_refusal ? 'Карточка создана до действующего разрешения доски. Для неё требуется отдельное подтверждение.'
          : ['revoked','changed','invalid'].includes(data.execution_permission?.status)
            ? 'Разрешение доски отозвано или требует проверки. Новые обращения к моделям остановлены.'
            : 'Сначала проверь назначение. Отдельное подтверждение ставит платную работу в очередь. После остановки автоматического повтора нет.';
      if (permitted && data.authorization) launchHint.textContent = 'Задача передана по разрешению доски. Фактическое назначение и расходы сохранены в истории.';
      if (permitted && submission && ['unassigned','prepared'].includes(data.status)) status.textContent = 'Ожидает передачи в работу';
      if (data.status === 'result' && data.candidate?.status === 'blocked')
        status.textContent = 'Нужна помощь: проверка отклонила файлы команды.';
      if (data.status === 'result' && data.candidate?.status === 'no_changes')
        status.textContent = 'Нужна помощь: команда завершила работу без изменений файлов.';
      const release = data.release;
      const groupReady = release?.stage === 'group-ready';
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
        } else if (groupReady) {
          setText(releaseStatus, `Проверен коммит ${release.commit}. ${release.checks}`);
        } else {
          setText(releaseStatus, release.message || 'Для доставки требуется внимание.');
        }
      }
      if (data.accepted) status.textContent = release?.completion?.message ||
        (data.delivered ? 'Версия принята, PR открыт' : 'Версия принята, ожидает создания PR');
      if (groupReady) status.textContent = 'Готова к общему выпуску';
      const draft = data.plan_draft === true;
      const launch = data.plan?.launch;
      const retry = launch?.retry;
      launchHint.hidden = draft;
      if (data.phase === 'plan') launchHint.textContent += ' На этапе Plan работает только ведущий.';
      if (data.phase === 'plan' && data.status === 'running') status.textContent = 'Ведущий готовит план';
      if (data.plan?.status === 'ready') status.textContent = `План проверен. Черновиков подзадач — ${data.plan.cards.length}.`;
      if (launch?.status !== 'prepared') planToken = null;
      planPrepare.textContent = retry ? 'Посмотреть повтор подзадачи' : 'Посмотреть запуск подзадач';
      planStart.textContent = retry ? 'Подтвердить повтор подзадачи' : 'Подтвердить запуск подзадач';
      planPrepare.hidden = !(['unassigned', 'prepared'].includes(launch?.status) || retry) || Boolean(planToken);
      planStart.hidden = launch?.status !== 'prepared' || !planToken;
      if (launch?.status === 'prepared' && planToken) {
        const cap = launch.budget ? `Остаток общего лимита — ${launch.budget.max_usd} USD.` : 'Отдельного денежного потолка нет.';
        const subject = retry ? `Повтор ${retry.identifier} на базе ${retry.base_commit.slice(0, 12)}. Уже проверено частей — ${launch.checked}.` : `Подзадач — ${launch.total}.`;
        launchHint.textContent = `${subject} Ведущий — ${model(launch.profiles.lead)}. Исполнитель — ${model(launch.profiles.worker)}. ${cap} Пределы запросов и ответов действуют на каждую подзадачу.`;
      }
      if (launch?.status === 'active') status.textContent = `Проверено подзадач — ${launch.checked} из ${launch.total}. Текущая — ${launch.next}.`;
      if (launch?.status === 'complete') status.textContent = 'Все подзадачи проверены. Общий результат ждёт итоговой проверки.';
      if (launch?.status === 'stopped' || (retry && launch.status === 'prepared')) status.textContent = 'Выполнение подзадач остановлено. ' + (reasons[launch.reason] || 'Посмотри отчёт текущей подзадачи.');
      const final = data.final;
      if (final?.status === 'checking') status.textContent = final.method === 'cloud'
        ? 'Общая версия готова к проверке на Linux' : 'Итоговая проверка общей версии';
      if (final?.status === 'failed') status.textContent = reasons[final.reason] || 'Итоговая проверка требует внимания.';
      if (final?.status === 'ready') status.textContent = 'Общий результат проверен и ждёт твоей приёмки';
      if (final && data.accepted) status.textContent = release?.completion?.message || 'Общий результат принят';
      if (data.subtask?.check?.status === 'passed') status.textContent = 'Целевая проверка подзадачи пройдена';
      if (draft) status.textContent = 'Черновик подзадачи';
      if (data.stop_requested && data.status === 'running') status.textContent += '. Остановка запрошена; текущий ответ ещё может оплачиваться.';
      if (data.reason) status.textContent += ' ' + (reasons[data.reason] || 'Причина остановки указана в отчёте.');
      if (data.gemini_wait_until) {
        const reset = new Date(data.gemini_wait_until);
        if (Number.isFinite(reset.getTime())) status.textContent += ' Обновление квоты ожидается ' + reset.toLocaleString('ru-RU') + '.';
      }
      if (data.session?.execution?.review_source === 'before_command')
        status.textContent += ' Можно посмотреть правки, сохранённые до этой команды.';
      if (data.status !== 'prepared') token = null;
      prepare.hidden = draft || !['unassigned', 'prepared'].includes(data.status);
      confirm.hidden = data.status !== 'prepared' || !token;
      submit.hidden = draft || !permitted || !submission || !['unassigned','prepared'].includes(data.status) || Boolean(selection);
      if (!submit.hidden) confirm.hidden = true;
      const choices = data.team_picker;
      selector.hidden = draft || !choices || !['unassigned','prepared'].includes(data.status);
      if (!selector.hidden && window.SymphonyApiModels) {
        const key = JSON.stringify(choices);
        if (key !== pickerKey) {
          pickerKey = key; selection = choices.override;
          const flag = el('input', { type: 'checkbox', 'aria-label': 'Другая команда для этой карточки' });
          flag.checked = Boolean(selection);
          const invalidate = () => {
            token = null; confirm.hidden = true;
            submit.hidden = !permitted || !submission || Boolean(selection);
          };
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
            submit.hidden = !permitted || !submission || Boolean(selection);
          });
        }
      } else { pickerKey = ''; selection = null; }
      stop.hidden = launch?.status !== 'active' && (!['prepared', 'queued', 'running'].includes(data.status) || data.stop_requested);
      review.hidden = !(data.session?.execution || final?.commit) || !['result', 'stopped'].includes(data.status);
      const check = data.cloud_check, receipt = check?.phase ? null : check?.receipt;
      cloud.hidden = !check || groupReady;
      workflowLink.hidden = !receipt;
      nextCheckRead = check?.status === 'ready' && !groupReady ? Date.now() + (receipt ? 60000 : 20000) : 0;
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
      spend.hidden = !data.session;
      if (data.session) {
        const spendData = spendSummary(data.report_text, data.session, data.profiles);
        const markSpend = (node, incomplete) => {
          node.dataset.incomplete = incomplete ? 'true' : 'false';
        };
        // Missing cost estimates do not make the saved session counters unavailable.
        setText(spendLead, spendData?.lead.value ?? 'н/д');
        setText(spendWorker, spendData?.worker.value ?? 'н/д');
        setText(spendTotalLabel, spendData?.subscription ? 'Итого по API' : 'Итого');
        setText(spendTotal, spendData?.total.value ?? 'н/д');
        setText(spendNote, spendData?.note ?? '');
        markSpend(spendLead, spendData?.lead.incomplete ?? true);
        markSpend(spendWorker, spendData?.worker.incomplete ?? true);
        markSpend(spendTotal, spendData?.total.incomplete ?? true);
        markSpend(spendNote, spendData?.total.incomplete);
        spend.classList.toggle('api-team-spend-incomplete', spendData?.total.incomplete ?? true);
        const calls = profileCallStats(data.session);
        setText(spendLeadCalls, calls.lead);
        setText(spendWorkerCalls, calls.worker);
        setText(spendLeadCache, leadCacheShare(data.session));
        const delegations = delegationStats(data.session);
        setText(spendDelegations, delegations.delegations);
        setText(spendReturns, delegations.returns);
      }
      reportDetails.hidden = !data.report_text;
      resultDetails.hidden = !data.session?.result;
      setText(report, data.report_text || '');
      setText(result, data.session?.result || '');
      if (data.profiles) {
        onProfiles?.(data.profiles);
        const limits = data.limits || {}, project = data.project || {};
        const subscription = data.profiles.worker?.provider === 'antigravity';
        const rows = [
          ['Ведущий', model(data.session?.lead_effort?.current ? { ...data.profiles.lead, effort: data.session.lead_effort.current } : data.profiles.lead)],
          ['Исполнитель', model(data.profiles.worker)],
          ...(subscription ? [
            ['Доступ Flash', 'Подписка Antigravity. Дополнительные кредиты выключены.'],
            ['Порог ответа Flash', 'Конвейер проверяет объём после ответа и до выполнения операций.'],
          ] : []),
          ...(data.lead_escalation === 'high-to-max' ? [['Повышение усилия', 'Ведущий может повысить high до max с записью причины.']] : []),
          ...(data.mode ? [['Режим', data.mode === 'economy' ? 'Экономия' : 'Продуктивность']] : []),
          ['Денежный потолок', data.budget ? `${data.budget.max_usd} USD` : 'без отдельного денежного потолка'],
          ['Аварийный предел запросов', number(limits.max_calls)],
          ['Токенов на ответ / на все ответы', `${number(limits.output_tokens_per_call)} / ${number(limits.output_tokens_total)}`],
          ['Время на запуск', `${number(data.process_timeout_seconds / 60)} мин`],
          ['Версия проекта', project.commit || 'не указана'], ['Доступные файлы', (project.paths || []).join(', ')],
          ...(data.project_context?.paths?.length ? [['Общий контекст DeepSeek', data.project_context.paths.join(', ')]] : []),
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
      const pollingStatus = launch?.status === 'active' ? 'running' : data.status;
      if (lastStatus !== null && (lastStatus !== pollingStatus || lastLane !== lane)) onTransition?.();
      lastStatus = pollingStatus;
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
        if (action === 'prepare-subtasks') planToken = data.confirmation_token || null;
        else token = data.confirmation_token || (action === 'prepare' ? null : token);
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
