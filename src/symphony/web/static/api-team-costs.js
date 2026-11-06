/* Spend summary for the API card. Amounts are read from the detailed report
   text the board already returns, so the card never invents a second price
   calculation and never turns an unknown amount into zero. */
(function (root) {
  'use strict';
  const amountPattern = /([0-9]+(?:\.[0-9]{1,6})?)\s*USD/;
  const INCOMPLETE = ' (неполный)';
  const SESSION_TOTAL = '  оценка за всю сессию:';
  const SESSION_KNOWN = '  известная часть:';
  const SESSION_MISSING = '  запросов без оценки:';

  // Micro-USD integer arithmetic keeps the displayed amounts identical to the
  // six-decimal amounts already printed in the report.
  function micros(value) {
    const match = typeof value === 'string' ? amountPattern.exec(value) : null;
    if (!match) return null;
    const parts = match[1].split('.');
    const fraction = ((parts[1] || '') + '000000').slice(0, 6);
    return Number(parts[0]) * 1000000 + Number(fraction);
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

  function text(value) {
    return typeof value === 'string' && value.trim() ? value : null;
  }

  // "0.1 USD — осторожная оценка по каталогу" is complete; "неполная,
  // известная часть 0.1 USD за 1 из 2 запросов" is a known part only.
  function costLine(value) {
    const known = micros(value);
    return { known, complete: known !== null && value.indexOf('осторожная оценка по каталогу') >= 0 };
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

  function reportLines(source) {
    const lines = source.split(/\r?\n/);
    const pick = prefix => lines.find(line => line.startsWith(prefix)) || null;
    return { rows: modelRows(lines), totalLine: pick(SESSION_TOTAL),
      knownLine: pick(SESSION_KNOWN), missingLine: pick(SESSION_MISSING) };
  }

  function zeroSummary(note, roleNames) {
    const role = assigned => ({ value: assigned ? usd(0) : 'не назначен', amount: assigned ? 0 : null,
      assigned, incomplete: false });
    return { lead: role(Boolean(roleNames.lead)), worker: role(Boolean(roleNames.worker)),
      total: { value: usd(0), amount: 0, known: 0, incomplete: false },
      note, complete: true, breakdownComplete: true, breakdownIncomplete: false,
      unallocated: { amount: 0, incomplete: false }, calls: 0, missing: 0, estimated: 0 };
  }

  function unknownSummary(note, roleNames, counts) {
    const role = assigned => ({ value: assigned ? 'н/д' : 'не назначен', amount: null,
      assigned, incomplete: assigned });
    return { lead: role(Boolean(roleNames.lead)), worker: role(Boolean(roleNames.worker)),
      total: { value: 'н/д', amount: null, known: null, incomplete: true },
      note, complete: false, breakdownComplete: false, breakdownIncomplete: true,
      unallocated: { amount: null, incomplete: true },
      calls: counts.calls, missing: null, estimated: null };
  }

  // Nothing was spent only when the saved counters say so; a truncated or
  // contradictory session is not reported as a confident zero.
  function nothingSpent(value) {
    if (!value || typeof value !== 'object') return true;
    if (typeof value.calls === 'number' && value.calls !== 0) return false;
    const counters = value.usage && typeof value.usage === 'object' ? Object.values(value.usage) : [];
    return counters.every(record => !record || typeof record !== 'object'
      || !(typeof record.calls === 'number' && record.calls > 0));
  }

  // The summary always describes what is known: the team total, the lead and
  // the executor part. A partial amount is shown as the known part and marked
  // as an incomplete total instead of being presented as the full price.
  function summary(source, session, profiles) {
    const sessionProfiles = session && typeof session === 'object' && session.profiles
      && typeof session.profiles === 'object' ? session.profiles : null;
    const boardProfiles = profiles && typeof profiles === 'object' ? profiles : null;
    const profile = role => (sessionProfiles && sessionProfiles[role])
      || (boardProfiles && boardProfiles[role]) || null;
    const lead = profile('lead'), worker = profile('worker');
    const leadName = text(session && session.lead_profile) || text(lead && lead.name);
    const declaredWorker = session && Object.prototype.hasOwnProperty.call(session, 'worker_profile')
      ? session.worker_profile : undefined;
    const workerName = text(declaredWorker) || text(worker && worker.name);
    const leadModel = text(lead && lead.model);
    const workerModel = text(worker && worker.model);
    const roleNames = { lead: leadName || leadModel, worker: workerName || workerModel };
    const usage = session && session.usage && typeof session.usage === 'object' ? session.usage : null;
    const sessionCalls = session && typeof session.calls === 'number' ? session.calls : null;
    const parsed = reportLines(text(source) || '');
    const hasCost = parsed.rows.length > 0 || parsed.totalLine !== null || parsed.knownLine !== null;
    // Nothing has been spent before the first run: zero is a recorded fact,
    // not a guess, so the card may show it without a detailed report.
    if (!hasCost && (session === null || session === undefined
                     || (sessionCalls === 0 && nothingSpent(session)))) {
      const started = session !== null && session !== undefined;
      return zeroSummary(started ? 'Запросов ещё не было: сохранённых расходов нет.'
        : 'Запуск ещё не начинался: сохранённых расходов нет.', roleNames);
    }
    if (!hasCost) {
      return unknownSummary('Подробный отчёт о расходе не распознан; итог неполный.',
        roleNames, { calls: sessionCalls });
    }

    const rows = parsed.rows;
    let totalKnown = null, totalComplete = false, knownTotal = null;
    let estimatedRequests = null, reportedCalls = null, missing = null;
    if (parsed.totalLine !== null && parsed.totalLine.indexOf('полной суммы нет') < 0) {
      totalKnown = micros(parsed.totalLine);
      totalComplete = totalKnown !== null;
    }
    if (parsed.knownLine !== null) {
      knownTotal = micros(parsed.knownLine);
      const match = /известная часть:.*? за\s+(.+?)\s+из\s+(.+?)\s+запросов/.exec(parsed.knownLine);
      if (match) {
        estimatedRequests = count(match[1]);
        reportedCalls = count(match[2]);
      }
    }
    if (parsed.missingLine !== null) {
      missing = count(parsed.missingLine.replace(/^  запросов без оценки:\s*/, ''));
    }
    const rowsKnown = rows.reduce((sum, row) => sum + (row.known === null ? 0 : row.known), 0);
    const rowsKnownSeen = rows.some(row => row.known !== null);
    if (knownTotal === null && rowsKnownSeen) knownTotal = rowsKnown;
    if (reportedCalls === null) {
      reportedCalls = rows.reduce((sum, row) => sum + (typeof row.calls === 'number' ? row.calls : 0), 0);
    }
    if (rows.length > 0 || parsed.knownLine !== null) {
      if (typeof missing === 'number' && missing > 0) totalComplete = false;
      if (totalComplete && typeof reportedCalls === 'number' && typeof estimatedRequests === 'number'
          && estimatedRequests < reportedCalls) totalComplete = false;
    }
    if (parsed.totalLine === null && parsed.knownLine === null) {
      totalComplete = rows.length > 0 && rows.every(row => row.complete && row.known !== null);
    } else if (parsed.knownLine !== null && missing === null
               && (estimatedRequests === null || reportedCalls === null)) {
      totalComplete = false;
    }

    const roleState = () => ({ assigned: false, known: 0, knownSeen: false,
      incomplete: false, seen: false, calls: 0 });
    const roles = { lead: roleState(), worker: roleState() };
    roles.lead.assigned = Boolean(roleNames.lead);
    roles.worker.assigned = Boolean(roleNames.worker);
    const roleName = name => {
      if (leadName && name === leadName) return 'lead';
      if (workerName && name === workerName) return 'worker';
      if (!leadName && /-lead$/i.test(name)) return 'lead';
      if (!workerName && /-worker$/i.test(name)) return 'worker';
      return null;
    };
    let breakdownIncomplete = false, unallocatedKnown = 0, unallocatedUnknown = false;
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
        if (!row.complete || row.known === null) state.incomplete = true;
      } else {
        breakdownIncomplete = true;
        if (row.known === null) unallocatedUnknown = true;
        else unallocatedKnown += row.known;
        for (const role of rowRoles) roles[role].incomplete = true;
      }
    }
    const usageCalls = name => {
      if (!name || !usage) return null;
      const record = usage[name];
      return record && typeof record === 'object' && typeof record.calls === 'number' ? record.calls : null;
    };
    const expected = { lead: usageCalls(leadName), worker: usageCalls(workerName) };
    for (const key of ['lead', 'worker']) {
      const state = roles[key];
      if (!state.assigned) {
        state.known = null;
        state.incomplete = false;
        continue;
      }
      const calls = expected[key];
      if (!state.seen) {
        // No model row for this participant: zero is only known when the
        // session counters confirm that it made no request at all.
        if (typeof calls === 'number' && calls === 0) {
          state.known = 0;
          state.knownSeen = true;
        } else {
          state.known = null;
          // Its requests are missing from the breakdown, so the split between
          // the participants is unknown even when the total is confirmed.
          breakdownIncomplete = true;
        }
      } else {
        // A disagreement between the report and the saved counters leaves the
        // split uncertain; it is not hidden behind a confident per-role sum.
        if (typeof calls === 'number' && state.calls !== calls) {
          state.incomplete = true;
          breakdownIncomplete = true;
        }
        // A model row without an amount means "unknown", never "zero".
        if (!state.knownSeen) state.known = null;
      }
      if (state.known === null) state.incomplete = true;
    }
    const roleValue = state => {
      if (!state.assigned) return 'не назначен';
      if (state.known === null) return 'н/д';
      return usd(state.known) + (state.incomplete ? INCOMPLETE : '');
    };

    let amount = totalComplete ? totalKnown : knownTotal;
    if (amount === null && totalComplete) amount = knownTotal;
    const totalIncomplete = !totalComplete;
    let note;
    if (totalIncomplete) {
      note = amount === null ? 'Сумма неизвестна; итог неполный.'
        : 'Показана известная часть; итог неполный';
      if (typeof missing === 'number' && missing > 0) {
        note += `: ${missing}${typeof reportedCalls === 'number' ? ` из ${reportedCalls}` : ''} запросов без оценки`;
      } else {
        note += ': часть запросов без оценки';
      }
      note += '.';
    } else if (reportedCalls === 0) {
      note = 'Оценка по каталогу; запросов ещё не было.';
    } else if (breakdownIncomplete) {
      note = 'Оценка по каталогу; разбивка по участникам неполная, счёт провайдера не сверен.';
    } else {
      note = 'Оценка по каталогу; счёт провайдера не сверен.';
    }
    if (breakdownIncomplete && totalIncomplete) note += ' Часть расхода не разделена между участниками.';
    // The known total is either attributed to a participant or left unallocated;
    // no part of it may disappear from the breakdown.
    let unallocatedAmount = knownTotal === null ? unallocatedKnown : knownTotal;
    if (knownTotal !== null) {
      const allocated = (roles.lead.known === null ? 0 : roles.lead.known)
        + (roles.worker.known === null ? 0 : roles.worker.known);
      unallocatedAmount = Math.max(knownTotal - allocated, 0);
    }
    return {
      lead: { value: roleValue(roles.lead), amount: roles.lead.known,
        assigned: roles.lead.assigned, incomplete: roles.lead.assigned && roles.lead.incomplete },
      worker: { value: roleValue(roles.worker), amount: roles.worker.known,
        assigned: roles.worker.assigned, incomplete: roles.worker.assigned && roles.worker.incomplete },
      total: { value: (amount === null ? 'н/д' : usd(amount)) + (totalIncomplete ? INCOMPLETE : ''),
        amount, known: knownTotal, incomplete: totalIncomplete },
      note, complete: !totalIncomplete, breakdownComplete: !breakdownIncomplete,
      breakdownIncomplete,
      unallocated: { amount: unallocatedAmount, incomplete: unallocatedUnknown },
      calls: reportedCalls, missing, estimated: estimatedRequests,
    };
  }

  root.SymphonyApiTeamCosts = { summary, micros, usd, count };
})(globalThis);
