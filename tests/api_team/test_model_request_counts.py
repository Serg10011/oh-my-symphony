"""OMS-24: exercise lead and worker request counts in the spend panel offline."""

from __future__ import annotations

import json
import subprocess
import unittest

from tests.api_team import STATIC, node_binary


_MOUNT = r"""
globalThis.SymphonyApiTeamCosts = {
  summary: text => text
    ? {
        lead: { value: '0,10 USD', incomplete: false },
        worker: { value: '0,03 USD', incomplete: false },
        total: { value: '0,13 USD', incomplete: false },
        note: '',
      }
    : null,
};
function el(tag, attrs = {}, content = []) {
  return {
    tag, attrs, isConnected: true, hidden: false, dataset: {},
    children: Array.isArray(content) ? content : [],
    textContent: typeof content === 'string' ? content : '',
    classList: { toggle() {} },
  };
}
const panels = cases.map(([session, report_text]) => SymphonyApiTeam.mount({
  el, identifier: 'OMS-24', isActive: () => true, schedule: () => 0, unschedule: () => {},
  request: async () => ({ status: session ? 'result' : 'unassigned', session, report_text }),
}));
const readPanels = () => JSON.stringify(panels.map(panel => {
  const error = panel.children.find(node => node.attrs.role === 'alert');
  if (error.textContent) throw new Error(error.textContent);
  const spend = panel.children.find(node => node.attrs['aria-label'] === 'Расход команды');
  if (spend.hidden) return null;
  const list = spend.children.find(node => node.tag === 'dl');
  const rows = {};
  for (let i = 0; i < list.children.length; i += 2) {
    rows[list.children[i].textContent] = list.children[i + 1]?.textContent;
  }
  return rows;
}));
"""


def _render_panels(cases: list[tuple[dict | None, str]]) -> list[dict[str, str] | None]:
    node = node_binary()
    if not node:
        raise unittest.SkipTest("Node runtime is required for the offline panel test")
    source = (STATIC / "api-team.js").read_text(encoding="utf-8")
    script = f"{source}\nconst cases = {json.dumps(cases)};\n{_MOUNT}"
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


class ModelRequestCountsTests(unittest.TestCase):
    def test_saved_counts_in_the_spend_panel(self):
        no_data = "Нет данных"
        assignment = {"lead_profile": "sol-lead", "worker_profile": "sol-worker"}
        profiles = {
            "lead": {"name": "sol-lead", "model": "gpt-6.1-sol"},
            "worker": {"name": "sol-worker", "model": "gpt-6.1-sol"},
        }
        usage = {"sol-lead": {"calls": 4}, "sol-worker": {"calls": 2}}
        calls_only = {**assignment, "usage": usage}
        # The aggregate and the model-keyed entry must not override the profile counters.
        with_costs = {
            **assignment,
            "profiles": profiles,
            "calls": 99,
            "usage": {
                **usage,
                "sol-lead": {"calls": 4, "input_tokens": 100, "cached_input_tokens": 50},
                "gpt-6.1-sol": {"calls": 99},
            },
            "parts": {"part1": {"attempts": 2}},
        }
        cases = [
            ("distinct profiles with the same model", with_costs, "Saved report", ("4", "2")),
            ("requests without tokens or cost summary", calls_only, "", ("4", "2")),
            (
                "known number of requests without token usage",
                {
                    **assignment,
                    "usage": {
                        "sol-lead": {"calls": 4, "unknown_usage_calls": 0},
                        "sol-worker": {"calls": 2, "unknown_usage_calls": 1},
                    },
                },
                "",
                ("4", "2 · без данных о токенах — 1"),
            ),
            (
                "impossible missing usage count",
                {**assignment, "usage": {**usage, "sol-worker": {"calls": 2, "unknown_usage_calls": 3}}},
                "",
                ("4", "2"),
            ),
            ("saved snapshot names", {"profiles": profiles, "usage": usage}, "", ("4", "2")),
            (
                "explicit solo with stale worker snapshot",
                {**calls_only, "profiles": profiles, "worker_profile": None}, "", ("4", "не назначен"),
            ),
            (
                "explicit zero and token-only usage",
                {**assignment, "usage": {"sol-lead": {"calls": 0}, "sol-worker": {"input_tokens": 9}}},
                "", ("0", no_data),
            ),
            (
                "invalid count and explicit zero",
                {**assignment, "usage": {"sol-lead": {"calls": -1}, "sol-worker": {"calls": 0}}},
                "", (no_data, "0"),
            ),
            ("assigned roles without usage", assignment, "", (no_data, no_data)),
            ("missing role metadata", {"usage": usage}, "", (no_data, no_data)),
            (
                "legacy snapshot with no assigned worker",
                {"profiles": {**profiles, "worker": None}, "usage": usage},
                "",
                ("4", "не назначен"),
            ),
            (
                "assigned worker snapshot without a lookup key",
                {"profiles": {"worker": {"model": "gpt-6.1-sol"}}, "usage": usage}, "", (no_data, no_data),
            ),
        ]
        panels = _render_panels([(None, ""), *((session, report) for _, session, report, _ in cases)])
        self.assertIsNone(panels[0])
        for (scenario, _, _, expected), panel in zip(cases, panels[1:], strict=True):
            with self.subTest(scenario=scenario):
                self.assertIsNotNone(panel)
                self.assertEqual((panel["Запросы ведущего"], panel["Запросы исполнителя"]), expected)

        billed = panels[1]
        self.assertEqual(list(billed)[:4], ["Ведущий", "Запросы ведущего", "Исполнитель", "Запросы исполнителя"])
        for label, value in {
            "Ведущий": "0,10 USD",
            "Исполнитель": "0,03 USD",
            "Итого": "0,13 USD",
            "Вход ведущего из кеша, токены": "50 (50%)",
            "Поручения исполнителю": "1",
            "Возвраты на доработку": "1",
        }.items():
            self.assertEqual(billed[label], value)
        self.assertEqual(panels[2]["Итого"], "н/д")


if __name__ == "__main__":
    unittest.main()
