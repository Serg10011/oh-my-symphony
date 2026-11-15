"""Group review transition, fields, and candidate verification before common release."""

from pathlib import Path
import shutil
import subprocess

import pytest

import test_api_delivery as delivery_tests
import api_board
import api_group_release as group


board = delivery_tests.board
world = delivery_tests.world
visit = delivery_tests.fixtures.visit


def test_group_review_transitions_to_human_review_and_returns_fields(world):
    board, _ = world
    assert board.card()[0]["state"] == "Verify"

    value = board._load()
    candidate_commit = value["candidate"]["commit"]
    checks_note = "Target checks: unit + integration passed"

    initial_offer = board.show()["release"]
    assert initial_offer["stage"] == "publish-check"
    assert board.show()["cloud_check"]["status"] == "ready"

    session_snapshot = board.session_path.read_bytes()
    receipt = group.record_review(board, candidate_commit, checks_note)
    assert board.session_path.read_bytes() == session_snapshot

    assert receipt["status"] == "ready"
    assert receipt["identifier"] == board.identifier
    assert receipt["commit"] == candidate_commit
    assert receipt["checks"] == checks_note
    assert isinstance(receipt["checked_at"], str)

    assert board.card()[0]["state"] == "Human Review"

    view = board.show()
    release_info = view["release"]
    assert release_info["stage"] == "group-ready"
    assert release_info["commit"] == candidate_commit
    assert release_info["checks"] == checks_note
    assert release_info["checked_at"] == receipt["checked_at"]
    assert view["delivery_status"] == "group-ready"
    assert not view["accepted"]
    assert not view["delivered"]
    assert "cloud_check" not in view or view["cloud_check"] is None

    reopened = api_board.Board(board.settings, board.context)
    visit(reopened)
    assert reopened.card()[0]["state"] == "Human Review"
    assert reopened.show()["release"]["stage"] == "group-ready"

    again = group.record_review(board, candidate_commit, checks_note)
    assert again == receipt
    assert board.card()[0]["state"] == "Human Review"


def test_group_review_refuses_foreign_commit_without_changing_card_or_readiness(world):
    board, hub = world
    assert board.card()[0]["state"] == "Verify"

    cand = board._load()["candidate"]
    candidate_commit = cand["commit"]
    foreign_commit = cand["base_commit"]
    assert foreign_commit != candidate_commit
    checks_note = "Target checks: unit + integration passed"

    card_snapshot = board.card()
    board_snapshot = board.path.read_bytes()
    session_snapshot = board.session_path.read_bytes()

    with pytest.raises(group.ReleaseError, match="group_review_changed"):
        group.record_review(board, foreign_commit, checks_note)

    assert board.card() == card_snapshot
    assert board.path.read_bytes() == board_snapshot
    assert board.session_path.read_bytes() == session_snapshot
    view = board.show()
    assert view["release"]["stage"] == "publish-check"
    assert view.get("delivery_status") != "group-ready"

    receipt = group.record_review(board, candidate_commit, checks_note)
    assert board.card()[0]["state"] == "Human Review"
    assert receipt["status"] == "ready"
    view_ready = board.show()

    card_snapshot_after = board.card()
    board_snapshot_after = board.path.read_bytes()
    session_snapshot_after = board.session_path.read_bytes()

    with pytest.raises(group.ReleaseError, match="group_review_changed"):
        group.record_review(board, foreign_commit, checks_note)

    assert board.card() == card_snapshot_after
    assert board.path.read_bytes() == board_snapshot_after
    assert board.session_path.read_bytes() == session_snapshot_after
    assert board.show()["release"] == view_ready["release"]
    assert hub.pushes == [] and hub.pr is None


@pytest.mark.parametrize("invalid_checks", ["", "   ", "\t\n", "x" * 501, "bad\x00char"])
def test_group_review_validates_checks_note(world, invalid_checks):
    board, hub = world
    candidate_commit = board._load()["candidate"]["commit"]
    board_snapshot = board.path.read_bytes()
    session_snapshot = board.session_path.read_bytes()

    with pytest.raises(group.ReleaseError, match="group_review_checks_required"):
        group.record_review(board, candidate_commit, invalid_checks)

    assert board.card()[0]["state"] == "Verify"
    assert board.path.read_bytes() == board_snapshot
    assert board.session_path.read_bytes() == session_snapshot
    assert board.show()["release"]["stage"] == "publish-check"


def test_group_review_refuses_card_after_individual_check_started(world):
    board, hub = world
    initial = board.show()["release"]
    assert initial["stage"] == "publish-check"

    delivery_tests.action(board, hub, "publish-check")
    assert "release_check" in board._load()
    candidate_commit = board._load()["candidate"]["commit"]

    with pytest.raises(group.ReleaseError, match="group_review_individual_check_started"):
        group.record_review(board, candidate_commit, "Target checks: ok")


def test_individual_check_flow_unaffected_when_no_group_review(world):
    board, hub = world
    initial = board.show()["release"]
    assert initial["stage"] == "publish-check"
    assert board.show()["cloud_check"]["status"] == "ready"
    assert board.card()[0]["state"] == "Verify"

    delivery_tests.action(board, hub, "publish-check")
    value = board._load()
    assert "release_check" in value
    assert "group_review" not in value


def test_ui_group_ready_rendering(tmp_path):
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not available")

    js_path = Path(__file__).resolve().parents[2] / "src" / "symphony" / "web" / "static" / "api-team.js"
    assert js_path.is_file(), f"api-team.js not found at {js_path}"

    script = """
    const fs = require('fs');
    const assert = require('assert/strict');
    const vm = require('vm');

    const jsPath = process.argv[2];
    const jsCode = fs.readFileSync(jsPath, 'utf8');

    function el(tag, attrs = {}, children = []) {
      const node = {
        tagName: tag.toUpperCase(),
        attributes: { ...attrs },
        dataset: {},
        classList: {
          _classes: new Set(attrs.class ? attrs.class.split(' ') : []),
          toggle(cls, force) {
            if (force === undefined) {
              if (this._classes.has(cls)) this._classes.delete(cls); else this._classes.add(cls);
            } else if (force) {
              this._classes.add(cls);
            } else {
              this._classes.delete(cls);
            }
          },
          contains(cls) { return this._classes.has(cls); }
        },
        hidden: Boolean(attrs.hidden),
        scrollTop: 0,
        scrollLeft: 0,
        isConnected: true,
        children: [],
        appendChild(child) { this.children.push(child); return child; },
        append(...items) { this.children.push(...items); },
        prepend(...items) { this.children.unshift(...items); },
        replaceChildren(...items) { this.children = [...items]; },
        setAttribute(k, v) { this.attributes[k] = v; },
        removeAttribute(k) { delete this.attributes[k]; },
        addEventListener() {},
      };
      if (typeof children === 'string') {
        node.textContent = children;
      } else if (Array.isArray(children)) {
        node.children = [...children];
      }
      return node;
    }

    const sandbox = {
      globalThis: {}, Number, Date, JSON, Boolean,
      setInterval: () => 1, clearInterval: () => {}
    };
    sandbox.globalThis = sandbox;

    vm.createContext(sandbox);
    vm.runInContext(jsCode, sandbox);

    const SymphonyApiTeam = sandbox.SymphonyApiTeam;
    assert.ok(SymphonyApiTeam, "SymphonyApiTeam must be defined");

    let currentData = null;
    const request = async () => currentData;

    const groupCommit = 'abcdef0123456789abcdef0123456789abcdef01';
    const checksNote = 'Target <checks> & specialized integration tests passed';
    currentData = {
      status: 'result',
      board_stages: ['Todo', 'Verify', 'Human Review'],
      session: { execution: { tool_calls: 3 } },
      release: {
        stage: 'group-ready',
        commit: groupCommit,
        checks: checksNote,
        checked_at: '2026-11-15T10:00:00Z',
      },
      cloud_check: null,
    };

    const section = SymphonyApiTeam.mount({
      el,
      identifier: 'TEST-1',
      request,
      isActive: () => true,
      schedule: () => 1,
      unschedule: () => {},
    });

    function find(node, predicate) {
      const results = [];
      if (predicate(node)) results.push(node);
      for (const child of node.children || []) {
        results.push(...find(child, predicate));
      }
      return results;
    }

    (async () => {
      // Drain the initial load, including operation() releasing its busy guard.
      await new Promise(setImmediate);

      const statusNode = find(section, n => n.attributes && n.attributes['aria-live'] === 'polite')[0];
      assert.ok(statusNode, 'Status element found');
      assert.equal(statusNode.textContent, 'Готова к общему выпуску');

      const releaseBoxes = find(section, n => n.attributes && n.attributes['aria-label'] === 'Передача результата');
      assert.equal(releaseBoxes.length, 1);
      const releaseBox = releaseBoxes[0];
      assert.equal(releaseBox.hidden, false, 'Release box should be visible');

      const releaseStatus = find(releaseBox, n => n.attributes && n.attributes['role'] === 'status')[0];
      assert.ok(releaseStatus, 'Release status element found');
      assert.ok(releaseStatus.textContent.includes(groupCommit));
      assert.ok(releaseStatus.textContent.includes(checksNote));

      const releaseButtons = find(releaseBox, n => n.tagName === 'BUTTON');
      assert.ok(releaseButtons.every(button => button.hidden), 'No release action in group-ready');
      const releaseBtn = releaseButtons.find(button => button.textContent !== 'Изменения исходной версии');
      assert.ok(releaseBtn, 'Release action button found');
      assert.equal(releaseBtn.hidden, true, 'No acceptance button in group-ready');

      const releaseLink = find(releaseBox, n => n.tagName === 'A')[0];
      assert.equal(releaseLink.hidden, true, 'No PR link in group-ready');

      const cloudBoxes = find(section, n => n.attributes && n.attributes['aria-label'] === 'Проверка файлов на Linux');
      assert.equal(cloudBoxes.length, 1);
      assert.equal(cloudBoxes[0].hidden, true, 'Linux cloud check panel should be hidden in group-ready');

      const allButtons = find(section, n => n.tagName === 'BUTTON');
      const reviewBtn = allButtons.find(b => b.textContent === 'Посмотреть изменения файлов');
      assert.ok(reviewBtn, 'Review button found');
      assert.equal(reviewBtn.hidden, false, 'Review button must be visible for execution results');

      const refreshBtn = allButtons.find(b => b.textContent === 'Обновить');
      assert.ok(refreshBtn, 'Refresh button found');
      assert.equal(refreshBtn.hidden, false, 'Refresh remains visible');

      const regularCommit = '1111111111111111111111111111111111111111';
      currentData = {
        status: 'result',
        board_stages: ['Todo', 'Verify', 'Human Review'],
        session: { execution: { tool_calls: 3 } },
        release: {
          stage: 'publish-check',
          offer: 'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210',
          repo: 'our/repo',
          branch: 'check/TEST-1',
          base: 'our-main',
          commit: regularCommit,
          file_count: 2,
          files: ['src/a.py', 'src/b.py'],
        },
        cloud_check: {
          status: 'ready',
          commit: regularCommit,
          receipt: {
            attempt: 2,
            run: 888,
            status: 'passed',
            comparison: 'success',
          },
        },
      };

      assert.ok(refreshBtn.attributes.onClick, 'onClick handler exists');
      await refreshBtn.attributes.onClick();
      await Promise.resolve();
      await Promise.resolve();

      assert.equal(cloudBoxes[0].hidden, false, 'Cloud check panel visible for standard card');
      const cloudStatus = find(cloudBoxes[0], n => n.attributes && n.attributes['role'] === 'status')[0];
      assert.ok(cloudStatus.textContent.includes('Сохранённая попытка №2'));
      assert.ok(cloudStatus.textContent.includes('прошла'));

      const workflowLink = find(cloudBoxes[0], n => n.tagName === 'A')[0];
      assert.equal(workflowLink.hidden, false, 'Workflow link visible');
      assert.equal(workflowLink.href, 'https://github.com/Serg10011/oh-my-symphony/actions/runs/888/attempts/2');

      assert.equal(releaseBtn.hidden, false, 'Accept/publish button visible for standard card');
      assert.equal(releaseBtn.textContent, 'Опубликовать и проверить на GitHub');
      assert.equal(reviewBtn.hidden, false, 'Review button remains visible');
      assert.notEqual(statusNode.textContent, 'Готова к общему выпуску');

      currentData.release.stage = 'accept';
      await refreshBtn.attributes.onClick();
      assert.equal(releaseBtn.hidden, false);
      assert.equal(releaseBtn.textContent, 'Принять версию и открыть PR');
      assert.equal(cloudBoxes[0].hidden, false);
      assert.ok(cloudStatus.textContent.includes('Сохранённая попытка №2'));
      assert.equal(workflowLink.hidden, false);
    })().catch(err => {
      console.error(err);
      process.exit(1);
    });
    """
    runner = tmp_path / "test_ui.js"
    runner.write_text(script, encoding="utf-8")
    proc = subprocess.run([node_bin, str(runner), str(js_path)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"Node test failed: {proc.stderr}"
