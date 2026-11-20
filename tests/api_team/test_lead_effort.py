"""OMS-25: exercise lead effort and escalation in the spend panel offline."""

from __future__ import annotations

import json
import subprocess
import unittest

from tests.api_team import STATIC, node_binary


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
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}
const panels = cases.map(([session, profiles]) => SymphonyApiTeam.mount({
  el, identifier: 'OMS-25', isActive: () => true, schedule: () => 0, unschedule: () => {},
  request: async () => ({ status: session ? 'result' : 'unassigned', session, profiles }),
}));
const readPanels = () => JSON.stringify(panels.map(panel => {
  const error = panel.children.find(node => node.attrs.role === 'alert');
  if (error.textContent) throw new Error(error.textContent);
  const spend = panel.children.find(node => node.attrs['aria-label'] === 'Расход команды');
  if (spend.hidden) return null;
  const list = spend.children.find(node => node.tag === 'dl');
  const labelIndex = list.children.findIndex(node => node.textContent === 'Усилие ведущего');
  if (labelIndex < 0 || list.children[labelIndex + 1]?.tag !== 'dd') throw new Error('Missing lead effort row');
  return list.children[labelIndex + 1].textContent;
}));
"""


def _render_panels(cases: list[tuple[dict | None, dict | None]]) -> list[str | None]:
    node = node_binary()
    if not node:
        raise unittest.SkipTest("Node runtime is required for the offline panel test")
    source = (STATIC / "api-team.js").read_text(encoding="utf-8")
    script = "\n".join([source, f"const cases = {json.dumps(cases)};", _MOUNT])
    runner = """
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
setImmediate(() => process.stdout.write(vm.runInContext('readPanels()', context)));
"""
    return json.loads(
        subprocess.run(
            [node, "-e", runner],
            input=script,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
    )


class LeadEffortSpendPanelTests(unittest.TestCase):
    def test_lead_effort_and_escalation_in_the_spend_panel(self):
        no_data = "Нет данных"
        table = [
            (
                "boundary with differing total vs lead request counts",
                {
                    "calls": 3,
                    "lead_profile": "sol-lead",
                    "usage": {"sol-lead": {"calls": 1}, "sol-worker": {"calls": 2}},
                    "lead_effort": {
                        "initial": "high",
                        "current": "max",
                        "events": [
                            {
                                "from": "high",
                                "to": "max",
                                "reason": "Сложная подзадача",
                                "effective_call_index": 3,
                            }
                        ],
                    },
                },
                None,
                "high → max · с запроса 3: Сложная подзадача",
            ),
            (
                "future request marked as appointed escalation",
                {
                    "calls": 2,
                    "lead_effort": {
                        "initial": "high",
                        "current": "max",
                        "events": [
                            {
                                "from": "high",
                                "to": "max",
                                "reason": "Предстоит глубокий анализ",
                                "effective_call_index": 3,
                            }
                        ],
                    },
                },
                None,
                "high → max · назначенное повышение с запроса 3: Предстоит глубокий анализ",
            ),
            (
                "empty history shows initial effort despite different current and profile",
                {
                    "calls": 3,
                    "lead_effort": {
                        "initial": "medium",
                        "current": "max",
                        "events": [],
                    },
                    "profiles": {"lead": {"effort": "high"}},
                },
                None,
                "medium",
            ),
            (
                "old card fallback to saved profiles.lead effort",
                {
                    "calls": 3,
                    "profiles": {"lead": {"name": "sol-lead", "effort": "high"}},
                },
                None,
                "high",
            ),
            (
                "top-level card profile does not leak without saved effort in session",
                {"calls": 2},
                {"lead": {"name": "sol-lead", "effort": "max"}},
                no_data,
            ),
            ("missing effort everywhere", {}, None, no_data),
        ]

        cases = [(None, None), *((session, profiles) for _, session, profiles, _ in table)]
        results = _render_panels(cases)
        self.assertIsNone(results[0], "Spend panel stays hidden without a session")
        for (scenario, _, _, expected), actual in zip(table, results[1:], strict=True):
            with self.subTest(scenario=scenario):
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
