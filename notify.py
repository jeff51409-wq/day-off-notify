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
CITIES = ['臺北市', '新北市']
NONE = '目前沒有停班停課訊息'   # 平常沒颱風時，表上找不到該縣市
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
TW = timezone(timedelta(hours=8))
FAIL_ALERT = 6   # 連續 6 次（約 30 分鐘）抓不到才警告


def fetch():
    # 新版 Python 的嚴格模式會擋 dgpa 的憑證（缺 Subject Key Identifier），只關掉這一項，其餘照常驗證
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return r.read().decode('utf-8', 'replace')


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


def send(text):
    token, chat = os.environ.get('TG_TOKEN'), os.environ.get('TG_CHAT')
    if not token or not chat:
        print('--- 沒設 TG_TOKEN／TG_CHAT，只印出 ---\n' + text)
        return
    data = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}).encode()
    urllib.request.urlopen(f'https://api.telegram.org/bot{token}/sendMessage', data, timeout=30).read()


def main():
    if os.environ.get('GITHUB_ACTIONS') and not os.environ.get('TG_TOKEN'):
        print('還沒設定 Telegram，先不記錄狀態')   # 不然設好之後收不到「已啟動」那則
        return
    old = {}
    if os.path.exists(STATE):
        with open(STATE, encoding='utf-8') as f:
            old = json.load(f)
    prev = old.get('status', {})
    fails = old.get('fails', 0)
    month = datetime.now(TW).strftime('%Y-%m')

    try:
        updated, status = parse(fetch())
    except Exception as e:   # 偶爾連不上就等下一輪；連續 FAIL_ALERT 次才警告，免得以為「沒消息」其實是壞掉
        print('抓取失敗：', e)
        old['fails'] = fails + 1
        if old['fails'] == FAIL_ALERT:
            send(f'⚠ 已經 30 分鐘抓不到人事行政總處網站，這段時間的停班停課消息可能收不到，請先直接看官網：\n{SITE}\n\n（錯誤：{e}）')
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(old, f, ensure_ascii=False, indent=1)
        return
    if fails >= FAIL_ALERT:
        send('✅ 已恢復連線，繼續幫你盯停班停課。')

    if not prev:
        lines = ['✅ 停班停課通知已啟動，之後狀態一有變動就會通知你', '']
        lines += [f'{c}：{status[c]}' for c in CITIES]
        send('\n'.join(lines + ['', f'官方更新時間 {updated}', SITE]))
    else:
        changed = [c for c in CITIES if status[c] != prev.get(c)]
        if changed:
            lines = [f'🌀 停班停課更新（官方 {updated}）', '']
            for c in CITIES:
                if c in changed:
                    lines.append(f'▶ {c}：{status[c]}')
                    lines.append(f'　（原本：{prev.get(c, "—")}）')
                else:
                    lines.append(f'{c}：{status[c]}')
            send('\n'.join(lines + ['', SITE]))

    # 狀態有變、或換月份才寫檔：每月至少 commit 一次，GitHub 才不會因為 60 天沒動靜停掉排程
    if status != prev or old.get('month') != month or fails:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump({'status': status, 'updated': updated, 'month': month, 'fails': 0}, f, ensure_ascii=False, indent=1)
    print(updated, status)


if __name__ == '__main__':
    sys.exit(main())
