#!/usr/bin/env python3
"""Download daily closes for the RRG app and write site/data/{meta,d,w}.json.

Runs on GitHub Actions on a schedule. Standard library only, so the job needs no installs.

  quick mode  fetches the last month for each symbol and merges it into the data already
              published on the live site (fast, used every 30 minutes in US hours)
  full mode   re-downloads ~10 years for every symbol (used once a day after the close,
              and for any symbol that is new or whose recent prices don't line up)

Prices are Yahoo Finance daily closes (split-adjusted, not dividend-adjusted), with Stooq
as a backup for US tickers when Yahoo fails.
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY_KEEP = 520           # ~2 years of daily bars in the app
WEEKLY_KEEP = 520          # ~10 years of weekly bars
MAX_SYMBOLS = 260
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
SYM_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-^=]{0,19}$')


class NotFound(Exception):
    pass


class FetchError(Exception):
    pass


def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*', 'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        return data


def day_of(ts, offset):
    return dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).strftime('%Y-%m-%d')


def monday_of(day):
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


# ───────────────────────── sources ─────────────────────────
def yahoo(sym, rng):
    """Return ({date: close}, info) from Yahoo's chart API."""
    last = 'no answer'
    for host in ('query1', 'query2'):
        for attempt in range(3):
            url = (f'https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}'
                   f'?range={rng}&interval=1d&includePrePost=false')
            try:
                j = json.loads(http_get(url))
            except urllib.error.HTTPError as e:
                body = ''
                try:
                    body = e.read().decode('utf-8', 'replace')
                except Exception:
                    pass
                if e.code == 404:
                    try:
                        desc = (json.loads(body).get('chart') or {}).get('error', {}).get('description')
                    except Exception:
                        desc = None
                    raise NotFound(str(desc or 'not found on Yahoo Finance')[:120])
                last = f'Yahoo HTTP {e.code}'
                time.sleep((6 if e.code == 429 else 2) * (attempt + 1) + random.random() * 2)
                continue
            except Exception as e:  # network trouble
                last = f'Yahoo {type(e).__name__}'
                time.sleep(2 * (attempt + 1))
                continue
            chart = j.get('chart') or {}
            res = chart.get('result')
            if not res:
                err = chart.get('error') or {}
                raise NotFound(str(err.get('description') or 'not found on Yahoo Finance')[:120])
            r = res[0]
            meta = r.get('meta') or {}
            off = meta.get('gmtoffset') or 0
            ts = r.get('timestamp') or []
            q = ((r.get('indicators') or {}).get('quote') or [{}])[0]
            closes = q.get('close') or []
            bars = {}
            for t, c in zip(ts, closes):
                if c is not None and c > 0:
                    bars[day_of(t, off)] = float(c)
            # the live price for today, when the daily bar hasn't caught up yet
            rmp, rmt = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
            if rmp and rmt and bars:
                d = day_of(rmt, off)
                if d >= max(bars):
                    bars[d] = float(rmp)
            if not bars:
                raise NotFound('no prices on Yahoo Finance')
            info = {'name': meta.get('longName') or meta.get('shortName') or '',
                    'currency': meta.get('currency') or '',
                    'exchange': meta.get('fullExchangeName') or meta.get('exchangeName') or ''}
            return bars, info
    raise FetchError(last)


def stooq(sym):
    """Backup for plain US tickers: full daily history as {date: close}."""
    if not re.fullmatch(r'[A-Z]{1,5}', sym):
        raise FetchError('no backup source for this ticker')
    url = f'https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d'
    try:
        text = http_get(url).decode('utf-8', 'replace')
    except Exception as e:
        raise FetchError(f'Stooq {type(e).__name__}')
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [h.strip().lower() for h in rows[0][:5]] != ['date', 'open', 'high', 'low', 'close']:
        raise FetchError('Stooq returned no data')
    bars = {}
    for r in rows[1:]:
        try:
            c = float(r[4])
            if c > 0:
                bars[r[0]] = c
        except (ValueError, IndexError):
            pass
    if not bars:
        raise FetchError('Stooq returned no data')
    return bars, {}


# ───────────────────────── previous data ─────────────────────────
def load_json(base, name):
    if not base:
        return None
    try:
        if base.startswith('http'):
            return json.loads(http_get(base.rstrip('/') + '/data/' + name + '?t=' + str(int(time.time()))))
        with open(os.path.join(base, 'data', name)) as f:
            return json.load(f)
    except Exception as e:
        log(f'  (no previous {name}: {type(e).__name__})')
        return None


def unpack(obj):
    """{dates:[...], c:{sym:[...]}} -> {sym: {date: close}}"""
    out = {}
    if not obj or 'dates' not in obj:
        return out
    dates = obj['dates']
    for s, arr in (obj.get('c') or {}).items():
        out[s] = {d: v for d, v in zip(dates, arr) if v is not None}
    return out


def pack(series, keep):
    dates = sorted({d for s in series.values() for d in s})[-keep:]
    first = dates[0] if dates else None
    return {'dates': dates,
            'c': {s: [round_px(v.get(d)) for d in dates] for s, v in series.items() if any(d >= first for d in v)}}


def round_px(v):
    return None if v is None else float(f'{v:.6g}')


def to_weekly(daily):
    wk = {}
    for d in sorted(daily):
        wk[monday_of(d)] = daily[d]      # last trading day of the week wins
    return wk


def lines_up(old, new):
    """True when the overlap agrees (ignoring the last two bars, which can still change)."""
    common = sorted(d for d in new if d in old)[:-2]
    return all(abs(new[d] / old[d] - 1) < 0.015 for d in common[-10:])


# ───────────────────────── main ─────────────────────────
def read_universe():
    with open(os.path.join(ROOT, 'scripts', 'universe.json')) as f:
        uni = json.load(f)
    groups = {g: list(ss) for g, ss in uni['groups'].items()}
    names = dict(uni['names'])
    extra = []
    path = os.path.join(ROOT, 'tickers.txt')
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                t = line.split('#', 1)[0].strip().upper()
                if ':' in t:                       # "NASDAQ:SMH" -> "SMH"
                    t = t.rsplit(':', 1)[1]
                if t and SYM_RE.match(t) and t not in extra:
                    extra.append(t)
    known = {s for ss in groups.values() for s in ss}
    added = [t for t in extra if t not in known]
    if added:
        groups['Your tickers'] = added
    syms = [s for ss in groups.values() for s in ss][:MAX_SYMBOLS]
    return groups, names, syms, uni.get('watchlist', [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['quick', 'full'], default='quick')
    ap.add_argument('--prev', default=os.environ.get('PAGES_URL', ''), help='live site URL (or folder) with the last published data')
    ap.add_argument('--out', default=os.path.join(ROOT, 'site'))
    ap.add_argument('--pause', type=float, default=0.25)
    a = ap.parse_args()

    groups, names, syms, watch = read_universe()
    log(f'{len(syms)} symbols, mode={a.mode}, previous data from {a.prev or "(none)"}')
    prev_meta = load_json(a.prev, 'meta.json') or {}
    prev_d = unpack(load_json(a.prev, 'd.json'))
    prev_w = unpack(load_json(a.prev, 'w.json'))
    prev_info = prev_meta.get('symbols') or {}

    daily, weekly, info = {}, {}, {}
    counts = {'quick': 0, 'full': 0, 'kept': 0, 'failed': 0}
    started = time.time()
    for i, s in enumerate(syms):
        pi = prev_info.get(s) or {}
        old_d, old_w = prev_d.get(s), prev_w.get(s)
        rec = {'name': names.get(s) or pi.get('name') or '', 'group': next(g for g, ss in groups.items() if s in ss)}
        done = False
        can_quick = a.mode == 'quick' and old_d and old_w and pi.get('src') == 'yahoo' and pi.get('status') == 'ok'
        if can_quick:
            try:
                new, meta = yahoo(s, '1mo')
                if lines_up(old_d, new):
                    d = dict(old_d); d.update(new)
                    w = dict(old_w)
                    for day in sorted(d):
                        if day >= min(new):
                            w[monday_of(day)] = d[day]
                    daily[s], weekly[s] = d, w
                    rec.update(src='yahoo', status='ok', fetched=a.mode)
                    rec['name'] = rec['name'] or meta.get('name', '')
                    counts['quick'] += 1
                    done = True
                else:
                    log(f'  {s}: recent prices changed (split?) - downloading full history')
            except NotFound as e:
                rec.update(status='failed', error=str(e))
            except FetchError as e:
                rec['error'] = str(e)
        if not done and rec.get('status') != 'failed':
            try:
                try:
                    d, meta = yahoo(s, '10y')
                    src = 'yahoo'
                except (FetchError, NotFound) as e1:
                    try:
                        d, meta = stooq(s)
                        src = 'stooq'
                    except FetchError:
                        raise e1
                daily[s], weekly[s] = d, to_weekly(d)
                rec.update(src=src, status='ok', fetched='full')
                rec.pop('error', None)
                rec['name'] = rec['name'] or meta.get('name', '')
                counts['full'] += 1
                done = True
            except (FetchError, NotFound) as e:
                rec.update(status='failed', error=str(e))
        if not done:
            if old_d and old_w:                    # keep the last good prices, flagged as stale
                daily[s], weekly[s] = old_d, old_w
                rec.update(status='stale', src=pi.get('src', ''), last_ok=pi.get('last_ok') or prev_meta.get('updated'))
                counts['kept'] += 1
            else:
                counts['failed'] += 1
            log(f'  {s}: {rec.get("error")}')
        else:
            rec['last_ok'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        if s in daily:
            rec['last'] = max(daily[s])
        info[s] = rec
        if (i + 1) % 25 == 0:
            log(f'  {i + 1}/{len(syms)} done ({time.time() - started:.0f}s)')
        time.sleep(a.pause)

    ok = sum(1 for r in info.values() if r['status'] == 'ok')
    log(f'ok={ok} quick={counts["quick"]} full={counts["full"]} kept-old={counts["kept"]} failed={counts["failed"]} in {time.time() - started:.0f}s')
    if not daily:
        log('No prices at all - leaving the published data untouched.')
        sys.exit(1)

    now = dt.datetime.now(dt.timezone.utc)
    meta = {'updated': now.isoformat(timespec='seconds'), 'mode': a.mode, 'source': 'Yahoo Finance',
            'ok': ok, 'total': len(syms), 'groups': groups, 'watchlist': watch, 'symbols': info}
    os.makedirs(os.path.join(a.out, 'data'), exist_ok=True)
    for name, obj in (('d.json', pack(daily, DAILY_KEEP)), ('w.json', pack(weekly, WEEKLY_KEEP)), ('meta.json', meta)):
        with open(os.path.join(a.out, 'data', name), 'w') as f:
            json.dump(obj, f, separators=(',', ':'), allow_nan=False)
        log(f'wrote data/{name} ({os.path.getsize(os.path.join(a.out, "data", name)) // 1024} KB)')
    # one-line summary on the run's page in the Actions tab
    lasts = [r.get('last') for r in info.values() if r.get('last')]
    latest = max(set(lasts), key=lasts.count) if lasts else '-'
    srcs = {k: sum(1 for r in info.values() if r.get('src') == k and r['status'] == 'ok') for k in ('yahoo', 'stooq')}
    print(f'::notice title=Price update::{ok}/{len(syms)} symbols OK ({counts["quick"]} quick, {counts["full"]} full; '
          f'Yahoo {srcs["yahoo"]}, Stooq {srcs["stooq"]}); latest bar {latest}; previous data '
          f'{"found" if prev_d else "not found"}', flush=True)
    bad = [f'{s} ({r["status"]}: {r.get("error", "")[:60]})' for s, r in info.items() if r['status'] != 'ok']
    if bad:
        print('::warning title=Symbols without fresh prices::' + '; '.join(bad)[:900], flush=True)
    if ok == 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
