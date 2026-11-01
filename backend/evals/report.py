"""Offline, evidence-bound human review. Never calls a model or grades by keywords alone."""
import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

try:
    from .dataset import build
except ImportError:
    from dataset import build

GROUPS = {
    'facts_preferences': '单事实与偏好', 'events_projects': '跨会话事件与项目',
    'updates_temporal': '更新与时间查询', 'subjects_quotes': '主体、引用与假设',
    'unknown_disputed': '未知与争议信息', 'deletion': '删除与派生清理',
    'restart_redelivery': '重启与重投递',
}
FIRST_FIVE = tuple(GROUPS)[:5]
SCOPE = 'retrieval_lifecycle_on_confirmed_fixtures'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def dataset_digest():
    return digest(build())


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('评测文件超过 16 MiB，请先核对文件范围')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError('评测 JSON 包含重复字段')
            out[key] = value
        return out
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def atomic_write(path, value, replace=False):
    """Write a complete snapshot; never truncate an earlier checkpoint on interruption."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace: raise ValueError('输出已存在，请使用新的文件名')
    descriptor, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            # Hard-link publication is atomic and refuses an existing destination.
            os.link(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def save_json(path, value, replace=False):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', replace)


def validate_results(data):
    if (not isinstance(data, dict) or data.get('schema_version') != 2 or data.get('scope') != SCOPE or
        data.get('dataset_sha256') != dataset_digest() or not isinstance(data.get('run_id'), str) or
        not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', data['run_id']) or type(data.get('planned_count')) is not int or
        not 1 <= data['planned_count'] <= 150 or not isinstance(data.get('results'), list)):
        raise ValueError('需要当前数据集的 v2 原始结果；旧报告不能冒充新版证据')
    cases = {row['id']: row for row in build()[:data['planned_count']]}
    seen = set()
    for row in data['results']:
        if (not isinstance(row, dict) or row.get('id') not in cases or row['id'] in seen or
            row.get('group') != cases[row['id']]['group'] or
            row.get('status') not in ('not_run', 'running', 'needs_review', 'error', 'interrupted') or
            ('answer' in row and (not isinstance(row['answer'], str) or len(row['answer']) > 200000))):
            raise ValueError('结果含重复、未知题号、类别失配或无效状态')
        seen.add(row['id'])
        if any(key in row and not isinstance(row[key], dict) for key in ('source_check', 'cleanup')):
            raise ValueError('来源检查或清理证据必须为对象')
        if row['status'] == 'needs_review' and (any(type(row.get('source_check', {}).get(key)) is not bool
            for key in ('passed', 'expected_retrieved')) or type(row.get('cleanup', {}).get('passed')) is not bool):
            raise ValueError('已执行结果必须保留来源与清理的明确检查结果')
    if seen != set(cases): raise ValueError('结果必须保留全部计划题号；未运行题不能省略')
    return cases


def template(data):
    cases = validate_results(data)
    return {'schema_version': 1, 'run_id': data['run_id'], 'dataset_sha256': dataset_digest(),
        'reviewer': '', 'reviews': [{'id': row['id'], 'result_sha256': digest(row),
            'question': cases[row['id']]['query'], 'answer': row.get('answer', ''),
            'correct': None, 'unsupported_assertion': None, 'blocking_issue': None,
            'reason': ''} for row in data['results']]}


def summarize(data, reviews):
    cases = validate_results(data)
    if (not isinstance(reviews, dict) or reviews.get('schema_version') != 1 or
        reviews.get('run_id') != data['run_id'] or reviews.get('dataset_sha256') != dataset_digest() or
        not isinstance(reviews.get('reviews'), list) or not isinstance(reviews.get('reviewer'), str)):
        raise ValueError('复核文件不属于本次评测')
    rows = {row['id']: row for row in data['results']}
    judged = {}
    for review in reviews['reviews']:
        if not isinstance(review, dict) or review.get('id') not in rows or review['id'] in judged:
            raise ValueError('复核含重复或未知题号')
        row = rows[review['id']]
        if review.get('result_sha256') != digest(row):
            raise ValueError('原始结果已变化，旧复核失效；请重新生成复核模板')
        if review.get('answer') != row.get('answer', '') or review.get('question') != cases[row['id']]['query']:
            raise ValueError('复核展示的题目或答案与原始结果不一致')
        for key in ('correct', 'unsupported_assertion', 'blocking_issue'):
            if review.get(key) is not None and type(review[key]) is not bool:
                raise ValueError('复核判断只能为 true、false 或 null，不能用字符串或数字代替')
        if not isinstance(review.get('reason'), str) or len(review['reason']) > 4000:
            raise ValueError('复核理由无效或过长')
        complete = (all(type(review.get(key)) is bool for key in
            ('correct', 'unsupported_assertion', 'blocking_issue')) and bool(review['reason'].strip()) and
            bool(reviews['reviewer'].strip()))
        judged[row['id']] = review | {'complete': complete}
    groups = []
    accepted = {}
    blockers = []
    pending = []
    evidence_missing = []
    for identity, row in rows.items():
        review = judged.get(identity, {})
        # Legacy/partial execution, failed cleanup or missing source checks never count as a valid answer.
        evidence = (row.get('status') == 'needs_review' and bool(row.get('answer', '').strip()) and
            row.get('source_check', {}).get('passed') is True and
            row.get('cleanup', {}).get('passed') is True)
        if not evidence: evidence_missing.append(identity)
        if not review.get('complete'): pending.append(identity)
        if review.get('blocking_issue') is True or row.get('source_check', {}).get('passed') is False:
            blockers.append(identity)
        accepted[identity] = bool(evidence and row.get('source_check', {}).get('expected_retrieved') is not False and
            review.get('complete') and review.get('correct') is True and review.get('unsupported_assertion') is False)
    for group, name in GROUPS.items():
        ids = [identity for identity, case in cases.items() if case['group'] == group]
        groups.append({'id': group, 'name': name, 'planned': len(ids),
            'reviewed': sum(bool(judged.get(identity, {}).get('complete')) for identity in ids),
            'correct': sum(accepted[identity] for identity in ids),
            'total': sum(case['group'] == group for case in build())})
    complete = data.get('status') == 'awaiting_review' and data['planned_count'] == 150 and not pending and not evidence_missing
    first = [identity for identity, case in cases.items() if case['group'] in FIRST_FIVE]
    unknown = [identity for identity, case in cases.items() if case['group'] == 'unknown_disputed']
    correct = sum(accepted[identity] for identity in first)
    unsupported = sum(judged.get(identity, {}).get('unsupported_assertion') is True for identity in unknown)
    answer_gate = complete and correct >= 108
    unknown_gate = complete and unsupported <= 1
    # This runner cannot prove cache/in-flight deletion or restored-backup isolation.
    lifecycle = [row for row in data['results'] if row['group'] in ('deletion', 'restart_redelivery')]
    lifecycle_reviewed = complete and all(accepted[row['id']] for row in lifecycle)
    status = ('blocked' if blockers else 'incomplete' if not complete else
        'answers_passed_system_checks_pending' if answer_gate and unknown_gate and lifecycle_reviewed else 'failed')
    return {'schema_version': 1, 'run_id': data['run_id'], 'dataset_sha256': dataset_digest(),
        'results_sha256': digest(data), 'reviews_sha256': digest(reviews), 'scope': SCOPE,
        'status': status, 'full_product_verified': False, 'groups': groups,
        'first_five': {'correct': correct, 'total': 120, 'rate': correct / 120, 'passed': answer_gate},
        'unknown_disputed': {'unsupported': unsupported, 'total': 20, 'rate': unsupported / 20,
            'passed': unknown_gate, 'review_complete': complete},
        'lifecycle_answers_reviewed': lifecycle_reviewed, 'system_lifecycle_gate': 'not_covered',
        'extraction_quality': 'not_covered', 'pending_reviews': pending,
        'missing_execution_evidence': evidence_missing, 'blocking_cases': blockers,
        'limits': ['仅测用户确认夹具的检索和回答，不测自动抽取质量。',
            '关键词只作复核提示，不能自动判定事实、主体和无依据断言。',
            '人工重启确认不等于自动观测服务重启；仍需独立运行证据。',
            '删除缓存、在途任务、重试和旧备份恢复须独立系统验收，不能由自然语言回答代替。']}


def markdown(report):
    names = {'blocked': '存在阻断问题', 'incomplete': '证据或复核未齐',
        'failed': '回答门槛未通过', 'answers_passed_system_checks_pending': '回答门槛通过，系统断言待验收'}
    lines = ['# 中文记忆评测报告', '', '状态：' + names[report['status']], '',
        '运行编号：`' + report['run_id'].replace('`', '') + '`', '',
        '仅覆盖用户确认夹具的检索与回答；不证明完整产品、自动抽取或备份恢复已经验收。', '',
        '| 类别 | 计划 / 基线 | 已复核 | 有依据的正确回答 |', '|---|---:|---:|---:|']
    lines.extend(f"| {row['name']} | {row['planned']} / {row['total']} | {row['reviewed']} | {row['correct']} |" for row in report['groups'])
    lines += ['', f"前五类：{report['first_five']['correct']} / 120，目标至少 108 题。",
        f"未知争议：已确认无依据断言 {report['unknown_disputed']['unsupported']} / 20，目标最多 1 题；复核未齐时不能据此认为断言率达标。",
        f"待复核 {len(report['pending_reviews'])} 题，执行证据不足 {len(report['missing_execution_evidence'])} 题，阻断 {len(report['blocking_cases'])} 题。", '',
        '删除与恢复的确定性系统断言：未被本报告覆盖。', '', '适用边界：', '']
    lines += ['- ' + value for value in report['limits']]
    lines += ['', '原始结果摘要：`' + report['results_sha256'] + '`',
        '人工复核摘要：`' + report['reviews_sha256'] + '`', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线复核中文记忆评测，不连接服务器或模型')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='生成未判分的人工复核模板')
    prepare.add_argument('results'); prepare.add_argument('--output', required=True)
    aggregate = commands.add_parser('summarize', help='校验复核与证据并输出报告')
    aggregate.add_argument('results'); aggregate.add_argument('--reviews', required=True)
    aggregate.add_argument('--json', required=True); aggregate.add_argument('--markdown', required=True)
    args = parser.parse_args(argv)
    try:
        data = read_json(args.results)
        if args.command == 'prepare':
            save_json(args.output, template(data))
            print('复核模板已生成；null 表示尚未判分。')
            return 0
        report = summarize(data, read_json(args.reviews))
        if Path(args.json).resolve() == Path(args.markdown).resolve(): raise ValueError('两个输出路径必须不同')
        if Path(args.json).exists() or Path(args.markdown).exists(): raise ValueError('报告输出已存在，请使用新的文件名')
        save_json(args.json, report)
        atomic_write(args.markdown, markdown(report))
        print(report['status'])
        return 0 if report['status'] == 'answers_passed_system_checks_pending' else 2
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(1, '评测文件、绑定或输出无效；请核对格式、摘要与目标文件，不覆盖已有证据。\n')


if __name__ == '__main__': raise SystemExit(main())
