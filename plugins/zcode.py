# -*- coding: utf-8 -*-
"""插件：ZCode（精确计数）。数据源：~/.zcode/cli/db/db.sqlite 的 model_usage 表。

model_usage 每行 = 一次模型调用的真实计数（input/output/reasoning/
cache_creation/cache_read + computed_total_tokens），status 分
completed / error / cancelled / running。

聚合口径（与 codex / kimi 一致）：
    total_tokens = input + output + cache_read + cache_write
注意：zcode 自带的 computed_total_tokens = input+output（不含 cache_read），
不能直接当 total 用，否则会漏掉缓存读取量。

- sessions 行粒度：session × model（真实会话标题/目录 join session 表）
- daily 行粒度：按 started_at 的本地日期拆分，供趋势曲线用
- 整体重查（sqlite 开销小，不走 need/mark 增量）；engine 会按 source_file
  先删后插，天然防残留。
- error / cancelled 行当前 token 均为 0（以 computed_total_tokens>0 过滤），
  若未来出现"有 token 的失败行"会被漏掉——届时放开该过滤即可。
"""
import os
from engine.common import ro_connect

KEY = 'zcode'
NAME = 'ZCode'
ESTIMATE = False
WATCH_PATHS = ['%USERPROFILE%\\.zcode\\cli\\db']

_DB = os.path.expanduser('~/.zcode/cli/db/db.sqlite')

# 只统计成功完成的调用；db 表不存在/结构变更时抛错（进 coverage 报警），
# 不静默返回空——静默返回空正是本插件此前"用了却没扫出来"的原因。
_SQL_BY_SESSION = '''
    select m.session_id, m.model_id, m.provider_id,
           sum(m.input_tokens), sum(m.output_tokens),
           sum(m.cache_read_input_tokens), sum(m.cache_creation_input_tokens),
           min(m.started_at), max(m.started_at), count(*)
    from model_usage m
    where m.status = 'completed' and m.computed_total_tokens > 0
    group by m.session_id, m.model_id
'''

_SQL_BY_DAY = '''
    select strftime('%Y-%m-%d', m.started_at / 1000, 'unixepoch', 'localtime'),
           sum(m.input_tokens + m.output_tokens
               + m.cache_read_input_tokens + m.cache_creation_input_tokens)
    from model_usage m
    where m.status = 'completed' and m.computed_total_tokens > 0
    group by 1
'''


def scan(full, need, mark):
    if not os.path.exists(_DB):
        return []
    con = ro_connect(_DB)
    if con is None:
        raise RuntimeError(f'ZCode 用量库打不开（可能被 ZCode CLI 独占锁定）：{_DB}')
    try:
        rows = con.execute(_SQL_BY_SESSION).fetchall()
        day_rows = con.execute(_SQL_BY_DAY).fetchall()
        titles = {}
        try:
            for sid, title, directory in con.execute(
                    'select id, title, directory from session'):
                titles[sid] = (title or '', directory or '')
        except Exception:
            pass          # session 表缺失/结构变更不阻断用量统计
    finally:
        con.close()

    sessions = []
    for (sid, model, provider, inp, outp, cr, cw,
         t_min, t_max, _n) in rows:
        total = int(inp) + int(outp) + int(cr) + int(cw)
        if total <= 0:
            continue
        title, directory = titles.get(sid, ('', ''))
        sessions.append({
            'agent': KEY,
            'session_id': f'zcode-{sid}-{model}',
            'title': title or f'ZCode {sid[:20]}',
            'cwd': directory,
            'model': model or '',
            'provider': provider or '',
            'created_at': int(t_min or 0),
            'last_activity_at': int(t_max or 0),
            'input_tokens': int(inp), 'output_tokens': int(outp),
            'cache_read_tokens': int(cr), 'cache_write_tokens': int(cw),
            'total_tokens': total, 'cost': None, 'est': 0,
            'source_file': _DB,
        })

    daily_rows = []
    for day, tokens in day_rows:
        tokens = int(tokens or 0)
        if not day or tokens < 1000:
            continue
        daily_rows.append({
            'agent': KEY, 'day': day, 'source_file': _DB,
            'tokens': tokens, 'est': 0,
        })

    return {'sessions': sessions, 'daily': daily_rows, 'daily_files': [_DB]}
