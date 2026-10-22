"""Claude Code 세션 기록(.jsonl)에서 토큰·시간 낭비 «관찰» 을 집계하는 읽기 전용 분석기.

    python session_waste_report.py [--days 14] [--session <id>] [--top 10] [--json]
                                   [--min-chars 20000] [--dir <프로젝트 세션 폴더>] [--out <reports 아래 폴더>]

계약(깨면 안 된다):
- 이 도구의 어떤 수치도 «검증된 사실» 이 아니다 — 관찰 보조일 뿐 의사결정 근거가 아니다(글자 수이지 토큰이 아니고,
  서브에이전트·첨부·thinking 은 보지 않는다).
- 기록 파일은 읽기만 한다. 입력 값은 길이·해시(내부 키)로만 바꾸고 출력하지 않는다.
- 출력 문자열은 전부 «고정 목록» 에서만 온다(명령 이름·하위 명령·플래그·도구 이름·트리거). 목록 밖은 (기타)/(플래그).
  사용자가 쓴 낱말은 어떤 모양이든 출력되지 않는다. 세션은 순번(s01…)으로만 부른다.
- 보고서는 이 스크립트 옆 reports 폴더 아래에만 쓴다. 파일 하나를 못 읽으면 그 파일만 «증명 불가» 로 센다.
- 예외 메시지는 찍지 않는다(예외 종류 이름만).
"""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(HERE, 'reports')          # report output root
DEFAULT_DIR = os.path.join(os.path.expanduser('~'), '.claude', 'projects')   # pass --dir <one project folder>; CLAUDE_SESSION_DIR also works
WRITE_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}
TOOLS = {'Bash', 'PowerShell', 'Read', 'Grep', 'Glob', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Task',
         'WebFetch', 'WebSearch', 'TodoWrite', 'Skill'}
CMDS = set('git python python3 py node npm pip cat type head tail grep rg find ls dir sed awk sort wc curl wget '
           'ssh scp schtasks taskkill tasklist powershell pwsh cmd bash sh pytest '
           'safepush mkdir cp mv rm del copy move echo sleep start where which get-content '
           'get-childitem select-string select-object where-object sort-object measure-object get-process '
           'get-date test-path cd export set'.split())
SUBS = {'git': set('status diff log show add commit push pull fetch rebase merge checkout restore reset stash '
                   'branch tag worktree rev-parse ls-files blame grep clean safepush'.split()),
        'schtasks': {'/query', '/create', '/run', '/end', '/delete', '/change'}}
LONG_FLAGS = set('--stat --oneline --name-only --no-pager --max-count --help --version --force --hard --cached '
                 '--staged --recursive --verbose --quiet --dry-run --json'.split())
PS_FLAGS = {'-first', '-last', '-totalcount', '-recurse', '-path', '-filter', '-include', '-exclude'}
TRIGGERS = {'auto', 'manual'}
_ASSIGN = re.compile(r'^[A-Za-z_]\w*=')
_SEG = re.compile(r'\|\||&&|;|\||\r?\n')


def tool_label(name):
    if name in TOOLS:
        return name
    return 'mcp' if isinstance(name, str) and name.startswith('mcp__') else '(기타)'


def _cmd_name(tok):
    """실행 파일 이름 — 고정 목록에 있을 때만. 따옴표·백틱·$()·괄호로 감싸졌으면 (기타)."""
    if not tok or tok[0] in '"\'`$(@{%&':
        return '(기타)'
    t = tok.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1].lower()
    t = re.sub(r'\.(exe|cmd|bat|ps1)$', '', t)
    return t if t in CMDS else '(기타)'


def _flag(tok):
    if tok.startswith('--'):
        n = re.split(r'[=:]', tok, 1)[0].lower()
        return n if n in LONG_FLAGS else '(플래그)'
    n = re.split(r'[=:]', tok, 1)[0].lower()
    if n in PS_FLAGS:
        return n
    m = re.match(r'^-([A-Za-z])', tok)
    return '-' + m.group(1) if m else '(플래그)'


def cmd_label(cmd):
    """Bash/PowerShell 명령 → «고정 목록 명령 이름 [하위 명령] [플래그…]». 그 밖 값은 전부 버린다."""
    toks = None
    for seg in _SEG.split(str(cmd or '')):
        t = seg.split()
        while t and _ASSIGN.match(t[0]):
            t = t[1:]
        if t and _cmd_name(t[0]) != 'cd':
            toks = t
            break
    if not toks:
        return '(빈 명령)'
    name = _cmd_name(toks[0])
    out = [name]
    rest = toks[1:]
    if name in SUBS:
        pos = next((x for x in rest if not x.startswith('-')), None)
        if pos is not None:
            out.append(pos.lower() if pos.lower() in SUBS[name] else '(기타)')
    fl = []
    for x in rest:
        if x.startswith('-'):
            f = _flag(x)
            if f not in fl:
                fl.append(f)
        if len(fl) >= 6:
            break
    return ' '.join(out + fl)


def cmd_kind(cmd):
    lab = cmd_label(cmd)
    if lab == '(빈 명령)':
        return lab
    p = lab.split(' ')
    return ' '.join(p[:2]) if p[0] in SUBS and len(p) > 1 and not p[1].startswith('-') else p[0]


def _h(s):
    return hashlib.sha1(str(s).encode('utf-8', 'replace')).hexdigest()[:16]


def _pkey(p):
    return _h(str(p).replace('/', '\\').lower())


def text_len(c):
    """tool_result content → (글자수, 이미지 포함 여부). 알 수 없는 모양이면 (None, False)."""
    if isinstance(c, str):
        return len(c), False
    if isinstance(c, list):
        n, img = 0, False
        for b in c:
            if isinstance(b, dict) and b.get('type') == 'text':
                t = b.get('text')
                n += len(t) if isinstance(t, str) else 0
            elif isinstance(b, dict) and b.get('type') == 'image':
                img = True
            elif isinstance(b, str):
                n += len(b)
            else:
                return None, img
        return n, img
    return (0, False) if c is None else (None, False)


def tool_rec(name, inp, side):
    d = inp if isinstance(inp, dict) else {}
    fp = d.get('file_path') if isinstance(d.get('file_path'), str) else None
    is_cmd = name in ('Bash', 'PowerShell')
    full = str(d.get('command', '')) if is_cmd else json.dumps(inp, sort_keys=True, default=str)
    rec = {'name': name, 'tool': tool_label(name), 'side': side,
           'nsig': name + ':' + re.sub(r'\s+', ' ', re.sub(r'\d+', '#', full)).strip()[:200],
           'file': _pkey(fp) if fp else None, 'sig': None, 'size': None, 'err': None, 'have': False, 'img': False,
           'label': tool_label(name), 'kind': tool_label(name)}
    if is_cmd:
        rec['label'] = cmd_label(full)
        rec['kind'] = cmd_kind(full)
    elif name == 'Read':
        rec['sig'] = _h('%s|%s|%s|%s' % (_pkey(fp), d.get('offset'), d.get('limit'), d.get('pages')))
    return rec


class Sess:
    def __init__(self):
        self.calls, self.compacts = [], []
        self.bad = self.valid = self.filtered = self.summaries = 0
        self.failed = False
        self.unprov = Counter()


def _consume(s, by_id, line, cutoff):
    try:
        o = json.loads(line)
    except Exception:
        s.bad += 1
        return
    if not isinstance(o, dict):
        s.bad += 1
        return
    s.valid += 1
    ts = o.get('timestamp')
    if cutoff and isinstance(ts, str):
        try:
            if dt.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() < cutoff:
                s.filtered += 1
                return
        except Exception:
            pass
    if o.get('type') == 'system' and o.get('subtype') == 'compact_boundary':
        m = o.get('compactMetadata') if isinstance(o.get('compactMetadata'), dict) else {}
        pt, tr = m.get('preTokens'), m.get('trigger')
        if pt is not None and not (type(pt) is int and 0 <= pt <= 10 ** 9):
            s.unprov['pretokens_out_of_range'] += 1
            pt = None
        s.compacts.append({'pre_tokens': pt,
                           'trigger': tr if tr in TRIGGERS else '(기타)', 'at': len(s.calls)})
        return
    if o.get('isCompactSummary'):
        s.summaries += 1
        return
    msg = o.get('message')
    c = msg.get('content') if isinstance(msg, dict) else None
    if not isinstance(c, list):
        return
    for b in c:
        if not isinstance(b, dict):
            continue
        if b.get('type') == 'tool_use' and o.get('type') == 'assistant':
            if not isinstance(b.get('id'), str) or not isinstance(b.get('name'), str):
                s.bad += 1
                continue
            rec = tool_rec(b['name'], b.get('input'), bool(o.get('isSidechain')))
            s.calls.append(rec)
            by_id[b['id']] = rec
        elif b.get('type') == 'tool_result':
            tid = b.get('tool_use_id')
            if not isinstance(tid, str):
                s.bad += 1
                continue
            r = by_id.get(tid)
            if r is None:
                s.unprov['result_without_use'] += 1
                continue
            r['size'], r['img'] = text_len(b.get('content'))
            r['have'] = True
            r['err'] = b['is_error'] if isinstance(b.get('is_error'), bool) else None


def parse_file(path, cutoff):
    s, by_id = Sess(), {}
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        _consume(s, by_id, line, cutoff)
                    except Exception:
                        s.bad += 1
    except Exception:
        s.failed = True
    if s.valid == 0 and s.bad > 0:      # 전부 깨짐(UTF-16·NUL·바이너리) — 증명 불가
        s.failed = True
    for r in s.calls:
        if not r['have']:
            s.unprov['use_without_result'] += 1
        elif r['size'] is None:
            s.unprov['size_unknown'] += 1
        elif r['err'] is None:
            s.unprov['error_flag_missing'] += 1
        if r['img']:
            s.unprov['image_size_not_counted'] += 1
    return s


def analyze(sessions, min_chars):
    P = defaultdict(lambda: {'count': 0, 'chars': 0, 'sessions': set(), 'max_chain': None})
    tool_huge, kind_huge, unprov = Counter(), Counter(), Counter()
    tot = {'calls': 0, 'side': 0, 'huge': 0, 'dup': 0, 'fail': 0, 'chain': 0}
    comp_rows, per = [], []
    for n, s in enumerate(sessions, 1):
        sid = 's%02d' % n
        comp_at = {c['at'] for c in s.compacts}
        seen = {}
        huge = dup = fail = 0
        main = []
        for idx, r in enumerate(s.calls):
            if idx in comp_at:
                seen.clear()
            if r['side']:
                tot['side'] += 1
                continue
            tot['calls'] += 1
            sz = r['size'] or 0
            if sz >= min_chars:
                huge += 1
                tool_huge[r['tool']] += 1
                kind_huge[r['kind']] += 1
                p = P[('거대 출력', r['label'])]
                p['count'] += 1
                p['chars'] += sz
                p['sessions'].add(sid)
            main.append(r)
            if r['name'] in WRITE_TOOLS and r['file']:
                for k in [k for k, f in seen.items() if f == r['file']]:
                    del seen[k]
            elif r['name'] == 'Read' and r['sig']:
                if r['sig'] in seen:
                    dup += 1
                    p = P[('같은 Read 반복', 'Read')]
                    p['count'] += 1
                    p['chars'] += sz
                    p['sessions'].add(sid)
                else:
                    seen[r['sig']] = r['file']
        chain = []

        def flush():
            nonlocal fail
            if len(chain) >= 2:
                fail += len(chain)
                p = P[('반복 실패 연쇄', chain[0]['label'])]
                p['count'] += len(chain)
                p['chars'] += sum(c['size'] or 0 for c in chain)
                p['sessions'].add(sid)
                p['max_chain'] = max(p['max_chain'] or 0, len(chain))
                tot['chain'] = max(tot['chain'], len(chain))
        for r in main:
            if r['err'] is True:
                if chain and chain[-1]['name'] == r['name'] and (
                        chain[-1]['nsig'] == r['nsig'] or
                        difflib.SequenceMatcher(None, chain[-1]['nsig'], r['nsig']).ratio() >= 0.85):
                    chain.append(r)
                else:
                    flush()
                    chain = [r]
            else:
                flush()
                chain = []
        flush()
        prev = 0
        for c in s.compacts:
            seg = [x['size'] for x in s.calls[prev:c['at']] if not x['side'] and x['size'] is not None]
            comp_rows.append({'session': sid, 'pre_tokens': c['pre_tokens'], 'trigger': c['trigger'],
                              'calls_before': len(seg),
                              'avg_result_chars': round(sum(seg) / len(seg)) if seg else None,
                              'max_result_chars': max(seg) if seg else None})
            prev = c['at']
        tot['huge'] += huge
        tot['dup'] += dup
        tot['fail'] += fail
        unprov.update(s.unprov)
        unprov['bad_lines'] += s.bad
        unprov['filtered_lines'] += s.filtered
        per.append({'session': sid, 'calls': len(s.calls), 'huge': huge, 'dup': dup, 'fail_in_chains': fail,
                    'compactions': len(s.compacts) or s.summaries})
    pats = [{'kind': k, 'label': lab, 'count': v['count'], 'chars': v['chars'], 'sessions': len(v['sessions']),
             'max_chain': v['max_chain']} for (k, lab), v in P.items()]
    pats.sort(key=lambda x: (x['chars'], x['count']), reverse=True)
    pre = [c['pre_tokens'] for c in comp_rows if c['pre_tokens']]
    summary = {'sessions': len(sessions), 'tool_calls': tot['calls'], 'sidechain_calls_excluded': tot['side'],
               'huge_outputs': tot['huge'], 'min_chars': min_chars, 'duplicate_read_calls': tot['dup'],
               'calls_in_fail_chains': tot['fail'], 'longest_fail_chain': tot['chain'],
               'compactions': sum(x['compactions'] for x in per),
               'sessions_with_compaction': sum(1 for x in per if x['compactions']),
               'avg_pre_compact_tokens': round(sum(pre) / len(pre)) if pre else None,
               'huge_by_tool': dict(tool_huge.most_common(15)), 'huge_by_kind': dict(kind_huge.most_common(15))}
    return {'summary': summary, 'patterns': pats, 'compactions': comp_rows,
            'per_session': sorted(per, key=lambda x: (x['huge'], x['calls']), reverse=True),
            'unprovable': dict(unprov)}


def to_md(rep, top):
    s = rep['summary']
    L = ['# 세션 낭비 관찰 (%s) — 검증된 사실이 아니다' % rep['generated'], '',
         '세션 %d · 도구 호출 %d(서브에이전트 %d 제외) · 거대 출력(≥%d자) %d · 같은 Read 반복 %d · 실패 연쇄 호출 %d'
         '(최장 %d) · 압축 %d회(%d 세션)' % (s['sessions'], s['tool_calls'], s['sidechain_calls_excluded'],
                                             s['min_chars'], s['huge_outputs'], s['duplicate_read_calls'],
                                             s['calls_in_fail_chains'], s['longest_fail_chain'], s['compactions'],
                                             s['sessions_with_compaction']),
         '증명 불가 %d개 파일 · 시간 필터로 버린 줄 %d' % (rep['unprovable_files'], rep['unprovable'].get('filtered_lines', 0)),
         '', '## 상위 패턴', '', '| # | 종류 | 대상 | 횟수 | 글자 | 세션 |', '|---|---|---|---|---|---|']
    for i, p in enumerate(rep['patterns'][:top], 1):
        L.append('| %d | %s | %s | %d | %d | %d |' % (i, p['kind'], p['label'], p['count'], p['chars'], p['sessions']))
    L += ['', '## 거대 출력 도구별', '', json.dumps(s['huge_by_tool'], ensure_ascii=False),
          '', '명령 종류별: ' + json.dumps(s['huge_by_kind'], ensure_ascii=False), '', '## 압축 직전 구간', '']
    for c in rep['compactions'][:top * 3]:
        L.append('- %s pre=%s trigger=%s 직전호출=%d 평균출력=%s 최대=%s' % (
            c['session'], c['pre_tokens'], c['trigger'], c['calls_before'], c['avg_result_chars'],
            c['max_result_chars']))
    return '\n'.join(L + ['', '## 증명 불가', '', json.dumps(rep['unprovable'], ensure_ascii=False)]) + '\n'


def _fail(msg):
    print('오류: ' + msg, file=sys.stderr)
    return 2


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=float, default=14)
    ap.add_argument('--session')
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--min-chars', type=int, default=20000)
    ap.add_argument('--dir')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    if a.session is not None and not a.session.strip():
        return _fail('--session 이 비어 있다')
    d = a.dir or os.environ.get('CLAUDE_SESSION_DIR') or DEFAULT_DIR
    base_logs = os.path.realpath(LOGS_DIR)
    ro = os.path.realpath(a.out or LOGS_DIR)
    try:
        inside = os.path.commonpath([base_logs, ro]) == base_logs
    except ValueError:
        inside = False
    if not inside:
        return _fail('--out 은 reports 폴더 아래만 허용된다')
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith('.jsonl'))
    except Exception:
        return _fail('세션 폴더를 읽을 수 없다(없음·권한)')
    cutoff = None if a.session else (dt.datetime.now() - dt.timedelta(days=a.days)).timestamp()
    bad_files, files = 0, []
    for n in names:
        f = os.path.join(d, n)
        if a.session:
            if n.startswith(a.session):
                files.append(f)
            continue
        try:
            if os.path.getmtime(f) >= cutoff:
                files.append(f)
        except Exception:
            bad_files += 1
    sessions = []
    for f in files:
        s = parse_file(f, cutoff)
        if s.failed:
            bad_files += 1
        else:
            sessions.append(s)
    if not sessions:
        return _fail('분석 대상 없음 — 정상이 아님(폴더 비었음·기간 내 파일 0·전부 증명 불가 %d개)' % bad_files)
    rep = analyze(sessions, a.min_chars)
    rep['unprovable_files'] = bad_files
    rep['generated'] = dt.date.today().isoformat()
    rep['patterns'] = rep['patterns'][:max(a.top, 50)]
    os.makedirs(ro, exist_ok=True)
    stem = os.path.join(ro, 'session_waste_%s' % rep['generated'])
    md = to_md(rep, a.top)
    with open(stem + '.json', 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    with open(stem + '.md', 'w', encoding='utf-8') as f:
        f.write(md)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else md)
    return 0


def main(argv=None):
    try:
        return run(argv)
    except SystemExit:
        raise
    except Exception as e:      # 메시지는 값을 담을 수 있어 찍지 않는다
        print('오류: 예외 ' + type(e).__name__, file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
