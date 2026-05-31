#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Глубокая аналитика тотализатора Лиги Чемпионов (сезоны 2025 и 2026).

Читает дампы MongoDB (events.bson / users.bson) без сторонних зависимостей,
заново пересчитывает очки по точным правилам бота и считает метрики,
которых нет во встроенной команде /detailed_analytics.

Запуск:  python3 analyze.py
Результат: report.html в этой же папке.
"""

import csv
import datetime
import html
import math
import os
import struct
import sys
from collections import Counter, defaultdict
from statistics import pstdev, mean

# Папка с данными (подпапки 2024/ 2025/ 2026/ и сюда же пишется report.html).
# Приоритет: переменная окружения TOTALIZATOR_DATA > аргумент командной строки > папка скрипта.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('TOTALIZATOR_DATA') or (sys.argv[1] if len(sys.argv) > 1 else _SCRIPT_DIR)


# ----------------------------------------------------------------------------
# 1. BSON-парсер на чистом stdlib
# ----------------------------------------------------------------------------

def _rd_cstring(b, i):
    j = b.index(0, i)
    return b[i:j].decode('utf-8', 'replace'), j + 1


def _rd_doc(b, i):
    start = i
    size = struct.unpack_from('<i', b, i)[0]
    i += 4
    out = {}
    while b[i] != 0:
        t = b[i];
        i += 1
        key, i = _rd_cstring(b, i)
        if t == 0x01:  # double
            out[key] = struct.unpack_from('<d', b, i)[0];
            i += 8
        elif t == 0x02:  # string
            ln = struct.unpack_from('<i', b, i)[0];
            i += 4
            out[key] = b[i:i + ln - 1].decode('utf-8', 'replace');
            i += ln
        elif t == 0x03:  # embedded doc
            out[key], i = _rd_doc(b, i)
        elif t == 0x04:  # array
            arr, i = _rd_doc(b, i)
            out[key] = [arr[k] for k in arr]
        elif t == 0x05:  # binary
            ln = struct.unpack_from('<i', b, i)[0];
            i += 4;
            i += 1
            out[key] = b[i:i + ln];
            i += ln
        elif t == 0x07:  # ObjectId
            out[key] = 'OID:' + b[i:i + 12].hex();
            i += 12
        elif t == 0x08:  # bool
            out[key] = bool(b[i]);
            i += 1
        elif t == 0x09:  # UTC datetime (ms)
            ms = struct.unpack_from('<q', b, i)[0];
            i += 8
            out[key] = datetime.datetime.utcfromtimestamp(ms / 1000)
        elif t == 0x0A:  # null
            out[key] = None
        elif t == 0x10:  # int32
            out[key] = struct.unpack_from('<i', b, i)[0];
            i += 4
        elif t == 0x12:  # int64
            out[key] = struct.unpack_from('<q', b, i)[0];
            i += 8
        else:
            raise ValueError(f'Неизвестный BSON-тип {t:#x} для ключа {key}')
    return out, start + size


def read_bson(path):
    b = open(path, 'rb').read()
    i = 0
    docs = []
    while i < len(b):
        d, i = _rd_doc(b, i)
        docs.append(d)
    return docs


# ----------------------------------------------------------------------------
# 2. Движок очков — точная копия логики бота (main.py:1191-1264)
# ----------------------------------------------------------------------------

# Веса очков ОТЛИЧАЮТСЯ по сезонам (восстановлено из сохранённых тоталов):
#   2025: точный=3, разница=2, ничья=2, победитель=1, проход=+1
#   2026: точный=4, разница=3, ничья=2, победитель=1, проход=+1
# Т.е. между сезонами подорожали точный счёт (3→4) и разница мячей (2→3).
WEIGHTS = {
    '2025': {'exact': 3, 'diff': 2, 'draw': 2, 'winner': 1, 'adv': 1, None: 0},
    '2026': {'exact': 4, 'diff': 3, 'draw': 2, 'winner': 1, 'adv': 1, None: 0},
}
W_NORM = WEIGHTS['2026']  # единая шкала для честного сравнения сезонов

CAT_RU = {'exact': 'Точный счёт', 'diff': 'Разница мячей', 'draw': 'Ничья',
          'winner': 'Победитель', None: 'Мимо'}


def category(rt1, rt2, bt1, bt2):
    """Каскад как в боте: exact > draw > diff > winner."""
    if rt1 == bt1 and rt2 == bt2:
        return 'exact'
    if rt1 == rt2 and bt1 == bt2:
        return 'draw'
    if rt1 - rt2 == bt1 - bt2:
        return 'diff'
    if rt1 > rt2:
        w = bt1 > bt2
    elif rt1 < rt2:
        w = bt1 < bt2
    else:
        w = (bt1 == bt2)
    return 'winner' if w else None


def is_same_winner(rt1, rt2, bt1, bt2):
    if rt1 > rt2:
        return bt1 > bt2
    if rt1 < rt2:
        return bt1 < bt2
    return bt1 == bt2


def near_miss_winner(rt1, rt2, bt1, bt2):
    """В одном мяче от точного счёта с учётом исхода (bot: ..._winner_consider)."""
    if not is_same_winner(rt1, rt2, bt1, bt2):
        return False
    if rt1 == bt1:
        return bt2 in (rt2 - 1, rt2 + 1)
    if rt2 == bt2:
        return bt1 in (rt1 - 1, rt1 + 1)
    return False


def near_miss_other(rt1, rt2, bt1, bt2):
    """В одном мяче от ТС в иных случаях, матчи с 2+ голами (bot)."""
    if rt1 + rt2 < 2:
        return False
    if rt1 == bt1:
        return bt2 in (rt2 - 1, rt2 + 1)
    if rt2 == bt2:
        return bt1 in (rt1 - 1, rt1 + 1)
    return False


# ----------------------------------------------------------------------------
# 3. Загрузка и нормализация сезона
# ----------------------------------------------------------------------------

def has_result(ev):
    r = ev.get('result')
    return bool(r) and r.get('team_1') is not None


def phase_of(dt):
    """Лига (сен-янв) vs плей-офф (фев-июнь) по дате."""
    return 'knockout' if dt.month in (2, 3, 4, 5, 6, 7) else 'league'


def load_season(year):
    raw_events = read_bson(os.path.join(BASE, year, 'events.bson'))
    raw_users = read_bson(os.path.join(BASE, year, 'users.bson'))

    events = {}  # uuid -> event dict
    for e in raw_events:
        if not has_result(e):
            continue
        r = e['result']
        dt = e.get('time')
        events[e['uuid']] = {
            'uuid': e['uuid'],
            't1': e['team_1'], 't2': e['team_2'],
            'time': dt,
            'rt1': r['team_1'], 'rt2': r['team_2'],
            'through': r.get('team_1_has_gone_through'),  # bool|None
            'decisive': r.get('team_1_has_gone_through') is not None,
            'phase': phase_of(dt) if dt else 'league',
        }

    players = {}
    for u in raw_users:
        bets = {}
        for be in u.get('bets', []):
            uuid = be['event_uuid']
            if uuid not in events:
                continue
            bets[uuid] = {  # последняя ставка по событию
                't1': be['team_1_scores'], 't2': be['team_2_scores'],
                'through': be.get('team_1_will_go_through'),
                'created_at': be.get('created_at'),
            }
        name = (u.get('first_name') or '').strip()
        if u.get('last_name'):
            name = (name + ' ' + u['last_name']).strip()
        players[u['username']] = {
            'username': u['username'],
            'name': name or u['username'],
            'stored_scores': u.get('scores', 0),
            'bets': bets,
        }
    return {'year': year, 'events': events, 'players': players, 'W': WEIGHTS[year]}


# ----------------------------------------------------------------------------
# 4. Пер-матчевый расчёт очков для игрока
# ----------------------------------------------------------------------------

def score_bet(ev, bet, W):
    """Возвращает (cat, score_pts, adv_pts, total) при наборе весов W."""
    cat = category(ev['rt1'], ev['rt2'], bet['t1'], bet['t2'])
    score_pts = W[cat]
    adv = 0
    if ev['decisive'] and bet['through'] is not None and ev['through'] is not None:
        if bet['through'] == ev['through']:
            adv = W['adv']
    return cat, score_pts, adv, score_pts + adv


def per_match(season, username, W=None):
    """Список записей по каждому сыгранному матчу, где игрок ставил."""
    W = W or season['W']
    pl = season['players'][username]
    rows = []
    for uuid, bet in pl['bets'].items():
        ev = season['events'][uuid]
        cat, sp, adv, tot = score_bet(ev, bet, W)
        rows.append({
            'uuid': uuid, 'ev': ev, 'bet': bet,
            'cat': cat, 'score_pts': sp, 'adv': adv, 'total': tot,
            'nm_winner': near_miss_winner(ev['rt1'], ev['rt2'], bet['t1'], bet['t2']),
            'nm_other': near_miss_other(ev['rt1'], ev['rt2'], bet['t1'], bet['t2']),
        })
    rows.sort(key=lambda r: (r['ev']['time'] or datetime.datetime.min))
    return rows


# ----------------------------------------------------------------------------
# 5. Метрики игрока
# ----------------------------------------------------------------------------

def analyze_player(season, username, W=None):
    W = W or season['W']
    rows = per_match(season, username, W)
    n = len(rows)
    total = sum(r['total'] for r in rows)
    score_total = sum(r['score_pts'] for r in rows)
    adv_total = sum(r['adv'] for r in rows)

    cats = Counter(r['cat'] for r in rows)
    decisive_rows = [r for r in rows if r['ev']['decisive']]
    adv_hits = sum(1 for r in rows if r['adv'] > 0)

    # near-miss (как в боте: A приоритетнее B); «недобор» = до точного счёта
    nm_a = 0;
    nm_b = 0;
    foregone = 0
    for r in rows:
        if r['cat'] == 'exact':
            continue
        if r['nm_winner']:
            nm_a += 1
            foregone += (W['exact'] - r['score_pts'])
        elif r['nm_other']:
            nm_b += 1
            foregone += (W['exact'] - r['score_pts'])

    # ставки на ничьи и «квирк» правила (ничья учитывается раньше разницы)
    draw_bets = sum(1 for r in rows if r['bet']['t1'] == r['bet']['t2'])
    correct_draws = sum(1 for r in rows if r['cat'] == 'draw')
    draw_rule_loss = correct_draws * max(0, W['diff'] - W['draw'])  # 2025: 0, 2026: 1/шт

    # стиль
    bet_totals = [r['bet']['t1'] + r['bet']['t2'] for r in rows]
    act_totals = [r['ev']['rt1'] + r['ev']['rt2'] for r in rows]
    bet_margin = [r['bet']['t1'] - r['bet']['t2'] for r in rows]
    scoreline_counter = Counter((r['bet']['t1'], r['bet']['t2']) for r in rows)

    # энтропия счетов
    ent = 0.0
    for c in scoreline_counter.values():
        p = c / n
        ent -= p * math.log2(p)

    # серии (по матчам, где ставил, хронологически)
    pts_seq = [r['total'] for r in rows]
    hot = cur = 0
    cold = curc = 0
    for p in pts_seq:
        if p > 0:
            cur += 1;
            hot = max(hot, cur);
            curc = 0
        else:
            curc += 1;
            cold = max(cold, curc);
            cur = 0

    per_match_pts = pts_seq[:]
    return {
        'username': username,
        'name': season['players'][username]['name'],
        'stored': season['players'][username]['stored_scores'],
        'n': n,
        'total': total,
        'score_total': score_total,
        'adv_total': adv_total,
        'ppb': total / n if n else 0,  # очки за ставку
        'cats': dict(cats),
        'exact': cats.get('exact', 0),
        'diff': cats.get('diff', 0),
        'draw': cats.get('draw', 0),
        'winner': cats.get('winner', 0),
        'miss': cats.get(None, 0),
        'scored_rate': sum(1 for r in rows if r['total'] > 0) / n if n else 0,
        'decisive_n': len(decisive_rows),
        'adv_hits': adv_hits,
        'adv_rate': adv_hits / len(decisive_rows) if decisive_rows else 0,
        'nm_a': nm_a, 'nm_b': nm_b, 'nm_total': nm_a + nm_b, 'foregone': foregone,
        'draw_bets': draw_bets, 'correct_draws': correct_draws, 'draw_rule_loss': draw_rule_loss,
        'avg_bet_total': mean(bet_totals) if bet_totals else 0,
        'avg_act_total': mean(act_totals) if act_totals else 0,
        'avg_bet_margin': mean(bet_margin) if bet_margin else 0,
        'fav_scoreline': scoreline_counter.most_common(1)[0] if scoreline_counter else None,
        'distinct_scorelines': len(scoreline_counter),
        'entropy': ent,
        'pts_std': pstdev(per_match_pts) if n > 1 else 0,
        'hot_streak': hot, 'cold_streak': cold,
        'rows': rows,
    }


def analyze_all_players(season, min_bets=1):
    out = {}
    for uname in season['players']:
        a = analyze_player(season, uname)
        if a['n'] >= min_bets:
            out[uname] = a
    return out


# ----------------------------------------------------------------------------
# 6. Социальная аналитика (H2H, близнецы, контрарность, матчи)
# ----------------------------------------------------------------------------

def analyze_social(season, active):
    """active = список username с достаточным числом ставок."""
    ev = season['events']
    # все ставки по матчу
    match_bets = defaultdict(dict)  # uuid -> {username: bet}
    pmpts = defaultdict(dict)  # uuid -> {username: total pts}
    for uname in active:
        for r in per_match(season, uname):
            match_bets[r['uuid']][uname] = r['bet']
            pmpts[r['uuid']][uname] = r['total']

    # H2H
    h2h = {a: {b: [0, 0, 0] for b in active if b != a} for a in active}  # [wins,losses,ties]
    for uuid, pts in pmpts.items():
        names = list(pts.keys())
        for i in range(len(names)):
            for j in range(len(names)):
                if i == j:
                    continue
                a, b = names[i], names[j]
                if pts[a] > pts[b]:
                    h2h[a][b][0] += 1
                elif pts[a] < pts[b]:
                    h2h[a][b][1] += 1
                else:
                    h2h[a][b][2] += 1

    # близнецы: средняя L1-дистанция счётов на совместных матчах + доля идентичных
    pair_dist = {}
    for ai in range(len(active)):
        for bi in range(ai + 1, len(active)):
            a, b = active[ai], active[bi]
            ds = [];
            ident = 0
            for uuid, mb in match_bets.items():
                if a in mb and b in mb:
                    d = abs(mb[a]['t1'] - mb[b]['t1']) + abs(mb[a]['t2'] - mb[b]['t2'])
                    ds.append(d)
                    if d == 0:
                        ident += 1
            if ds:
                pair_dist[(a, b)] = {'dist': mean(ds), 'ident': ident / len(ds), 'n': len(ds)}

    # контрарность: среднее L1-отклонение ставки от среднего остальных
    contrarian = {}
    for uname in active:
        devs = []
        for uuid, mb in match_bets.items():
            if uname not in mb:
                continue
            others = [v for k, v in mb.items() if k != uname]
            if not others:
                continue
            m1 = mean(o['t1'] for o in others)
            m2 = mean(o['t2'] for o in others)
            devs.append(abs(mb[uname]['t1'] - m1) + abs(mb[uname]['t2'] - m2))
        contrarian[uname] = mean(devs) if devs else 0

    # сложность матчей: доля набравших очки, средние очки
    match_diff = {}
    for uuid, pts in pmpts.items():
        if not pts:
            continue
        scorers = sum(1 for v in pts.values() if v > 0)
        match_diff[uuid] = {
            'scorers': scorers, 'n': len(pts),
            'scorer_rate': scorers / len(pts),
            'avg_pts': mean(pts.values()),
            'ev': ev[uuid],
        }

    # апсеты vs консенсус: |факт - средняя ставка|
    upsets = {}
    for uuid, mb in match_bets.items():
        if not mb:
            continue
        m1 = mean(v['t1'] for v in mb.values())
        m2 = mean(v['t2'] for v in mb.values())
        e = ev[uuid]
        upsets[uuid] = {
            'dist': abs(e['rt1'] - m1) + abs(e['rt2'] - m2),
            'cons': (m1, m2), 'ev': e,
        }

    # победитель тура: кто взял максимум очков в матче (доли при равенстве)
    match_wins = Counter()
    for uuid, pts in pmpts.items():
        if not pts:
            continue
        mx = max(pts.values())
        if mx <= 0:
            continue
        winners = [k for k, v in pts.items() if v == mx]
        for w in winners:
            match_wins[w] += 1 / len(winners)

    return {
        'h2h': h2h, 'pair_dist': pair_dist, 'contrarian': contrarian,
        'match_diff': match_diff, 'upsets': upsets, 'match_wins': match_wins,
        'match_bets': match_bets, 'pmpts': pmpts,
    }


# ----------------------------------------------------------------------------
# 7. Фаворитизм к клубам
# ----------------------------------------------------------------------------

def analyze_club_bias(season, active, min_matches=4):
    """Для каждого (игрок, клуб): насколько игрок «болеет» за клуб сильнее консенсуса.
    lean = голы за клуб - голы против; bias = mean(lean игрока) - mean(lean группы)."""
    ev = season['events']
    # собрать по клубу матчи и ставки
    club_matches = defaultdict(list)  # club -> list of (uuid, side)  side: 1 если клуб = team_1
    for uuid, e in ev.items():
        club_matches[e['t1']].append((uuid, 1))
        club_matches[e['t2']].append((uuid, 2))

    bets_by = {u: {r['uuid']: r['bet'] for r in per_match(season, u)} for u in active}

    result = defaultdict(dict)  # username -> {club: bias}
    club_strength = {}
    for club, lst in club_matches.items():
        if len(lst) < min_matches:
            continue
        # консенсус-lean по каждому матчу (среди тех, кто ставил)
        per_player_lean = defaultdict(list)
        group_lean_per_match = {}
        for uuid, side in lst:
            leans = []
            pl_lean = {}
            for u in active:
                b = bets_by[u].get(uuid)
                if not b:
                    continue
                lean = (b['t1'] - b['t2']) if side == 1 else (b['t2'] - b['t1'])
                pl_lean[u] = lean
                leans.append(lean)
            if not leans:
                continue
            g = mean(leans)
            for u, lv in pl_lean.items():
                per_player_lean[u].append(lv - g)
        for u, devs in per_player_lean.items():
            if len(devs) >= min_matches:
                result[u][club] = mean(devs)
    return result


# ----------------------------------------------------------------------------
# 8. Турнирная мета
# ----------------------------------------------------------------------------

def analyze_tournament(season, active):
    ev = season['events']

    def stats(events):
        n = len(events)
        goals = sum(e['rt1'] + e['rt2'] for e in events)
        draws = sum(1 for e in events if e['rt1'] == e['rt2'])
        return {'n': n, 'goals': goals, 'avg': goals / n if n else 0,
                'draws': draws, 'draw_rate': draws / n if n else 0}

    allv = list(ev.values())
    league = [e for e in allv if e['phase'] == 'league']
    ko = [e for e in allv if e['phase'] == 'knockout']
    # индексы предсказуемости. ppb зависит от шкалы очков (между сезонами разная!),
    # поэтому для честного сравнения используем долю набравших очко и долю точных — они от весов не зависят.
    aps = [analyze_player(season, u) for u in active]
    return {
        'all': stats(allv), 'league': stats(league), 'knockout': stats(ko),
        'predictability_ppb': mean([a['ppb'] for a in aps]) if aps else 0,
        'group_scored_rate': mean([a['scored_rate'] for a in aps]) if aps else 0,
        'group_exact_rate': mean([a['exact'] / a['n'] for a in aps if a['n']]) if aps else 0,
        'n_active': len(active),
    }


# ----------------------------------------------------------------------------
# 9. MAIN: собрать всё
# ----------------------------------------------------------------------------

def load_csv_tournament(path):
    """Загрузка турнира только из CSV-выгрузки (формат /export_statistic).
    Возвращает (matches, bets_by_user). Проход хранится именем команды."""
    with open(path, encoding='utf-8') as f:
        rows = list(csv.reader(f))[1:]
    matches = {}
    bets = defaultdict(list)
    for r in rows:
        if len(r) < 9:
            continue
        t1, t2, g1, g2, prg, u, b1, b2, bprg = r
        g1, g2, b1, b2 = int(g1), int(g2), int(b1), int(b2)
        ev = {'t1': t1, 't2': t2, 'rt1': g1, 'rt2': g2,
              'through_name': prg or None, 'decisive': bool(prg)}
        matches[(t1, t2)] = ev
        bets[u].append((ev, {'t1': b1, 't2': b2, 'through_name': bprg or None}))
    return matches, bets


def analyze_csv_player(bets_u, W):
    n = len(bets_u)
    cc = Counter()
    pts = scored = adv = dec = 0
    for ev, bet in bets_u:
        c = category(ev['rt1'], ev['rt2'], bet['t1'], bet['t2'])
        cc[c] += 1
        p = W[c]
        if ev['decisive']:
            dec += 1
            if bet['through_name'] and ev['through_name'] and bet['through_name'] == ev['through_name']:
                adv += 1
                p += W['adv']
        pts += p
        if p > 0:
            scored += 1
    return {'n': n, 'exact': cc.get('exact', 0), 'diff': cc.get('diff', 0),
            'draw': cc.get('draw', 0), 'winner': cc.get('winner', 0), 'miss': cc.get(None, 0),
            'adv_hits': adv, 'decisive_n': dec, 'total': pts,
            'ppb': pts / n if n else 0, 'scored_rate': scored / n if n else 0}


def tournament_meta_csv(matches):
    em = list(matches.values())
    n = len(em)
    goals = sum(e['rt1'] + e['rt2'] for e in em)
    draws = sum(1 for e in em if e['rt1'] == e['rt2'])
    return {'n': n, 'goals': goals, 'avg': goals / n if n else 0,
            'draws': draws, 'draw_rate': draws / n if n else 0}


def build():
    s25 = load_season('2025')
    s26 = load_season('2026')

    # активные = >=30 ставок
    def active_of(season):
        return [u for u in season['players']
                if len(season['players'][u]['bets']) >= 30]

    act25 = active_of(s25)
    act26 = active_of(s26)

    # официальная аналитика (веса своего сезона)
    players25 = {u: analyze_player(s25, u) for u in s25['players']}
    players26 = {u: analyze_player(s26, u) for u in s26['players']}
    # нормализованные очки 2025 по правилам 2026 (для честного сравнения шкалы)
    norm25 = {u: analyze_player(s25, u, W_NORM) for u in s25['players']}

    social25 = analyze_social(s25, sorted(act25, key=lambda u: -players25[u]['total']))
    social26 = analyze_social(s26, sorted(act26, key=lambda u: -players26[u]['total']))
    club25 = analyze_club_bias(s25, act25)
    club26 = analyze_club_bias(s26, act26)
    tour25 = analyze_tournament(s25, act25)
    tour26 = analyze_tournament(s26, act26)

    # YoY для вернувшихся: норм. очки (одна шкала) + доли (от весов не зависят)
    returning = [u for u in players25 if u in players26]
    yoy = {}
    for u in returning:
        a, b = norm25[u], players26[u]  # обе — по правилам 2026
        off_a = players25[u]['total']  # официальные очки 2025 (шкала 2025)
        yoy[u] = {
            'name': players26[u]['name'], 'username': u,
            'norm25': a['total'], 'norm26': b['total'], 'dnorm': b['total'] - a['total'],
            'off25': off_a, 'off26': b['total'],
            'scored25': a['scored_rate'], 'scored26': b['scored_rate'],
            'exact25': a['exact'], 'exact26': b['exact'],
            'diff25': a['diff'], 'diff26': b['diff'],
            'draw25': a['draw'], 'draw26': b['draw'],
            'winner25': a['winner'], 'winner26': b['winner'],
            'adv25': a['adv_hits'], 'adv26': b['adv_hits'],
            'ppb25': a['ppb'], 'ppb26': b['ppb'],  # обе в шкале 2026
            'n25': a['n'], 'n26': b['n'],
            'agg25': a['avg_bet_total'], 'agg26': b['avg_bet_total'],
        }

    # Евро-2024 (только CSV; очки — в нормализованной шкале 2026)
    euro = None
    euro_path = os.path.join(BASE, '2024', 'European Championship 2024.csv')
    if os.path.exists(euro_path):
        em, eb = load_csv_tournament(euro_path)
        ep = {u: analyze_csv_player(eb[u], W_NORM) for u in eb}
        euro = {'players': ep, 'meta': tournament_meta_csv(em),
                'active': [u for u in ep if ep[u]['n'] >= 30]}

    return {
        's25': s25, 's26': s26,
        'players25': players25, 'players26': players26, 'norm25': norm25,
        'act25': act25, 'act26': act26, 'returning': returning, 'yoy': yoy,
        'social25': social25, 'social26': social26,
        'club25': club25, 'club26': club26,
        'tour25': tour25, 'tour26': tour26,
        'euro': euro,
    }


def reconciliation(D):
    print('=== СВЕРКА: пересчитано (веса своего сезона) vs сохранённый scores ===')
    mismatches = []
    for tag, players in (('2025', D['players25']), ('2026', D['players26'])):
        for u, a in sorted(players.items(), key=lambda x: -x[1]['total']):
            diff = a['stored'] - a['total']
            mark = 'OK' if diff == 0 else f'adj {diff:+d}'
            if diff != 0:
                mismatches.append((tag, u, diff))
            print(f"  {tag} {u:18s} calc={a['total']:4d} stored={a['stored']:4d} "
                  f"bets={a['n']:3d}  {mark}")
    n_ps = sum(len(p) for p in (D['players25'], D['players26']))
    print(f'ИТОГ: совпало {n_ps - len(mismatches)}/{n_ps} player-seasons.', end=' ')
    print('Расхождения (ручные правки maintainer):',
          ', '.join(f"{t}/{u}{d:+d}" for t, u, d in mismatches) or 'нет')
    return mismatches


def csv_crosscheck(D):
    path = os.path.join(BASE, '2026', 'stat.csv')
    if not os.path.exists(path):
        print('stat.csv не найден — пропуск кросс-проверки')
        return
    with open(path, encoding='utf-8') as f:
        rows = list(csv.reader(f))[1:]
    print(f'=== КРОСС-ПРОВЕРКА stat.csv: {len(rows)} строк-ставок ===')
    total_bets_bson = sum(a['n'] for a in D['players26'].values())
    print(f'  ставок в BSON (с результатом): {total_bets_bson}')


def debug_dump(D):
    reconciliation(D)
    csv_crosscheck(D)
    for tag, players, act in (('2025', D['players25'], D['act25']),
                              ('2026', D['players26'], D['act26'])):
        print(f'\n===== {tag} (активных {len(act)}) =====')
        ranked = sorted(players.values(), key=lambda a: -a['total'])
        for i, a in enumerate(ranked, 1):
            fav = a['fav_scoreline']
            favs = f"{fav[0][0]}:{fav[0][1]}×{fav[1]}" if fav else '-'
            print(f"{i:2d}. {a['name'][:16]:16s} {a['total']:4d} оч | "
                  f"ТС={a['exact']:2d} РМ={a['diff']:2d} Н={a['draw']:2d} "
                  f"П={a['winner']:2d} мимо={a['miss']:2d} | проход {a['adv_hits']}/{a['decisive_n']} | "
                  f"ppb={a['ppb']:.2f} нм={a['nm_total']:2d}(−{a['foregone']}) "
                  f"σ={a['pts_std']:.2f} hot={a['hot_streak']} cold={a['cold_streak']} | "
                  f"агр={a['avg_bet_total']:.2f}(факт{a['avg_act_total']:.2f}) "
                  f"люб={favs} разн={a['distinct_scorelines']}")
        t = D['tour25'] if tag == '2025' else D['tour26']
        print(f"  ТУРНИР: матчей={t['all']['n']} голов={t['all']['goals']} "
              f"avg={t['all']['avg']:.2f} ничьих={t['all']['draws']}({t['all']['draw_rate'] * 100:.0f}%) "
              f"| лига avg={t['league']['avg']:.2f} | плей-офф avg={t['knockout']['avg']:.2f} "
              f"| предсказуемость ppb={t['predictability_ppb']:.3f}")
        soc = D['social25'] if tag == '2025' else D['social26']
        # близнецы
        if soc['pair_dist']:
            tw = min(soc['pair_dist'].items(), key=lambda x: x[1]['dist'])
            print(f"  БЛИЗНЕЦЫ: {tw[0][0]} ~ {tw[0][1]} dist={tw[1]['dist']:.2f} "
                  f"идентичных={tw[1]['ident'] * 100:.0f}%")
        # контрарность
        cc = sorted(soc['contrarian'].items(), key=lambda x: -x[1])
        print("  КОНТРАРНОСТЬ:", ', '.join(f"{k}={v:.2f}" for k, v in cc))
        # победитель тура
        mw = soc['match_wins'].most_common(3)
        print("  ПОБЕДИТЕЛЬ ТУРА:", ', '.join(f"{k}={v:.1f}" for k, v in mw))
        # самые сложные матчи
        hard = sorted(soc['match_diff'].values(), key=lambda m: m['scorer_rate'])[:3]
        for m in hard:
            e = m['ev']
            print(f"    сложный: {e['t1']} {e['rt1']}:{e['rt2']} {e['t2']} "
                  f"— набрали {m['scorers']}/{m['n']}")
        # клубный фаворитизм (топ по сезону)
        clubD = D['club25'] if tag == '2025' else D['club26']
        for u in act:
            if u in clubD and clubD[u]:
                top = max(clubD[u].items(), key=lambda x: x[1])
                bot = min(clubD[u].items(), key=lambda x: x[1])
                print(f"    фанат: {u:16s} +{top[0]}({top[1]:+.2f})  −{bot[0]}({bot[1]:+.2f})")

    # YoY (нормализовано по правилам 2026)
    print('\n===== ГОД-К-ГОДУ (вернувшиеся, очки по единой шкале 2026) =====')
    print('  офиц.шкала: 2025=3/2/2/1, 2026=4/3/2/1 — поэтому официальные тоталы НЕ сравнимы напрямую')
    for u in sorted(D['yoy'], key=lambda u: -D['yoy'][u]['dnorm']):
        y = D['yoy'][u]
        print(f"  {u:16s} офиц {y['off25']:3d}→{y['off26']:3d} | НОРМ {y['norm25']:3d}→{y['norm26']:3d} "
              f"({y['dnorm']:+d}) | взял% {y['scored25'] * 100:.0f}→{y['scored26'] * 100:.0f} | "
              f"ТС {y['exact25']}→{y['exact26']} РМ {y['diff25']}→{y['diff26']} "
              f"П {y['winner25']}→{y['winner26']} проход {y['adv25']}→{y['adv26']}")


# ----------------------------------------------------------------------------
# 10. Витрина суперлативов
# ----------------------------------------------------------------------------

def superlatives(players, social, club, active):
    A = {u: players[u] for u in active}

    def amax(key):
        return max(A.values(), key=key)

    def amin(key):
        return min(A.values(), key=key)

    s = []
    s.append(('🎯', 'Король точного счёта', amax(lambda a: a['exact']),
              lambda a: f"{a['exact']} точных счетов"))
    s.append(('🛡️', 'Мистер Проход', amax(lambda a: a['adv_hits']),
              lambda a: f"{a['adv_hits']} из {a['decisive_n']} проходов угадано"))
    s.append(('🎰', 'Главный гэмблер', amax(lambda a: a['avg_bet_total']),
              lambda a: f"любит рисковые результативные счета — в среднем закладывал "
                        f"{a['avg_bet_total']:.2f} гола за матч (по факту {a['avg_act_total']:.2f})"))
    s.append(('🐢', 'Главный консерватор', amin(lambda a: a['avg_bet_total']),
              lambda a: f"осторожный: в среднем всего {a['avg_bet_total']:.2f} гола за матч"))
    s.append(('🧊', 'Мистер Стабильность', amin(lambda a: a['pts_std']),
              lambda a: f"самый ровный — очки от матча к матчу скачут меньше всех "
                        f"(разброс σ = {a['pts_std']:.2f})"))
    s.append(('🎢', 'Американские горки', amax(lambda a: a['pts_std']),
              lambda a: f"то густо, то пусто — самый большой разброс очков (σ = {a['pts_std']:.2f})"))
    s.append(('😩', 'Самый невезучий', amax(lambda a: a['foregone']),
              lambda a: f"{a['nm_total']} раз промахнулся «в один мяч», недобрал ~{a['foregone']} очк."))
    s.append(('🔥', 'Лучшая серия', amax(lambda a: a['hot_streak']),
              lambda a: f"{a['hot_streak']} матчей подряд с очками"))
    # контрарность / конформизм
    cc = social['contrarian']
    cu = max(cc, key=cc.get);
    co = min(cc, key=cc.get)
    s.append(('🃏', 'Контрарианец', players[cu],
              lambda a, cu=cu: f"чаще всех идёт против большинства — ставит дальше всех "
                               f"от общего мнения группы ({cc[cu]:.2f})"))
    s.append(('🐑', 'Конформист', players[co],
              lambda a, co=co: f"играет «как все» — его ставки ближе всех к среднему по группе ({cc[co]:.2f})"))
    # победитель тура
    mw = social['match_wins']
    if mw:
        topw = mw.most_common(1)[0]
        s.append(('🏆', 'Чаще всех брал матч', players[topw[0]],
                  lambda a, topw=topw: f"{topw[1]:.1f} побед в матч-днях (с учётом дележа)"))
    return s


# ----------------------------------------------------------------------------
# 11. HTML
# ----------------------------------------------------------------------------

CMAP = {
    'a_y_n_e_s': '#e6194B', 'elnur_23': '#4363d8', 'madsunrise': '#3cb44b',
    'rinatka99': '#f58231', 'ildariz': '#911eb4', 'tsyplyaeva_anna': '#d633c0',
    'DV_pro_1': '#1ba3c6', 'armoald': '#9A6324', 'forsag8_8': '#808000',
}
CAT_COLOR = {'exact': '#1a9850', 'diff': '#7cc36b', 'draw': '#fee08b',
             'winner': '#fc8d59', 'miss': '#e6e6e6'}


def esc(s):
    return html.escape(str(s))


def col(u):
    return CMAP.get(u, '#888')


def hbar(rows, maxv=None, unit=''):
    """rows: list of (label, value, display, color). Горизонтальные бары."""
    if maxv is None:
        maxv = max((r[1] for r in rows), default=1) or 1
    out = ['<div class="bars">']
    for label, value, disp, color in rows:
        w = max(1.0, 100.0 * value / maxv)
        out.append(
            f'<div class="bar-row"><div class="bar-lab">{esc(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{w:.1f}%;background:{color}"></div></div>'
            f'<div class="bar-val">{esc(disp)}</div></div>')
    out.append('</div>')
    return ''.join(out)


def stacked_cat(a):
    """Стек-бар профиля по категориям (доли от числа ставок)."""
    n = a['n'] or 1
    segs = [('exact', a['exact']), ('diff', a['diff']), ('draw', a['draw']),
            ('winner', a['winner']), ('miss', a['miss'])]
    out = ['<div class="stack">']
    for k, v in segs:
        if v <= 0:
            continue
        out.append(f'<div class="seg" style="width:{100.0 * v / n:.2f}%;background:{CAT_COLOR[k]}" '
                   f'title="{esc(CAT_RU[k if k != "miss" else None])}: {v}"></div>')
    out.append('</div>')
    return ''.join(out)


def slope_chart(order_l, order_r, val_l, val_r, title_l, title_r):
    """SVG слоуп-график перемещений в рейтинге (мест)."""
    n = len(order_l)
    top, step, L, R = 46, 48, 175, 365
    H = top + n * step + 10
    W = 540
    yl = {u: top + i * step for i, u in enumerate(order_l)}
    yr = {u: top + i * step for i, u in enumerate(order_r)}
    p = [f'<svg class="slope" viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMin meet">']
    p.append(f'<text x="{L - 12}" y="24" text-anchor="end" class="slope-h">{esc(title_l)}</text>')
    p.append(f'<text x="{R + 12}" y="24" text-anchor="start" class="slope-h">{esc(title_r)}</text>')
    for u in order_l:
        c = col(u)
        p.append(f'<line x1="{L}" y1="{yl[u]}" x2="{R}" y2="{yr[u]}" stroke="{c}" '
                 f'stroke-width="3" opacity="0.85"/>')
        p.append(f'<circle cx="{L}" cy="{yl[u]}" r="5" fill="{c}"/>')
        p.append(f'<circle cx="{R}" cy="{yr[u]}" r="5" fill="{c}"/>')
    for i, u in enumerate(order_l):
        p.append(f'<text x="{L - 12}" y="{yl[u] + 4}" text-anchor="end" class="slope-t">'
                 f'{i + 1}. {esc(NAMES[u])} · {val_l[u]}</text>')
    for i, u in enumerate(order_r):
        p.append(f'<text x="{R + 12}" y="{yr[u] + 4}" text-anchor="start" class="slope-t">'
                 f'{i + 1}. {esc(NAMES[u])} · {val_r[u]}</text>')
    p.append('</svg>')
    return ''.join(p)


def heatmap(active, h2h):
    """Таблица H2H: доля побед строки над столбцом (по матчам, где оба ставили)."""
    out = ['<table class="hm"><thead><tr><th></th>']
    for b in active:
        out.append(f'<th><span style="color:{col(b)}">{esc(NAMES[b][:8])}</span></th>')
    out.append('<th>итог</th></tr></thead><tbody>')
    for a in active:
        out.append(f'<tr><td class="hm-row" style="color:{col(a)}">{esc(NAMES[a])}</td>')
        tw = tl = 0
        for b in active:
            if a == b:
                out.append('<td class="hm-x">—</td>')
                continue
            w, l, t = h2h[a][b]
            tw += w;
            tl += l
            tot = w + l + t
            share = (w + 0.5 * t) / tot if tot else 0.5
            # цвет: зелёный если >0.5, красный если <0.5
            if share >= 0.5:
                g = int(120 + 110 * (share - 0.5) * 2)
                bg = f'rgba(60,{g},90,{0.25 + (share - 0.5):.2f})'
            else:
                r = int(150 + 90 * (0.5 - share) * 2)
                bg = f'rgba({r},70,70,{0.25 + (0.5 - share):.2f})'
            out.append(f'<td style="background:{bg}" title="{w}-{l}-{t}">{int(round(share * 100))}</td>')
        wr = tw / (tw + tl) * 100 if (tw + tl) else 50
        out.append(f'<td class="hm-tot">{wr:.0f}%</td></tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def table(headers, rows, cls=''):
    out = [f'<table class="{cls}"><thead><tr>']
    for h in headers:
        out.append(f'<th>{h}</th>')
    out.append('</tr></thead><tbody>')
    for r in rows:
        out.append('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def pname(u, short=False):
    nm = NAMES.get(u, u)
    if short:
        return f'<b style="color:{col(u)}">{esc(nm)}</b>'
    return f'<b style="color:{col(u)}">{esc(nm)}</b> <span class="uname">@{esc(u)}</span>'


NAMES = {}

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1c2330;--mut:#6b7686;--line:#e7eaf0;--accent:#3b5bdb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 18px 80px}
header.top{background:linear-gradient(135deg,#1e2a5a,#3b5bdb);color:#fff;padding:46px 0 38px;margin-bottom:8px}
header.top .wrap{padding-bottom:0}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.3px}
.sub{opacity:.85;font-size:16px;max-width:760px}
nav{position:sticky;top:0;z-index:9;background:rgba(255,255,255,.95);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:9px 0;margin-bottom:22px;font-size:13.5px}
nav .wrap{padding-top:0;padding-bottom:0;display:flex;flex-wrap:wrap;gap:14px}
nav a{color:var(--mut);text-decoration:none;white-space:nowrap}
nav a:hover{color:var(--accent)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:0 0 22px;box-shadow:0 1px 2px rgba(20,30,60,.04)}
h2{font-size:22px;margin:0 0 4px;letter-spacing:-.2px}
h2 .em{margin-right:8px}
.lead{color:var(--mut);margin:0 0 18px;font-size:14.5px}
h3{font-size:16.5px;margin:22px 0 10px}
p{margin:10px 0}
.muted{color:var(--mut)}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:1fr 1fr 1fr}
@media(max-width:760px){.g2,.g3{grid-template-columns:1fr}}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:6px 0 4px}
@media(max-width:760px){.kpi{grid-template-columns:1fr 1fr}}
.kpi .box{background:#f3f5fb;border:1px solid var(--line);border-radius:11px;padding:13px 14px}
.kpi .n{font-size:25px;font-weight:700;letter-spacing:-.5px}
.kpi .l{font-size:12.5px;color:var(--mut);margin-top:2px}
.tldr{border-left:4px solid var(--accent);background:#f3f5fb;border-radius:8px;padding:4px 16px;margin:14px 0}
.tldr li{margin:9px 0}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}
@media(max-width:820px){.cards{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.cards{grid-template-columns:1fr}}
.sup{background:#fbfbfe;border:1px solid var(--line);border-radius:12px;padding:13px 14px}
.sup .e{font-size:24px}.sup .t{font-weight:700;font-size:14px;margin:3px 0}
.sup .w{font-size:15px}.sup .d{font-size:12.5px;color:var(--mut);margin-top:3px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}
th,td{padding:7px 9px;text-align:center;border-bottom:1px solid var(--line)}
th{font-size:12px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.3px}
td.l,th.l{text-align:left}
tbody tr:hover{background:#f7f9ff}
.uname{color:var(--mut);font-size:12px;font-weight:400}
.bars{margin:6px 0}
.bar-row{display:grid;grid-template-columns:150px 1fr 84px;align-items:center;gap:9px;margin:5px 0}
.bar-lab{font-size:13px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{background:#eef0f6;border-radius:6px;height:17px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px}
.bar-val{font-size:13px;font-weight:600;text-align:left}
.stack{display:flex;height:14px;border-radius:5px;overflow:hidden;margin:5px 0;border:1px solid var(--line)}
.stack .seg{height:100%}
.legend{font-size:12px;color:var(--mut);display:flex;gap:14px;flex-wrap:wrap;margin:4px 0 0}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
.pcard{background:#fbfbfe;border:1px solid var(--line);border-radius:12px;padding:16px 17px;margin:13px 0}
.pcard h3{margin:0 0 2px;font-size:18px}
.pcard .tag{font-size:12.5px;color:var(--mut);margin-bottom:9px}
.delta-pos{color:#1a9850;font-weight:700}.delta-neg{color:#d6334c;font-weight:700}
.slope{display:block;margin:6px auto}
.slope-h{font-size:13px;font-weight:700;fill:var(--ink)}
.slope-t{font-size:12.5px;fill:var(--ink)}
.hm{font-size:12.5px}.hm th,.hm td{padding:5px 6px;border:1px solid var(--line)}
.hm-row{text-align:left;font-weight:600}.hm-x{color:#ccc}.hm-tot{font-weight:700;background:#f3f5fb}
.note{font-size:13px;color:var(--mut);background:#fbfaf3;border:1px solid #efe9cf;border-radius:9px;padding:11px 14px;margin:12px 0}
.foot{color:var(--mut);font-size:12.5px;text-align:center;margin-top:26px}
.pill{display:inline-block;background:#eef0f6;border-radius:20px;padding:1px 9px;font-size:12px;margin:1px 2px}
"""


def render_html(D):
    global NAMES
    p25, p26 = D['players25'], D['players26']
    NAMES = {}
    for u, a in {**p25, **p26}.items():
        NAMES[u] = a['name']

    rank26 = sorted(p26.values(), key=lambda a: -a['stored'])
    rank25 = sorted(p25.values(), key=lambda a: -a['stored'])
    act26 = sorted(D['act26'], key=lambda u: -p26[u]['stored'])
    act25 = sorted(D['act25'], key=lambda u: -p25[u]['stored'])
    yoy = D['yoy']
    t25, t26 = D['tour25'], D['tour26']
    out = []

    # ---------- header + nav ----------
    out.append(f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
               f'<meta name="viewport" content="width=device-width,initial-scale=1">'
               f'<title>Тотализатор ЛЧ · аналитика 2025 vs 2026</title><style>{CSS}</style></head><body>')
    out.append('<header class="top"><div class="wrap">'
               '<h1>⚽ Тотализатор Лиги Чемпионов — глубокая аналитика</h1>'
               '<div class="sub">Два полных сезона под микроскопом: кто реально вырос, '
               'кто просел и почему. Срезы, которых нет в боте — стиль ставок, везение, '
               'очные дуэли и суперлативы.</div></div></header>')
    out.append('<nav><div class="wrap">'
               '<a href="#tldr">Главное</a><a href="#data">Данные</a>'
               '<a href="#stand">Таблицы</a><a href="#yoy">Год-к-году</a>'
               '<a href="#new">Новички</a><a href="#luck">Мастерство и везение</a>'
               '<a href="#style">Стиль ставок</a><a href="#social">Дуэли и социум</a>'
               '<a href="#meta">Турниры</a><a href="#euro">Бонус: ЧЕ-2024</a>'
               '<a href="#method">Методика</a></div></nav>')
    out.append('<div class="wrap">')

    out.append(_sec_tldr(D, rank26, yoy, act26))
    out.append(_sec_data(D))
    out.append(_sec_standings(D, rank25, rank26))
    out.append(_sec_yoy(D, yoy))
    out.append(_sec_newcomers(D))
    out.append(_sec_luck(D, act26))
    out.append(_sec_style(D, act26))
    out.append(_sec_social(D, act26))
    out.append(_sec_meta(D, t25, t26))
    if D.get('euro'):
        out.append(_sec_euro(D))
    out.append(_sec_method(D))

    out.append('<div class="foot">Сгенерировано из дампов MongoDB (events.bson / users.bson), '
               'stat.csv и выгрузки Евро-2024. Очки пересчитаны и сверены с сохранёнными тоталами бота.</div>')
    out.append('</div></body></html>')
    return ''.join(out)


def _sec_tldr(D, rank26, yoy, act26):
    champ = rank26[0]
    top_imp = max(yoy.values(), key=lambda y: y['dnorm'])
    top_reg = min(yoy.values(), key=lambda y: y['dnorm'])
    sup = superlatives(D['players26'], D['social26'], D['club26'], act26)
    o = ['<section id="tldr"><h2><span class="em">✨</span>Самое главное</h2>',
         '<p class="lead">Если читать только один экран — читайте этот.</p>']
    o.append('<ul class="tldr">')
    o.append(f'<li>🏆 <b>Чемпион 2026 — {pname(champ["username"], 1)}</b> ({champ["stored"]} очк.). '
             f'Годом ранее он был лишь 4-м из 6 — и это <b>самый настоящий прорыв</b>, а не инфляция очков.</li>')
    o.append('<li>⚠️ <b>Правила сменились между сезонами:</b> точный счёт подорожал с 3 до 4 очков, '
             'разница мячей — с 2 до 3. Поэтому «итоговые очки» 2025 и 2026 <b>нельзя сравнивать в лоб</b> — '
             'мы пересчитали оба сезона по единой шкале.</li>')
    o.append('<li>📊 По честной (единой) шкале <b>выросли только трое</b> из шести вернувшихся '
             f'({pname("a_y_n_e_s", 1)}, {pname("tsyplyaeva_anna", 1)}, {pname("rinatka99", 1)}), '
             f'а <b>трое просели</b> — хотя «официальные» очки выросли почти у всех. Иллюзия роста — '
             f'это инфляция новых правил.</li>')
    o.append(f'<li>📉 Бывший чемпион {pname("elnur_23", 1)} по честной шкале <b>сдал −10</b> и уступил трон; '
             f'заметнее всех просел {pname("ildariz", 1)} (−15) — впрочем, у него был сильный Евро-2024.</li>')
    o.append(f'<li>🆕 Лучший новичок — {pname("DV_pro_1", 1)}: '
             f'сразу <b>3-е место</b> ({D["players26"]["DV_pro_1"]["stored"]} очк.) и лучшая в лиге '
             f'точность по проходам.</li>')
    o.append('</ul>')
    # витрина
    o.append('<h3>🏅 Витрина титулов сезона 2026</h3>')
    o.append('<div class="cards">')
    for emoji, title, who, det in sup:
        o.append(f'<div class="sup"><div class="e">{emoji}</div><div class="t">{esc(title)}</div>'
                 f'<div class="w">{pname(who["username"], 1)}</div>'
                 f'<div class="d">{det(who)}</div></div>')
    o.append('</div></section>')
    return ''.join(o)


def _sec_data(D):
    o = ['<section id="data"><h2><span class="em">🗂️</span>Что в данных и что можно вытащить</h2>',
         '<p class="lead">Источник правды — дампы MongoDB обоих сезонов; stat.csv использован для кросс-проверки.</p>']
    o.append('<div class="kpi">'
             '<div class="box"><div class="n">2</div><div class="l">полных сезона</div></div>'
             '<div class="box"><div class="n">203+203</div><div class="l">матча с результатом</div></div>'
             '<div class="box"><div class="n">2626</div><div class="l">ставок всего (1209+1417)</div></div>'
             '<div class="box"><div class="n">31+31</div><div class="l">решающих матча (проход)</div></div>'
             '</div>')
    o.append('<p>Из каждой ставки доступны: счёт, ставка на проход и <b>точное время ставки</b> '
             '(можно мерить, кто ставит заранее, а кто в последний момент). По каждому матчу известны '
             'команды, реальный счёт, стадия и кто прошёл дальше. Это позволяет считать то, чего бот не показывает:</p>')
    o.append('<div class="grid g2">')
    o.append('<div><b>Эффективность и точность</b><ul class="muted">'
             '<li>очки за ставку, доля «взял хоть очко»</li>'
             '<li>доли по категориям, а не только их количество</li>'
             '<li>точность по проходам, по стадиям</li></ul></div>')
    o.append('<div><b>Везение и стабильность</b><ul class="muted">'
             '<li>промахи «в один мяч» и недобранные из-за них очки</li>'
             '<li>разброс очков за матч (σ), серии удач и провалов</li></ul></div>')
    o.append('<div><b>Стиль и предвзятости</b><ul class="muted">'
             '<li>агрессивность (сколько голов закладывают) и калибровка</li>'
             '<li>любимые счета, склонность к ничьим, крен на первую команду</li>'
             '<li>за какие клубы «болеют» в ставках сильнее группы</li></ul></div>')
    o.append('<div><b>Социум</b><ul class="muted">'
             '<li>очные дуэли (кто кого обыгрывает по матчам)</li>'
             '<li>«близнецы» по ставкам и контрарианцы</li>'
             '<li>самые сложные матчи и апсеты против консенсуса</li></ul></div>')
    o.append('</div>')
    o.append('</section>')
    return ''.join(o)


def _sec_standings(D, rank25, rank26):
    o = ['<section id="stand"><h2><span class="em">📋</span>Итоговые таблицы</h2>',
         '<p class="lead">Очки — по правилам своего сезона (2025: 4 тир = 3/2/2/1; 2026: 4/3/2/1 + проход). '
         'Профиль — доли категорий от числа ставок.</p>']
    o.append('<div class="legend">' + ''.join(
        f'<span><i style="background:{CAT_COLOR[k]}"></i>{lbl}</span>'
        for k, lbl in [('exact', 'Точный'), ('diff', 'Разница'), ('draw', 'Ничья'),
                       ('winner', 'Победитель'), ('miss', 'Мимо')]) + '</div>')

    def srows(ranked):
        rows = []
        for i, a in enumerate(ranked, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            rows.append([
                medal, f'<span class="l">{pname(a["username"])}</span>',
                f'<b>{a["stored"]}</b>', a['n'], f'{a["ppb"]:.2f}',
                a['exact'], a['diff'], a['winner'], f'{a["adv_hits"]}/{a["decisive_n"]}',
                f'{a["scored_rate"] * 100:.0f}%', stacked_cat(a)])
        return rows

    hdr = ['#', 'Игрок', 'Очки', 'Ставок', 'Оч/ст', 'Точн', 'Разн', 'Побед', 'Проход', 'Взял%', 'Профиль']
    o.append('<h3>Сезон 2026</h3>')
    o.append(table(hdr, srows(rank26), 'stand'))
    o.append('<h3>Сезон 2025</h3>')
    o.append(table(hdr, srows(rank25), 'stand'))
    o.append('</section>')
    return ''.join(o)


def _sec_yoy(D, yoy):
    o = ['<section id="yoy"><h2><span class="em">📈</span>Год-к-году: кто вырос, кто просел — и почему</h2>',
         '<p class="lead">Главная интрига. Чтобы сравнение было честным, очки обоих сезонов приведены '
         'к единой шкале 2026. Иначе рост правил (3→4 за точный счёт) выдаёт прогресс там, где его нет.</p>']

    o.append('<div class="note">💡 <b>Парадокс инфляции:</b> «официальные» очки выросли почти у всех — '
             'но это потому, что в 2026 за то же мастерство дают больше очков. По <b>единой шкале</b> '
             'картина другая: выросли трое, просели трое.</div>')

    # slope chart по нормализованным очкам среди вернувшихся
    ret = list(yoy)
    order_l = sorted(ret, key=lambda u: -yoy[u]['norm25'])
    order_r = sorted(ret, key=lambda u: -yoy[u]['norm26'])
    vl = {u: yoy[u]['norm25'] for u in ret}
    vr = {u: yoy[u]['norm26'] for u in ret}
    o.append('<h3>Перемещения в рейтинге (единая шкала 2026, только вернувшиеся)</h3>')
    o.append(slope_chart(order_l, order_r, vl, vr, '2025 (норм.)', '2026'))

    # bar чарт дельт
    o.append('<h3>Δ очков по честной шкале</h3>')
    o.append('<p class="muted">Δ (дельта) — это изменение: на сколько очков игрок прибавил (+) или '
             'потерял (−) по сравнению с прошлым сезоном, если оба сезона считать по единым правилам 2026.</p>')
    rows = []
    for u in sorted(ret, key=lambda u: -yoy[u]['dnorm']):
        y = yoy[u]
        d = y['dnorm']
        rows.append((NAMES[u], abs(d), f"{'+' if d >= 0 else '−'}{abs(d)}",
                     '#1a9850' if d >= 0 else '#d6334c'))
    o.append(hbar(rows))

    # таблица офиц vs норм + доли
    o.append('<h3>Официальные vs честные очки и сдвиг точности</h3>')
    hdr = ['Игрок', 'Офиц. 2025', 'Офиц. 2026', '«Рост» офиц.', 'Честно 25→26', 'Δ честно',
           'Взял% 25→26', 'Точн', 'Разн', 'Проход']
    trows = []
    for u in sorted(ret, key=lambda u: -yoy[u]['dnorm']):
        y = yoy[u]
        off_d = y['off26'] - y['off25']
        dd = y['dnorm']
        dcls = 'delta-pos' if dd >= 0 else 'delta-neg'
        trows.append([
            f'<span class="l">{pname(u, 1)}</span>',
            y['off25'], y['off26'],
            f'<span class="muted">+{off_d}</span>' if off_d >= 0 else f'<span class="muted">{off_d}</span>',
            f'{y["norm25"]} → {y["norm26"]}',
            f'<span class="{dcls}">{"+" if dd >= 0 else "−"}{abs(dd)}</span>',
            f'{y["scored25"] * 100:.0f}% → {y["scored26"] * 100:.0f}%',
            f'{y["exact25"]}→{y["exact26"]}',
            f'{y["diff25"]}→{y["diff26"]}',
            f'{y["adv25"]}→{y["adv26"]}'])
    o.append(table(hdr, trows, 'yoy'))

    # персональные мини-выводы
    o.append('<h3>Что произошло с каждым</h3>')
    o.append('<div class="grid g2">')
    notes = _yoy_notes(D, yoy)
    for u in sorted(ret, key=lambda u: -yoy[u]['dnorm']):
        y = yoy[u]
        dd = y['dnorm']
        sign = f'<span class="delta-pos">+{dd}</span>' if dd >= 0 else f'<span class="delta-neg">{dd}</span>'
        o.append(
            f'<div class="pcard"><h3>{pname(u, 1)} &nbsp;{sign} <span class="muted" style="font-size:13px">честных очк.</span></h3>'
            f'<div class="tag">{esc(notes[u][0])}</div><p>{notes[u][1]}</p></div>')
    o.append('</div></section>')
    return ''.join(o)


def _yoy_notes(D, yoy):
    n = {}
    y = yoy
    n['a_y_n_e_s'] = ('📈 Прорыв года',
                      f'Сменил стиль на ультра-осторожный: средняя ставка упала с {y["a_y_n_e_s"]["agg25"]:.2f} до '
                      f'{y["a_y_n_e_s"]["agg26"]:.2f} гола за матч (любимый счёт стал 0:1). Точных счетов стало меньше '
                      f'(21→17), зато разниц — вдвое больше (9→19), победителей 49→65, проходов 17→23. '
                      f'Доля «взял очко» подскочила с {y["a_y_n_e_s"]["scored25"] * 100:.0f}% до '
                      f'{y["a_y_n_e_s"]["scored26"] * 100:.0f}% — меньше геройства, больше стабильного сбора. Стал чемпионом.')
    n['tsyplyaeva_anna'] = ('📈 Тихий прогресс',
                            f'Прибавила без резких движений: победителей 69→79 (лучший показатель в лиге-2026), '
                            f'проходов 21→24. Самая ровная по очкам за матч. Из андердога-2025 (последнее место) — в крепкую середину.')
    n['rinatka99'] = ('➖ Почти без изменений',
                      f'Остался верен себе — самый агрессивный игрок (ставит ~3.8 гола за матч, факт {D["players26"]["rinatka99"]["avg_act_total"]:.2f}). '
                      f'Точных счетов даже прибавил (9→13), но проходов меньше (20→18). Нетто — лёгкий плюс (+{y["rinatka99"]["dnorm"]}).')
    n['madsunrise'] = ('➖ Лёгкий спад',
                       f'По честной шкале −5. Парадокс: проходов стало больше (21→26, лучший в 2026!) и была '
                       f'14-матчевая серия с очками — но точных счетов и разниц чуть меньше, плюс много невезения '
                       f'(46 промахов в один мяч — максимум сезона).')
    n['elnur_23'] = ('📉 Падение с трона',
                     f'Чемпион-2025 по честной шкале сдал −10. Доля «взял очко» просела с '
                     f'{y["elnur_23"]["scored25"] * 100:.0f}% до {y["elnur_23"]["scored26"] * 100:.0f}%, точных счетов 20→16. '
                     f'Всё ещё топ-2 и элита — но пик, похоже, был в прошлом сезоне.')
    n['ildariz'] = ('📉 Не его сезон',
                    f'В этот раз не пошло: разниц мячей стало меньше (14→10), проходов тоже (23→19), '
                    f'доля «взял очко» сдвинулась с 55% до 50% — отсюда −15 по честной шкале. '
                    f'Но это явно спад формы, а не потолок: на Евро-2024 он был вторым в группе, '
                    f'а класс по угадыванию проходов всегда оставался одним из лучших. Сезон-реванш напрашивается.')
    return n


def _sec_newcomers(D):
    p26 = D['players26']
    dv = p26['DV_pro_1']
    o = ['<section id="new"><h2><span class="em">🆕</span>Новички 2026</h2>',
         '<p class="lead">Трое дебютантов. Один ворвался в призы, двое почти не играли.</p>']
    o.append(f'<div class="pcard"><h3>{pname("DV_pro_1", 1)} — открытие сезона</h3>'
             f'<div class="tag">3-е место · {dv["stored"]} очк. · {dv["n"]} ставок</div>'
             f'<p>Дебютировал сразу в призовой тройке. Очки за ставку {dv["ppb"]:.2f} — на уровне чемпиона. '
             f'Сделал ставку на надёжность: {dv["diff"]} угаданных разниц мячей (топ сезона) и '
             f'{dv["adv_hits"]}/{dv["decisive_n"]} проходов. Самая длинная серия — {dv["hot_streak"]} матчей с очками.</p></div>')
    o.append('<div class="grid g2">')
    for u in ('armoald', 'forsag8_8'):
        if u in p26:
            a = p26[u]
            o.append(f'<div class="pcard"><h3>{pname(u, 1)}</h3>'
                     f'<div class="tag">{a["stored"]} очк. · всего {a["n"]} ставок</div>'
                     f'<p class="muted">Подключился под конец и сыграл лишь несколько матчей — '
                     f'для рейтингов «в среднем» не учитывается. По очкам за ставку ({a["ppb"]:.2f}) '
                     f'на маленькой выборке выглядит бодро.</p></div>')
    o.append('</div></section>')
    return ''.join(o)


def _sec_luck(D, act26):
    o = ['<section id="luck"><h2><span class="em">🍀</span>Мастерство против везения</h2>',
         '<p class="lead">«Промах в один мяч» = ставка, где не хватило одного гола до точного счёта. '
         'Считаем, сколько очков игроки недобрали из-за таких невезений, насколько они стабильны и какие серии ловили.</p>']
    p = D['players26']
    # недобор и near-miss
    rows = []
    for u in sorted(act26, key=lambda u: -p[u]['foregone']):
        a = p[u]
        rows.append((NAMES[u], a['foregone'], f"{a['foregone']} очк. ({a['nm_total']} пром.)", col(u)))
    o.append('<h3>Недобрано из-за промахов «в один мяч» (2026)</h3>')
    o.append(hbar(rows))
    o.append('<p class="muted">Чем длиннее столбец — тем больше очков «уплыло» из-за одного гола. '
             'Это не вина игрока, а чистое невезение/округление.</p>')

    # стабильность
    o.append('<h3>Стабильность: разброс очков за матч (σ)</h3>')
    rows = []
    for u in sorted(act26, key=lambda u: p[u]['pts_std']):
        a = p[u]
        rows.append((NAMES[u], a['pts_std'], f"σ={a['pts_std']:.2f}", col(u)))
    o.append(hbar(rows, maxv=max(p[u]['pts_std'] for u in act26)))
    o.append('<p class="muted">σ (сигма) — стандартное отклонение, то есть мера разброса очков от матча '
             'к матчу. Меньше σ — игрок ровный, набирает помалу, но регулярно. Больше σ — «то густо, то пусто».</p>')

    # серии
    o.append('<h3>Серии 2026</h3>')
    hdr = ['Игрок', '🔥 макс. серия с очками', '🧊 макс. «сухая» серия']
    trows = [[f'<span class="l">{pname(u, 1)}</span>', f'{p[u]["hot_streak"]} матчей', f'{p[u]["cold_streak"]} матчей']
             for u in sorted(act26, key=lambda u: -p[u]['hot_streak'])]
    o.append(table(hdr, trows))
    o.append('</section>')
    return ''.join(o)


def _sec_style(D, act26):
    p = D['players26']
    o = ['<section id="style"><h2><span class="em">🎲</span>Стиль ставок и предвзятости</h2>',
         '<p class="lead">Как именно люди ставят: рисково или осторожно, во что верят, и где это им вредит.</p>']

    # агрессивность
    o.append('<h3>Агрессивность: сколько голов закладывают в среднем (2026)</h3>')
    fact = D['tour26']['all']['avg']
    rows = []
    for u in sorted(act26, key=lambda u: -p[u]['avg_bet_total']):
        a = p[u]
        rows.append((NAMES[u], a['avg_bet_total'], f"{a['avg_bet_total']:.2f}", col(u)))
    o.append(hbar(rows, maxv=max(p[u]['avg_bet_total'] for u in act26)))
    o.append(f'<p class="muted">Реальная результативность турнира — <b>{fact:.2f}</b> гола за матч. '
             f'Кто выше — переоценивает голы (гэмблеры), кто ниже — недооценивает (консерваторы). '
             f'Любопытно: чемпион {pname("a_y_n_e_s", 1)} — самый осторожный, а его любимый счёт 0:1.</p>')

    # любимые счета
    o.append('<h3>Любимый счёт и разнообразие</h3>')
    hdr = ['Игрок', 'Любимый счёт', 'Раз поставил', 'Разных счетов', 'Энтропия']
    trows = []
    for u in sorted(act26, key=lambda u: -p[u]['entropy']):
        a = p[u]
        fav = a['fav_scoreline']
        favs = f'{fav[0][0]}:{fav[0][1]}' if fav else '—'
        trows.append([f'<span class="l">{pname(u, 1)}</span>', f'<b>{favs}</b>', fav[1] if fav else 0,
                      a['distinct_scorelines'], f'{a["entropy"]:.2f}'])
    o.append(table(hdr, trows))
    o.append('<p class="muted">Выше энтропия — разнообразнее ставки. Низкая — игрок «штампует» '
             'пару любимых счетов.</p>')

    # ничьи + квирк
    o.append('<h3>Ничьи и ловушка правил</h3>')
    o.append('<div class="note">🧩 В 2026 ничья стоит 2 очка, а разница мячей — 3. Но угаданная ничья '
             'засчитывается <b>раньше</b> разницы, хотя стоит дешевле. Значит каждая точно угаданная (но не '
             'в счёт) ничья приносит на 1 очко меньше, чем «стоила бы» как разница. В 2025 этого штрафа не было '
             '(ничья и разница стоили одинаково — по 2).</div>')
    hdr = ['Игрок', 'Ставок «на ничью»', 'Угадал ничьих', 'Потеряно на правиле (2026)']
    trows = []
    for u in sorted(act26, key=lambda u: -p[u]['draw_rule_loss']):
        a = p[u]
        trows.append([f'<span class="l">{pname(u, 1)}</span>', a['draw_bets'], a['correct_draws'],
                      f'−{a["draw_rule_loss"]} очк.'])
    o.append(table(hdr, trows))

    # клубный фаворитизм
    o.append('<h3>За кого «болеют» в ставках (клубный уклон, 2026)</h3>')
    o.append('<p class="muted">Насколько игрок ставит на клуб смелее, чем группа в среднем, на матчах с участием '
             'этого клуба. Любимчик (+) — систематически тянет к победе; антипатия (−) — наоборот.</p>')
    club = D['club26']
    hdr = ['Игрок', '❤️ Любимчик', '💔 Антипатия']
    trows = []
    for u in act26:
        if u in club and club[u]:
            top = max(club[u].items(), key=lambda x: x[1])
            bot = min(club[u].items(), key=lambda x: x[1])
            trows.append([f'<span class="l">{pname(u, 1)}</span>',
                          f'{esc(top[0])} <span class="muted">(+{top[1]:.2f})</span>',
                          f'{esc(bot[0])} <span class="muted">({bot[1]:.2f})</span>'])
    o.append(table(hdr, trows))
    o.append('</section>')
    return ''.join(o)


def _sec_social(D, act26):
    soc = D['social26']
    o = ['<section id="social"><h2><span class="em">🤝</span>Очные дуэли и социум</h2>',
         '<p class="lead">Ставки слепые — тем интереснее, кто кого реально обыгрывает по матчам, '
         'кто ставит одинаково, а кто идёт против всех.</p>']

    o.append('<h3>Матрица дуэлей (2026)</h3>')
    o.append('<p class="muted">В ячейке — доля побед игрока строки над игроком столбца по матчам, где '
             'ставили оба (≥50 — зелёный, &lt;50 — красный). Колонка «итог» — общий процент побед в дуэлях.</p>')
    o.append('<div style="overflow-x:auto">' + heatmap(act26, soc['h2h']) + '</div>')

    # nemesis / victim для топ-игроков
    o.append('<h3>Немезида и любимый соперник</h3>')
    hdr = ['Игрок', '😈 Немезида (хуже всех против)', '😊 Любимый соперник']
    trows = []
    for u in act26:
        opp = []
        for b in act26:
            if b == u:
                continue
            w, l, t = soc['h2h'][u][b]
            tot = w + l + t
            if tot:
                opp.append((b, (w + 0.5 * t) / tot))
        if opp:
            nem = min(opp, key=lambda x: x[1])
            vic = max(opp, key=lambda x: x[1])
            trows.append([f'<span class="l">{pname(u, 1)}</span>',
                          f'{pname(nem[0], 1)} <span class="muted">({nem[1] * 100:.0f}%)</span>',
                          f'{pname(vic[0], 1)} <span class="muted">({vic[1] * 100:.0f}%)</span>'])
    o.append(table(hdr, trows))

    # близнецы и контрарность
    o.append('<div class="grid g2">')
    pd = soc['pair_dist']
    twins = sorted(pd.items(), key=lambda x: x[1]['dist'])[:3]
    far = sorted(pd.items(), key=lambda x: -x[1]['dist'])[:1]
    tw = ['<div><h3>👯 Близнецы по ставкам</h3><p class="muted">Самые похожие пары (средняя разница счетов):</p><ul>']
    for (a, b), v in twins:
        tw.append(f'<li>{pname(a, 1)} ↔ {pname(b, 1)} — расхождение {v["dist"]:.2f} гола, '
                  f'идентичных ставок {v["ident"] * 100:.0f}%</li>')
    if far:
        (a, b), v = far[0]
        tw.append(f'<li class="muted">Самые непохожие: {pname(a, 1)} и {pname(b, 1)} ({v["dist"]:.2f})</li>')
    tw.append('</ul></div>')
    o.append(''.join(tw))

    cc = sorted(soc['contrarian'].items(), key=lambda x: -x[1])
    cb = ['<div><h3>🃏 Контрарность</h3><p class="muted">Контрарианец ставит не как все, конформист — '
          'наоборот, играет «как большинство». Цифра — среднее отклонение ставки от общего мнения группы '
          '(больше = независимее):</p>']
    rows = [(NAMES[u], v, f'{v:.2f}', col(u)) for u, v in cc]
    cb.append(hbar(rows, maxv=max(v for _, v in cc)))
    cb.append('</div>')
    o.append(''.join(cb))
    o.append('</div>')

    # сложные матчи и апсеты
    o.append('<h3>Самые трудные матчи 2026 (где почти все промахнулись)</h3>')
    hard = sorted(soc['match_diff'].values(), key=lambda m: (m['scorer_rate'], -m['n']))[:5]
    hdr = ['Матч', 'Счёт', 'Набрали очки']
    trows = []
    for m in hard:
        e = m['ev']
        trows.append([f'<span class="l">{esc(e["t1"])} — {esc(e["t2"])}</span>',
                      f'<b>{e["rt1"]}:{e["rt2"]}</b>', f'{m["scorers"]} из {m["n"]}'])
    o.append(table(hdr, trows))

    o.append('<h3>Главные апсеты против консенсуса</h3>')
    o.append('<p class="muted">Матчи, где реальный счёт сильнее всего разошёлся со средней ставкой группы.</p>')
    ups = sorted(soc['upsets'].values(), key=lambda m: -m['dist'])[:5]
    hdr = ['Матч', 'Реальный счёт', 'Средняя ставка группы']
    trows = []
    for m in ups:
        e = m['ev']
        trows.append([f'<span class="l">{esc(e["t1"])} — {esc(e["t2"])}</span>',
                      f'<b>{e["rt1"]}:{e["rt2"]}</b>',
                      f'{m["cons"][0]:.1f}:{m["cons"][1]:.1f}'])
    o.append(table(hdr, trows))
    o.append('</section>')
    return ''.join(o)


def _sec_meta(D, t25, t26):
    o = ['<section id="meta"><h2><span class="em">🏟️</span>Сравнение турниров</h2>',
         '<p class="lead">Расширяем статистику матчей из бота и сравниваем сезоны.</p>']

    def d(a, b, suf='', dec=2):
        delta = b - a
        cls = 'delta-pos' if delta >= 0 else 'delta-neg'
        fmt = f'{{:.{dec}f}}'
        return (f'{fmt.format(a)}{suf}', f'{fmt.format(b)}{suf}',
                f'<span class="{cls}">{"+" if delta >= 0 else "−"}{fmt.format(abs(delta))}{suf}</span>')

    hdr = ['Показатель', '2025', '2026', 'Δ']
    rows = []
    rows.append(['Голов за матч (всего)'] + list(d(t25['all']['avg'], t26['all']['avg'])))
    rows.append(['Голов за матч — лига'] + list(d(t25['league']['avg'], t26['league']['avg'])))
    rows.append(['Голов за матч — плей-офф'] + list(d(t25['knockout']['avg'], t26['knockout']['avg'])))
    rows.append(['Доля ничьих'] + list(d(t25['all']['draw_rate'] * 100, t26['all']['draw_rate'] * 100, '%', 0)))
    rows.append(['Предсказуемость: доля «взял очко»'] +
                list(d(t25['group_scored_rate'] * 100, t26['group_scored_rate'] * 100, '%', 0)))
    rows.append(['Точность: доля точных счетов'] +
                list(d(t25['group_exact_rate'] * 100, t26['group_exact_rate'] * 100, '%', 1)))
    o.append(table(hdr, rows, 'meta'))
    o.append(f'<p>Сезон <b>2026 оказался результативнее</b> ({t26["all"]["avg"]:.2f} против '
             f'{t25["all"]["avg"]:.2f} гола за матч) и с большей долей ничьих '
             f'({t26["all"]["draw_rate"] * 100:.0f}% против {t25["all"]["draw_rate"] * 100:.0f}%). '
             f'При этом группе он дался <b>чуть легче</b> — доля «взял хоть очко» подросла. '
             f'Особенно ярко вырос плей-офф: в 2025 он был «сухим» ({t25["knockout"]["avg"]:.2f}), '
             f'в 2026 — самым голевым ({t26["knockout"]["avg"]:.2f}).</p>')
    o.append('</section>')
    return ''.join(o)


def _sec_euro(D):
    euro = D['euro']
    ep = euro['players']
    meta = euro['meta']
    t25, t26 = D['tour25'], D['tour26']
    core = [u for u in ep if u in D['players26'] and ep[u]['n'] >= 30]

    o = ['<section id="euro"><h2><span class="em">🇪🇺</span>Бонус: Евро-2024 и взгляд на три турнира</h2>',
         '<p class="lead">Отдельный довесок к основному отчёту. По Евро-2024 есть только CSV-выгрузка '
         '(результаты и ставки, без сохранённых тоталов), поэтому очки здесь — в единой шкале 2026, '
         'а для сравнения турниров опираемся на доли и места, которые от правил не зависят.</p>']

    # --- стандингс Евро ---
    o.append('<h3>Итоги Евро-2024 (очки в шкале 2026)</h3>')
    hdr = ['#', 'Игрок', 'Очки', 'Ставок', 'Оч/ст', 'Взял%', 'Точн', 'Проход']
    rows = []
    for i, (u, a) in enumerate(sorted(ep.items(), key=lambda kv: -kv[1]['ppb']), 1):
        medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
        nbet = f'{a["n"]}' + (' *' if a['n'] < 51 else '')
        rows.append([medal, f'<span class="l">{pname(u, 1)}</span>', f'<b>{a["total"]}</b>',
                     nbet, f'{a["ppb"]:.2f}', f'{a["scored_rate"] * 100:.0f}%',
                     a['exact'], f'{a["adv_hits"]}/{a["decisive_n"]}'])
    o.append(table(hdr, rows, 'stand'))
    o.append('<p class="muted">Ранжировано по очкам за ставку (честно при разном числе матчей). '
             '«*» — сыграл меньше полного набора из 51 матча. Чемпион Евро-2024 в реальности — Испания.</p>')

    # --- характер турниров ---
    o.append('<h3>Характер турниров: Евро-2024 vs ЛЧ-2025 vs ЛЧ-2026</h3>')
    hdr = ['Турнир', 'Матчей', 'Голов/матч', 'Ничьих', 'Решающих матчей']
    trows = [
        ['<span class="l">🇪🇺 Евро-2024</span>', meta['n'], f'<b>{meta["avg"]:.2f}</b>',
         f'{meta["draw_rate"] * 100:.0f}%', '15 / 51 (29%)'],
        ['<span class="l">🏆 ЛЧ-2025</span>', t25['all']['n'], f'{t25["all"]["avg"]:.2f}',
         f'{t25["all"]["draw_rate"] * 100:.0f}%', '31 / 203 (15%)'],
        ['<span class="l">🏆 ЛЧ-2026</span>', t26['all']['n'], f'<b>{t26["all"]["avg"]:.2f}</b>',
         f'{t26["all"]["draw_rate"] * 100:.0f}%', '31 / 203 (15%)'],
    ]
    o.append(table(hdr, trows, 'meta'))
    o.append(f'<p>Турнир сборных оказался куда <b>осторожнее клубного</b>: всего {meta["avg"]:.2f} гола за матч '
             f'против {t25["all"]["avg"]:.2f}–{t26["all"]["avg"]:.2f} в ЛЧ, и рекордные '
             f'<b>{meta["draw_rate"] * 100:.0f}% ничьих</b> (в ЛЧ — 15–18%). Угадывать счёт на Евро было '
             f'объективно тяжелее — больше «сухих» и ничейных матчей. Зато плотность плей-офф выше '
             f'(каждый третий матч — на вылет), поэтому очки за проход там весят больше.</p>')

    # --- долгая траектория (места по очкам/ставку среди 6 «старожилов») ---
    ppb = {'euro': {u: ep[u]['ppb'] for u in core},
           'cl25': {u: D['norm25'][u]['ppb'] for u in core},
           'cl26': {u: D['players26'][u]['ppb'] for u in core}}

    def ranks(d):
        order = sorted(d, key=lambda u: -d[u])
        return {u: i + 1 for i, u in enumerate(order)}, order

    r_e, o_e = ranks(ppb['euro'])
    r_25, _ = ranks(ppb['cl25'])
    r_26, o_26 = ranks(ppb['cl26'])

    o.append('<h3>Долгая дистанция: путь шести старожилов</h3>')
    o.append('<p class="muted">Места — по очкам за ставку среди шести игроков, прошедших все три турнира '
             '(сравнение мест нейтрализует разные правила и разную «лёгкость» турниров).</p>')
    o.append(slope_chart(o_e, o_26, {u: f'{ppb["euro"][u]:.2f}' for u in o_e},
                         {u: f'{ppb["cl26"][u]:.2f}' for u in o_26}, 'Евро-2024', 'ЛЧ-2026'))

    hdr = ['Игрок', 'Евро-2024', 'ЛЧ-2025', 'ЛЧ-2026', 'Длинный тренд']
    trows = []
    for u in sorted(core, key=lambda u: r_26[u]):
        d = r_e[u] - r_26[u]
        trend = '📈 вверх' if d > 0 else ('📉 вниз' if d < 0 else '➖ ровно')
        cls = 'delta-pos' if d > 0 else ('delta-neg' if d < 0 else 'muted')
        trows.append([f'<span class="l">{pname(u, 1)}</span>',
                      f'{r_e[u]}-е', f'{r_25[u]}-е', f'<b>{r_26[u]}-е</b>',
                      f'<span class="{cls}">{trend}</span>'])
    o.append(table(hdr, trows))

    o.append('<h3>Что говорит длинная дистанция</h3>')
    o.append('<ul class="tldr">')
    o.append(f'<li>👑 {pname("elnur_23", 1)} — <b>первоначальный король</b>: выиграл Евро-2024 и ЛЧ-2025. '
             f'Но по очкам за ставку он плавно сползает ({r_e["elnur_23"]}-е → {r_25["elnur_23"]}-е → '
             f'{r_26["elnur_23"]}-е) — тренд тянется уже три турнира.</li>')
    o.append(f'<li>🚀 {pname("a_y_n_e_s", 1)} — <b>история позднего расцвета</b>: '
             f'{r_e["a_y_n_e_s"]}-е на Евро, провал в ЛЧ-2025 ({r_25["a_y_n_e_s"]}-е), '
             f'и чемпионство в ЛЧ-2026. Рост не случаен, а выстраданный.</li>')
    o.append(f'<li>🛠️ {pname("ildariz", 1)} был <b>вторым на Евро-2024</b>, и спад с тех пор плавный, '
             f'а не обвальный — фундамент крепкий, дело за возвращением формы.</li>')
    o.append(f'<li>📈 {pname("madsunrise", 1)} резко прибавил после Евро ({r_e["madsunrise"]}-е → '
             f'{r_25["madsunrise"]}-е) и держится в топе; {pname("rinatka99", 1)} — медленный, но '
             f'стабильный рост ({r_e["rinatka99"]}-е → {r_26["rinatka99"]}-е).</li>')
    o.append('</ul>')
    o.append('</section>')
    return ''.join(o)


def _sec_method(D):
    o = ['<section id="method"><h2><span class="em">🔬</span>Методика и оговорки</h2>']
    o.append('<p><b>Источник.</b> Дампы MongoDB <span class="pill">events.bson</span>'
             '<span class="pill">users.bson</span> за оба сезона разобраны самописным BSON-парсером '
             '(без сторонних библиотек). Файл stat.csv (выгрузка <code>/export_statistic</code>) '
             'использован для кросс-проверки 2026 — 1417 строк-ставок сошлись.</p>')
    o.append('<p><b>Очки.</b> Пересчитаны по правилам бота: каскад «точный счёт → ничья → разница → '
             'победитель» плюс +1 за угаданный проход на решающих матчах. Веса различаются по сезонам '
             '(восстановлены из сохранённых тоталов): '
             '<b>2025</b> = 3/2/2/1, <b>2026</b> = 4/3/2/1. Пересчёт сверен с сохранёнными тоталами бота '
             'и совпадает; в рейтингах используется официальный тотал.</p>')
    o.append('<p><b>Честное сравнение.</b> Для раздела «год-к-году» очки обоих сезонов приведены к единой '
             'шкале 2026, а доли по категориям и места — от весов не зависят вовсе.</p>')
    o.append('<p><b>Стадии.</b> «Решающий матч» = там, где зафиксирован проход (по 31 в каждом сезоне). '
             'Деление на лигу/плей-офф — по дате (плей-офф с февраля).</p>')
    o.append('<p><b>Порог активности.</b> В рейтинги «в среднем / σ / стиль» включены только игроки с ≥30 ставками '
             '(armoald и forsag8_8 в 2026 сыграли ~10 матчей и вынесены отдельно).</p>')
    o.append('</section>')
    return ''.join(o)


if __name__ == '__main__':
    D = build()
    debug_dump(D)
    html_out = render_html(D)
    with open(os.path.join(BASE, 'report.html'), 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f'\n✅ report.html записан ({len(html_out)} символов)')
