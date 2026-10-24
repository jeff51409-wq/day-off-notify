"""
盯人事行政總處的停班停課表，臺北／新北「某一天放不放假」的結論一變就推到 Telegram；颱風警報只報發布／等級變更／解除。
GitHub Actions 接力每分鐘跑一次；上次的狀態存在 state.json（有變才 commit）。

環境變數：TG_TOKEN（機器人代碼）、TG_CHAT（聊天室 ID）。沒設就只印出來（本機測試用）。
"""
import html
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

URL = 'https://www.dgpa.gov.tw/typh/daily/nds.html'
SITE = 'https://www.dgpa.gov.tw/'
CWA = 'https://www.cwa.gov.tw'
CITIES = ['臺北市', '新北市']
NONE = '目前沒有停班停課訊息'   # 平常沒颱風時，表上找不到該縣市
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
TW = timezone(timedelta(hours=8))
FAIL_ALERT = 6   # 連續 6 次（約 30 分鐘）抓不到才警告
GAP_ALERT = 15   # 上一棒結束到這一棒開始超過 15 分鐘，就算中斷過


def fetch(url=URL, raw=False):
    # 新版 Python 的嚴格模式會擋 dgpa 的憑證（缺 Subject Key Identifier），只關掉這一項，其餘照常驗證
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        data = r.read()
    return data if raw else data.decode('utf-8', 'replace')


def clean(s):
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = html.unescape(re.sub(r'<[^>]+>', '', s))
    return '\n'.join(line.strip() for line in s.splitlines() if line.strip())


def parse(page):
    m = re.search(r'更新時間：\s*([\d/]+\s+[\d:]+)', page)
    updated = m.group(1) if m else ''
    status = {}
    for city in CITIES:
        # <TD ...><FONT>臺北市</FONT></TD><TD ...>狀態</TD>
        m = re.search(r'<FONT>\s*' + city + r'\s*</FONT>\s*</TD>\s*<TD[^>]*>(.*?)</TD>', page, re.S | re.I)
        status[city] = clean(m.group(1)) if m else NONE
    return updated, status


WEEK = '一二三四五六日'


def day_lines(updated, s):
    """把官方原文拆成「哪一天 → 白話」：{'2026-10-24': '放假', ...}，另回傳有沒有區域性的行。
    今天／明天 換成實際日期，這樣半夜官網把「明天放假」改寫成「今天放假」不會被當成新消息。"""
    base = datetime.strptime(updated[:10], '%Y/%m/%d').date() if updated else datetime.now(TW).date()
    days, partial = {}, False
    if s == NONE:
        return days, partial
    for l in s.split('\n'):
        m = re.match(r'(今天|明天|(\d+)月(\d+)日|(\d+)日)', l)
        if not m:
            partial = True
            continue
        if m.group(1) == '今天':
            d = base
        elif m.group(1) == '明天':
            d = base + timedelta(days=1)
        else:
            mon = int(m.group(2) or base.month)
            d = base.replace(month=mon, day=int(m.group(3) or m.group(4)))
        rest = l[m.end():].lstrip('（(').split('）')[-1].split(')')[-1]
        if '尚未宣布' in l:
            word = '還沒宣布'
        elif '停止上班' in l:
            when = rest.split('停止上班')[0].replace('已達', '').rstrip('、，, ')
            word = (when + '放假') if '起' in when else '放假'
        elif '照常上班' in l:
            word = '照常上班' + ('（但學校停課）' if '停止上課' in l else '')
        elif '停止上課' in l:
            word = '照常上班（只有學校停課）'
        else:
            word = l
        days.setdefault(d.isoformat(), word)
    return days, partial


def day_label(iso, today):
    d = datetime.strptime(iso, '%Y-%m-%d').date()
    rel = {0: '今天', 1: '明天', 2: '後天'}.get((d - today).days, '')
    return f'{rel} {d.month}/{d.day}（{WEEK[d.weekday()]}）'.strip()


def report(prev, prev_updated, updated, status, today):
    """決定要不要通知、傳什麼。回傳 (訊息, 靜音) 或 None。
    只在「今天以後某一天的結論變了」才響；官網改寫措辭、半夜換日、隔天下午清掉當天公告都不吵你。"""
    if prev_updated and updated and updated < prev_updated:
        return None   # 官網不同主機回舊版（10/23 20:05→19:59→20:05 來回跳過），舊的不理
    now_days = {c: day_lines(updated, status[c]) for c in CITIES}
    old_days = {c: day_lines(prev_updated, prev.get(c, NONE)) for c in CITIES}
    t = today.isoformat()
    changed = False
    for c in CITIES:
        new, old = now_days[c][0], old_days[c][0]
        for d in set(new) | set(old):
            if new.get(d) != old.get(d) and (d > t or (d == t and d in new)):
                changed = True
    partial_changed = any(now_days[c][1] and status[c] != prev.get(c) for c in CITIES)
    if not changed and not partial_changed:
        return None

    dates = sorted({d for c in CITIES for d in now_days[c][0] if d >= t}, reverse=True)
    lines = [f'🌀 停班停課有新消息（官方 {updated[5:16]}）', '']
    if not dates:
        lines.append('臺北、新北目前都沒有停班停課')
    for d in dates:
        lines.append(f'📅 {day_label(d, today)}')
        lines += [f'　{c}：{now_days[c][0].get(d, "沒有停班停課")}' for c in CITIES]
    if any(now_days[c][1] for c in CITIES):
        lines.append('⚠ 有部分區域不一樣，看下面原文')
    lines += ['', '── 官方原文 ──']
    lines += [f'{c}：{status[c]}' for c in CITIES]
    return '\n'.join(lines + ['', SITE]), not changed


def send(text, silent=False):
    token, chat = os.environ.get('TG_TOKEN'), os.environ.get('TG_CHAT')
    if not token or not chat:
        print('--- 沒設 TG_TOKEN／TG_CHAT，只印出' + ('（靜音）' if silent else '') + ' ---\n' + text)
        return
    data = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true',
                                   'disable_notification': str(silent).lower()}).encode()
    urllib.request.urlopen(f'https://api.telegram.org/bot{token}/sendMessage', data, timeout=30).read()


def send_photo(png, caption, silent=False):
    token, chat = os.environ.get('TG_TOKEN'), os.environ.get('TG_CHAT')
    if not token or not chat:
        print(f'--- 沒設 TG_TOKEN／TG_CHAT，只印出（圖片 {len(png)} bytes' + ('，靜音' if silent else '') + '）---\n' + caption)
        return
    b = 'xxBOUNDARYxx'
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (('chat_id', chat), ('caption', caption), ('disable_notification', str(silent).lower()))]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="pta.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + f'\r\n--{b}--\r\n'.encode())
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendPhoto', b''.join(parts),
                                 {'Content-Type': f'multipart/form-data; boundary={b}'})
    urllib.request.urlopen(req, timeout=60).read()


def main():
    if os.environ.get('GITHUB_ACTIONS') and not os.environ.get('TG_TOKEN'):
        print('還沒設定 Telegram，先不記錄狀態')   # 不然設好之後收不到「已啟動」那則
        return
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding='utf-8') as f:
            state = json.load(f)
    before = json.dumps(state, sort_keys=True)
    state['month'] = datetime.now(TW).strftime('%Y-%m')   # 每月至少 commit 一次，GitHub 才不會因為 60 天沒動靜停掉排程

    dayoff(state)
    try:
        typhoon(state)
    except Exception as e:   # 氣象署那邊壞掉不影響停班停課
        print('颱風資料失敗：', e)

    if json.dumps(state, sort_keys=True) != before:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)


def gap(prev_end):
    """每一棒開始時跑一次：跟上一棒結束時間（GitHub API 給的 UTC）比，斷太久就回報。"""
    prev_end = prev_end.strip()
    if not prev_end:
        return
    end = datetime.fromisoformat(prev_end.replace('Z', '+00:00')).astimezone(TW)
    now = datetime.now(TW)
    mins = int((now - end).total_seconds() // 60)
    print(f'上一棒結束 {end:%m/%d %H:%M}，中斷 {mins} 分鐘')
    if mins > GAP_ALERT:
        send(f'⚠ 停班停課機器人剛剛中斷了 {mins} 分鐘（{end:%m/%d %H:%M}～{now:%H:%M}），現在已恢復。\n'
             f'中斷期間如果臺北／新北狀態有變，下一則會馬上補上；沒收到就是沒變。')


def dayoff(state):
    prev = state.get('status', {})
    fails = state.get('fails', 0)
    try:
        updated, status = parse(fetch())
    except Exception as e:   # 偶爾連不上就等下一輪；連續 FAIL_ALERT 次才警告，免得以為「沒消息」其實是壞掉
        print('抓取失敗：', e)
        state['fails'] = fails + 1
        if state['fails'] == FAIL_ALERT:
            send(f'⚠ 已經 30 分鐘抓不到人事行政總處網站，這段時間的停班停課消息可能收不到，請先直接看官網：\n{SITE}\n\n（錯誤：{e}）')
        return
    if fails >= FAIL_ALERT:
        send('✅ 已恢復連線，繼續幫你盯停班停課。')
    state['fails'] = 0

    if not prev:
        lines = ['✅ 停班停課通知已啟動，之後狀態一有變動就會通知你', '']
        lines += [f'{c}：{status[c]}' for c in CITIES]
        send('\n'.join(lines + ['', f'官方更新時間 {updated}', SITE]))
    else:
        msg = report(prev, state.get('updated', ''), updated, status, datetime.now(TW).date())
        if msg:
            send(*msg)
    if status != prev and not (state.get('updated') and updated < state['updated']):
        state['status'], state['updated'] = status, updated
    print(updated, status)


def js_str(src, key):
    """從氣象署的 JS 資料檔裡取 'key':'值'（單引號字串，處理 \\n、\\' 跳脫）。"""
    m = re.search(r"'" + key + r"'\s*:\s*'((?:\\.|[^'\\])*)'", src)
    return m.group(1).encode().decode('unicode_escape').encode('latin-1').decode('utf-8') if m else ''


def pta_png():
    """氣象署最新一張路徑潛勢圖；抓不到就算了。"""
    d = fetch(CWA + '/Data/js/typhoon/TY_NEWS-Data.js?T=' + str(int(datetime.now().timestamp())))
    m = re.search(r"TY_DataTime\s*=\s*'(\d+)'", d)
    if not m:
        return None
    imgs = json.loads(fetch(f'{CWA}/Data/typhoon/TY_NEWS/PTA_IMGS_{m.group(1)}_zhtw.json'))
    return fetch(f'{CWA}/Data/typhoon/TY_NEWS/{imgs["WHOLE"][-1]}', raw=True)


def typhoon(state):
    """颱風警報只在發布、等級變更、解除時傳一則（警報內容＋路徑圖合在一起）；每小時的例行報不傳。"""
    ty = state.setdefault('ty', {})
    ty.pop('pta', None)
    w = fetch(CWA + '/Data/js/warn/Warning_Content.js?T=' + str(int(datetime.now().timestamp())))
    i = w.find("'TY_WARN':{")   # 前面的 WarnAll 清單也有 'TY_WARN'，要找物件本身
    block = w[i:w.find("'E'", i)] if i >= 0 else ''
    title, issued, validto = js_str(block, 'title'), js_str(block, 'issued'), js_str(block, 'validto')
    content = re.sub(r'^發布時間：.*?\n+', '', js_str(block, 'content')).strip()
    now = datetime.now(TW).strftime('%Y/%m/%d %H:%M')
    active = bool(issued) and validto > now and '解除' not in title + content[:30]

    if issued and issued != ty.get('issued') and (title != ty.get('title') or active != ty.get('active')) \
            and (active or ty.get('active')):
        if not active:
            head = '✅ 颱風警報解除'
        else:
            head = f'🚨 {title}' + ('發布' if not ty.get('active') else '（等級變更）')
        text = f'{head}\n{issued} 發布\n\n{content}\n\n{CWA}/V8/C/P/Typhoon/TY_WARN.html'
        png = None
        if active:
            try:
                png = pta_png()
            except Exception as e:
                print('路徑圖失敗：', e)
        if png and len(text) <= 1000:
            send_photo(png, text)
        else:
            send(text)
            if png:
                send_photo(png, '🗺 最新路徑潛勢', silent=True)
    ty.update(issued=issued, title=title, active=active)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--gap']:
        sys.exit(gap(os.environ.get('PREV_END', '')))
    sys.exit(main())
