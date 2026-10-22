"""
盯人事行政總處的停班停課表，指定縣市的狀態一變就推到 Telegram。
GitHub Actions 每 5 分鐘跑一次；上次的狀態存在 state.json（有變才 commit）。

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


def verdict(s):
    """把官方原文翻成一句白話：放假／照常上班／還沒宣布。只看全市的那一行（以今天、明天開頭），區域性的另外提示。"""
    if '尚未宣布' in s:
        return '還沒宣布'
    if s == NONE:
        return '沒有停班停課'
    whole = [l for l in s.split('\n') if re.match(r'(今天|明天|\d)', l)]
    if not whole:
        return '只有部分區域停班停課，看下面原文'
    l = whole[0]
    for key, word in (('停止上班', '放假'), ('照常上班', '照常上班')):
        if key in l:
            when = l.split(key)[0].rstrip('、，, ')
            when = when[:-1] + '起' if when.endswith('起') else when
            out = when + word
            if word == '照常上班' and '停止上課' in l:
                out += '（但學校停課）'
            if len(whole) < len(s.split('\n')):
                out += '；部分區域不同，看下面原文'
            return out
    return '看下面原文'


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
        changed = [c for c in CITIES if status[c] != prev.get(c)]
        if changed:
            lines = [f'🌀 停班停課更新（官方 {updated}）', '']
            lines += [f'👉 {c}：{verdict(status[c])}' for c in CITIES]
            lines += ['', '── 官方原文 ──']
            for c in CITIES:
                if c in changed:
                    lines.append(f'▶ {c}：{status[c]}')
                    lines.append(f'　（原本：{prev.get(c, "—")}）')
                else:
                    lines.append(f'{c}：{status[c]}')
            send('\n'.join(lines + ['', SITE]))
    if status != prev:
        state['status'], state['updated'] = status, updated
    print(updated, status)


def js_str(src, key):
    """從氣象署的 JS 資料檔裡取 'key':'值'（單引號字串，處理 \\n、\\' 跳脫）。"""
    m = re.search(r"'" + key + r"'\s*:\s*'((?:\\.|[^'\\])*)'", src)
    return m.group(1).encode().decode('unicode_escape').encode('latin-1').decode('utf-8') if m else ''


def typhoon(state):
    """有颱風警報時：每一報傳警報內容（等級變了才響），路徑預測更新時傳潛勢圖。"""
    ty = state.setdefault('ty', {})
    w = fetch(CWA + '/Data/js/warn/Warning_Content.js?T=' + str(int(datetime.now().timestamp())))
    i = w.find("'TY_WARN':{")   # 前面的 WarnAll 清單也有 'TY_WARN'，要找物件本身
    block = w[i:w.find("'E'", i)] if i >= 0 else ''
    title, issued, validto = js_str(block, 'title'), js_str(block, 'issued'), js_str(block, 'validto')
    content = re.sub(r'^發布時間：.*?\n+', '', js_str(block, 'content')).strip()
    now = datetime.now(TW).strftime('%Y/%m/%d %H:%M')
    active = bool(issued) and validto > now and '解除' not in title + content[:30]

    if issued and issued != ty.get('issued') and (active or ty.get('active')):
        level_changed = title != ty.get('title') or active != ty.get('active')
        if not active:
            head = '✅ 颱風警報解除'
        elif level_changed:
            head = f'🚨 {title}' + ('發布' if not ty.get('active') else '（等級變更）')
        else:
            head = f'🌀 {title}（最新一報）'
        send(f'{head}\n{issued} 發布\n\n{content}\n\n{CWA}/V8/C/P/Typhoon/TY_WARN.html', silent=not level_changed)
    ty.update(issued=issued, title=title, active=active)

    if not active:
        return
    d = fetch(CWA + '/Data/js/typhoon/TY_NEWS-Data.js?T=' + str(int(datetime.now().timestamp())))
    m = re.search(r"TY_DataTime\s*=\s*'(\d+)'", d)
    if not m or m.group(1) == ty.get('pta'):
        return
    t = m.group(1)
    imgs = json.loads(fetch(f'{CWA}/Data/typhoon/TY_NEWS/PTA_IMGS_{t}_zhtw.json'))
    png = fetch(f'{CWA}/Data/typhoon/TY_NEWS/{imgs["WHOLE"][-1]}', raw=True)
    k = d.find("TY_LIST_1['C']")
    desc = clean(re.sub(r"'\s*\+\s*'", '', d[k:d.find(';', k)]).replace('<br>', ' ').replace('</h4>', '\n').replace('</p>', '\n'))
    desc = re.sub(r"^TY_LIST_1\['C'\]\s*=\s*'?", '', desc).rstrip("'")
    send_photo(png, f'🗺 颱風路徑潛勢（{t[4:6]}/{t[6:8]} {t[8:10]}:{t[10:]} UTC）\n\n{desc}'[:1000], silent=bool(ty.get('pta')))
    ty['pta'] = t


if __name__ == '__main__':
    sys.exit(main())
