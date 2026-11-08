"""Exercise workflow routing expressions with real trigger contexts, offline.

The workflow subset here is scalar YAML, so no extra YAML dependency is needed.
"""
import ast
from datetime import datetime, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pytest

WORKFLOW = Path(__file__).parents[1] / '.github/workflows/personal-wechat.yml'


def text():
    return WORKFLOW.read_text()


def stage_expression():
    return re.search(r'^\s+STAGE: \$\{\{ (.+) \}\}$', text(), re.M).group(1)


def delivery_condition():
    step = text().split('      - name: Deliver short guide after checking the published page\n', 1)[1]
    return re.search(r'^        if: (.+)$', step, re.M).group(1)


def evaluate(expression, schedule='', stage='', bootstrap=False):
    """Evaluate only the workflow's small Boolean routing expression grammar."""
    expression = expression.replace('github.event.schedule', 'schedule').replace('inputs.stage', 'stage').replace('inputs.bootstrap', 'bootstrap')
    expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('false', 'False')
    tree = ast.parse(expression, mode='eval')
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Constant)
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return eval(compile(tree, '<workflow routing>', 'eval'), {'__builtins__': {}}, {'schedule': schedule, 'stage': stage, 'bootstrap': bootstrap})


def test_real_schedules_convert_to_shanghai_and_route_without_accidental_generation():
    schedules = re.findall(r"^    - cron: '([^']+)'", text(), re.M)
    actual = {}
    for schedule in schedules:
        minute, hour, day, month, weekday = schedule.split()
        assert (day, month, weekday) == ('*', '*', '*')
        for value in minute.split(','):
            local = datetime(2026, 11, 8, int(hour), int(value), tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
            actual[local.strftime('%H:%M')] = (evaluate(stage_expression(), schedule), bool(evaluate(delivery_condition(), schedule)))
    assert actual == {
        '08:07': ('generate', False), '09:00': ('run', True),
        '09:17': ('deliver', True), '09:37': ('deliver', True), '09:57': ('deliver', True),
    }


@pytest.mark.parametrize('stage,should_send', [('run', True), ('deliver', True), ('generate', False), ('format', False)])
def test_manual_modes_preserve_no_send_for_generate_and_format(stage, should_send):
    assert evaluate(stage_expression(), stage=stage) == stage
    assert bool(evaluate(delivery_condition(), stage=stage)) == should_send


def test_bootstrap_requires_explicit_manual_opt_in_and_state_retention_is_private_only():
    content = text()
    bootstrap = content.split('      bootstrap:\n', 1)[1].split('permissions:', 1)[0]
    assert re.search(r'^        default: false$', bootstrap, re.M)
    expression = re.search(r'^          BOOTSTRAP: \$\{\{ (.+) \}\}$', content, re.M).group(1)
    assert evaluate(expression) is False
    assert evaluate(expression, bootstrap=True) is True
    state = content.split('      - name: Preserve rotating credentials and delivery ledger encrypted\n', 1)[1]
    assert re.search(r'^          retention-days: 7$', state, re.M)
    state_name = re.search(r'^          name: (.+)$', state, re.M).group(1)
    assert state_name == 'personal-horizon-state-${{ github.run_id }}-${{ github.run_attempt }}'
    assert 'overwrite:' not in state
    pages = content.split('      - name: Upload public report HTML only\n', 1)[1].split('      - name:', 1)[0]
    assert 'retention-days:' not in pages
    assert 'path: data/pages-site' in pages


def test_failed_prepare_or_publish_cannot_be_bypassed_by_delivery_condition():
    # GitHub's implicit success() guard applies because the send step has no
    # always()/failure() override; ordering keeps it behind verified publication.
    content = text()
    assert content.index('id: prepare') < content.index('uses: actions/deploy-pages@v4') < content.index('name: Deliver short guide')
    assert 'always(' not in delivery_condition() and 'failure(' not in delivery_condition()
    assert 'actions_run.py deliver --send-only' in content
    assert 'cancel-in-progress: false' in content


def test_pages_upload_and_deploy_share_attempt_specific_artifact_identity():
    content = text()
    upload = content.split('      - name: Upload public report HTML only\n', 1)[1].split('      - name:', 1)[0]
    deploy = content.split('      - name: Publish mobile detail pages\n', 1)[1].split('      - name:', 1)[0]
    uploaded_name = re.search(r'^          name: (.+)$', upload, re.M).group(1)
    deployed_name = re.search(r'^          artifact_name: (.+)$', deploy, re.M).group(1)
    assert uploaded_name == deployed_name
    assert '${{ github.run_id }}' in uploaded_name
    assert '${{ github.run_attempt }}' in uploaded_name
    # Same run's rerun must not collide with its earlier published artifact.
    def render(run, attempt):
        return uploaded_name.replace('${{ github.run_id }}', str(run)).replace('${{ github.run_attempt }}', str(attempt))
    assert len({render(100, 1), render(100, 2), render(101, 1)}) == 3
