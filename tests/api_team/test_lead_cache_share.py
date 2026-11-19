"""OMS-22: exercise the spend panel offline with saved-session fixtures."""

from __future__ import annotations

import ctypes as ct
import json
import subprocess
import sys
import unittest

from tests.api_team import STATIC, node_binary
from tests.api_team.reports import session_for, shared_model_report


# Only the element factory and the unrelated money summary are doubled. The
# component reads the fixture through its normal request/render path.
_MOUNT = r"""
const spendLine = { value: 'н/д', incomplete: true };
globalThis.SymphonyApiTeamCosts = {
  summary: () => ({ lead: spendLine, worker: spendLine, total: spendLine, note: '' }),
};
function el(tag, attrs = {}, content = []) {
  return {
    tag, attrs, isConnected: true, hidden: false, dataset: {},
    children: Array.isArray(content) ? content : [],
    textContent: typeof content === 'string' ? content : '',
    classList: { toggle() {} },
  };
}
const panels = sessions.map(session => SymphonyApiTeam.mount({
  el, identifier: 'OMS-22', isActive: () => true, schedule: () => 0, unschedule: () => {},
  request: async () => ({ status: session ? 'result' : 'unassigned', session }),
}));
const readPanels = () => JSON.stringify(panels.map(panel => {
  const error = panel.children.find(node => node.attrs.role === 'alert');
  if (error.textContent) throw new Error(error.textContent);
  const spend = panel.children.find(node => node.attrs['aria-label'] === 'Расход команды');
  if (spend.hidden) return null;
  const list = spend.children.find(node => node.tag === 'dl');
  const label = list.children.findIndex(node => node.textContent === 'Вход ведущего из кеша, токены');
  if (label < 0 || list.children[label + 1]?.tag !== 'dd') throw new Error('Missing cache row');
  return list.children[label + 1].textContent;
}));
"""


def _render_with_jsc(script: str) -> str:
    """Run with the system JS runtime on Macs where Node is not installed."""
    try:
        jsc = ct.CDLL('/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore')
    except OSError as exc:
        raise unittest.SkipTest(f'No offline JavaScript runtime: {exc}') from exc

    ptr = ct.c_void_p
    signatures = {
        'JSGlobalContextCreate': (ptr, [ptr]),
        'JSGlobalContextRelease': (None, [ptr]),
        'JSStringCreateWithUTF8CString': (ptr, [ct.c_char_p]),
        'JSStringRelease': (None, [ptr]),
        'JSEvaluateScript': (ptr, [ptr, ptr, ptr, ptr, ct.c_int, ct.POINTER(ptr)]),
        'JSValueToStringCopy': (ptr, [ptr, ptr, ct.POINTER(ptr)]),
        'JSStringGetMaximumUTF8CStringSize': (ct.c_size_t, [ptr]),
        'JSStringGetUTF8CString': (ct.c_size_t, [ptr, ptr, ct.c_size_t]),
    }
    for name, (result_type, arg_types) in signatures.items():
        function = getattr(jsc, name)
        function.restype, function.argtypes = result_type, arg_types

    context = jsc.JSGlobalContextCreate(None)

    def evaluate(source: str) -> str:
        script_ref = jsc.JSStringCreateWithUTF8CString(source.encode('utf-8'))
        exception = ptr()
        try:
            result = jsc.JSEvaluateScript(context, script_ref, None, None, 1, ct.byref(exception))
        finally:
            jsc.JSStringRelease(script_ref)
        text = jsc.JSValueToStringCopy(context, exception.value or result, None)
        try:
            buf = ct.create_string_buffer(jsc.JSStringGetMaximumUTF8CStringSize(text))
            jsc.JSStringGetUTF8CString(text, buf, len(buf))
            message = buf.value.decode('utf-8')
        finally:
            jsc.JSStringRelease(text)
        if exception.value:
            raise AssertionError(message)
        return message

    try:
        evaluate(script)
        # Read in a separate evaluation so the initial Promise-based load has run.
        return evaluate('readPanels()')
    finally:
        jsc.JSGlobalContextRelease(context)


def _render_panels(sessions: list[dict | None]) -> list[str | None]:
    source = (STATIC / 'api-team.js').read_text(encoding='utf-8')
    script = f'{source}\nconst sessions = {json.dumps(sessions)};\n{_MOUNT}'
    if node := node_binary():
        runner = """
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
setImmediate(() => process.stdout.write(vm.runInContext('readPanels()', context)));
"""
        return json.loads(subprocess.run([node, '-e', runner], input=script,
                                         text=True, capture_output=True, timeout=10, check=True).stdout)
    if sys.platform == 'darwin':
        return json.loads(_render_with_jsc(script))
    raise unittest.SkipTest('Node is required for the offline API-team panel test')


class LeadCacheShareTests(unittest.TestCase):
    def test_saved_lead_counters_in_the_spend_panel(self):
        no_data = 'Нет данных'
        cases = [
            ('known share', {'input_tokens': 30, 'cached_input_tokens': 20}, '20 (66,67%)'),
            ('small nonzero share', {'input_tokens': 1_000_000, 'cached_input_tokens': 1}, '1 (<0,01%)'),
            ('confirmed zero cache', {'input_tokens': 30, 'cached_input_tokens': 0}, '0 (0%)'),
            ('no cache count', {'input_tokens': 30}, no_data),
            ('unknown cache count', {'input_tokens': 30, 'cached_input_tokens': None}, no_data),
            ('no input count', {'cached_input_tokens': 0}, no_data),
            ('unknown input count', {'input_tokens': None, 'cached_input_tokens': 20}, no_data),
            ('empty input', {'input_tokens': 0, 'cached_input_tokens': 0}, no_data),
            ('inconsistent counters', {'input_tokens': 30, 'cached_input_tokens': 31}, no_data),
            ('only worker counters', {}, no_data),
        ]
        sessions = [None]
        for _, counters, _ in cases:
            session = session_for(shared_model_report())
            session['usage'][session['lead_profile']].update(counters)
            # Distinct profiles may use the same model; do not include worker tokens.
            session['usage'][session['worker_profile']].update(input_tokens=100, cached_input_tokens=90)
            sessions.append(session)
        results = _render_panels(sessions)
        self.assertIsNone(results[0], 'Spend panel stays hidden without a session')
        for (scenario, _, expected), actual in zip(cases, results[1:], strict=True):
            with self.subTest(scenario=scenario):
                self.assertEqual(actual, expected)
