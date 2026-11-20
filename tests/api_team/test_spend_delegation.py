"""OMS-23: exercise delegations and returns rows in the spend panel offline."""

from __future__ import annotations

import json
import subprocess
import unittest

from tests.api_team import STATIC, node_binary
from tests.api_team.reports import session_for, shared_model_report


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
  el, identifier: 'OMS-23', isActive: () => true, schedule: () => 0, unschedule: () => {},
  request: async () => ({ status: session ? 'result' : 'unassigned', session }),
}));
const readPanels = () => JSON.stringify(panels.map(panel => {
  const error = panel.children.find(node => node.attrs.role === 'alert');
  if (error.textContent) throw new Error(error.textContent);
  const spend = panel.children.find(node => node.attrs['aria-label'] === 'Расход команды');
  if (spend.hidden) return null;
  const list = spend.children.find(node => node.tag === 'dl');
  const delLabel = list.children.findIndex(node => node.textContent === 'Поручения исполнителю');
  if (delLabel < 0 || list.children[delLabel + 1]?.tag !== 'dd') throw new Error('Missing delegations row');
  const retLabel = list.children.findIndex(node => node.textContent === 'Возвраты на доработку');
  if (retLabel < 0 || list.children[retLabel + 1]?.tag !== 'dd') throw new Error('Missing returns row');
  return {
    delegations: list.children[delLabel + 1].textContent,
    returns: list.children[retLabel + 1].textContent,
  };
}));
"""


def _render_panels(sessions: list[dict | None]) -> list[dict | None]:
    node = node_binary()
    if not node:
        raise unittest.SkipTest('Node runtime is required for the offline panel test')
    source = (STATIC / 'api-team.js').read_text(encoding='utf-8')
    script = f"{source}\nconst sessions = {json.dumps(sessions)};\n{_MOUNT}"
    runner = """
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
setImmediate(() => process.stdout.write(vm.runInContext('readPanels()', context)));
"""
    return json.loads(subprocess.run([node, '-e', runner], input=script,
                                     text=True, capture_output=True, timeout=10, check=True).stdout)


class SpendDelegationTests(unittest.TestCase):
    def test_delegations_and_returns_counters_in_the_spend_panel(self):
        no_data = "Нет данных"
        cases = [
            (
                "main scenario: several parts and multiple returns",
                {"setup": {"attempts": 1}, "impl": {"attempts": 3}},
                "2", "2",
            ),
            ("empty dictionary", {}, "0", "0"),
            ("first transfer -> 0 returns", {"setup": {"attempts": 1}}, "1", "0"),
            ("old log without parts", None, no_data, no_data),
            ("incomplete attempts", {"setup": {"attempts": 1}, "impl": {}}, "2", no_data),
            ("broken collection", "invalid", no_data, no_data),
        ]

        sessions = [None]
        for _, parts_val, _, _ in cases:
            session = session_for(shared_model_report())
            if parts_val is not None:
                session["parts"] = parts_val
            sessions.append(session)

        results = _render_panels(sessions)
        self.assertIsNone(results[0], "Spend panel stays hidden without a session")
        for (scenario, _, exp_del, exp_ret), actual in zip(cases, results[1:], strict=True):
            with self.subTest(scenario=scenario):
                self.assertIsNotNone(actual, f"Spend panel should be visible for {scenario}")
                self.assertEqual(actual["delegations"], exp_del, f"Delegations mismatch for {scenario}")
                self.assertEqual(actual["returns"], exp_ret, f"Returns mismatch for {scenario}")


if __name__ == "__main__":
    unittest.main()
