#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ロト6・ロト7 当せん番号の収集プログラム(GitHub Actions 用)

取得元: みずほ銀行の公式サイト(過去の当せん番号案内)だけ。
  https://www.mizuhobank.co.jp/takarakuji/check/loto/backnumber/detail.html?fromto=開始回_終了回&type=loto6 / loto7
  ※ このページの表は「あとから JavaScript で読み込まれる」ため、
     ふつうに HTML を取るだけでは空っぽです。そこで、パソコンの代わりに
     見えない Chrome(Playwright)でページを開き、表が出てから読み取ります。

使い方:
    python collect_loto.py test         ← 古いページ・新しいページ・月ごとのページを1つずつ試して、結果を表示(保存しない)
    python collect_loto.py init-short   ← 直近2か月ぶんくらいを取って保存(動作確認用)
    python collect_loto.py init         ← 第1回から現在までの全回分を取って保存(途中で止まっても、続きから再開できる)
    python collect_loto.py daily        ← 保存済みの最新回より新しい回だけを足す(毎日の自動実行)
  オプション: --game loto6 / loto7 / both (初期値 both)
              --max-minutes 100  (この時間を過ぎたら、そこまでを保存して止まる)

保存先:
    loto6.csv   回号, 抽せん日, 本数字1〜6, ボーナス数字
    loto7.csv   回号, 抽せん日, 本数字1〜7, ボーナス数字1, ボーナス数字2

集めるのは「回号・抽せん日・本数字・ボーナス数字」だけです(賞金・口数などは集めません)。
秘密の情報(パスワード・鍵など)は一切使っていません。
"""

import os
import re
import sys
import csv
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
WAIT_SECONDS = float(os.environ.get("LOTO_WAIT", "3"))   # 1ページ取るごとに、これだけ間をあける(サイトに負担をかけないため)
PAGE_TIMEOUT_MS = int(os.environ.get("LOTO_TIMEOUT_MS", "30000"))   # 表が出るのを待つ最長時間(ミリ秒)
STAMP = "%Y-%m-%d %H:%M:%S"

SITE = "https://www.mizuhobank.co.jp"
BASE = SITE + "/takarakuji/check/loto/backnumber/detail.html"          # 20回ずつのページ(ロト7は第1回から。ロト6は第461回から)
OLD6 = [SITE + "/retail/takarakuji/loto/backnumber/loto6{start:04d}.html",       # ロト6の古い回(第1〜460回)
        SITE + "/takarakuji/check/loto/backnumber/loto6{start:04d}.html"]
MONTH = SITE + "/takarakuji/check/loto/{game}/index.html?year={year}&month={month}"   # 月ごとのページ
OLD6_LAST_KAI = 460

GAMES = {
    "loto6": {"n_main": 6, "n_bonus": 1, "max_num": 43, "csv": "loto6.csv",
              "first_month": (2000, 10),      # 第1回は2000年10月
              "short_start": 2101},          # 20ずつの区切りの先頭は 1, 21, 41, ... の形
    "loto7": {"n_main": 7, "n_bonus": 2, "max_num": 37, "csv": "loto7.csv",
              "first_month": (2013, 4),       # 第1回は2013年4月
              "short_start": 661},
}
PAGE_SIZE = 20
LABELS = ("回別", "抽せん日", "本数字", "ボーナス数字")


def now_str():
    return datetime.now(JST).strftime(STAMP)


def log(*a):
    print(f"[{now_str()}]", *a, flush=True)


# ======================================================================
# 読み取り(ページの表 → 当せん番号)  ※ 画面(ブラウザ)に依存しない部分
# ======================================================================
def norm(s):
    s = (s or "").replace("　", " ").replace("\xa0", " ")
    # 全角数字 → 半角
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789")).strip()


def find_label(text):
    t = norm(text).replace(" ", "")
    for lab in LABELS:
        if t.startswith(lab):
            return lab
    return None


def to_date(text):
    m = re.search(r"(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", norm(text))
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def to_kai(text):
    m = re.search(r"(\d+)", norm(text))
    return int(m.group(1)) if m else None


def make_draw(game, kai, date, main_txt, bonus_txt):
    """文字 → 当せん番号の1行。数や範囲がおかしければ None(理由も返す)"""
    g = GAMES[game]
    main = [int(x) for x in re.findall(r"\d{1,2}", norm(main_txt))]
    bonus = [int(x) for x in re.findall(r"\d{1,2}", norm(bonus_txt))]
    if kai is None or date is None:
        return None, "回号か抽せん日が読めません"
    if len(main) != g["n_main"]:
        return None, f"第{kai}回: 本数字が {len(main)} 個です(正しくは {g['n_main']} 個): {main}"
    if len(bonus) < g["n_bonus"]:
        return None, f"第{kai}回: ボーナス数字が {len(bonus)} 個です(正しくは {g['n_bonus']} 個): {bonus}"
    bonus = bonus[:g["n_bonus"]]
    allnum = main + bonus
    if any(n < 1 or n > g["max_num"] for n in allnum) or len(set(allnum)) != len(allnum):
        return None, f"第{kai}回: 数字の範囲か重複がおかしい: {main} / {bonus}"
    return {"kai": kai, "date": date, "main": main, "bonus": bonus}, None


def parse_tables(game, tables):
    """tables: ページ内の表 [[ [セルの文字,...], ...行 ], ...表 ] から当せん番号を取り出す"""
    draws, problems = [], []
    for t in tables:
        rows = {}
        for cells in t:
            if not cells:
                continue
            lab = find_label(cells[0])
            if lab:
                rows[lab] = [norm(c) for c in cells[1:]]
        if "回別" not in rows or "本数字" not in rows:
            continue
        kai_cells = [c for c in rows["回別"] if re.search(r"\d", c)]
        main_cells = rows["本数字"]
        # 1つの表に1回分(ふつう)。横に何回分も並んでいる場合は列ごとに分ける
        if len(kai_cells) > 1 and len(main_cells) == len(kai_cells):
            for j in range(len(kai_cells)):
                d, why = make_draw(game, to_kai(kai_cells[j]),
                                   to_date((rows.get("抽せん日") or [""] * len(kai_cells))[j]),
                                   main_cells[j], (rows.get("ボーナス数字") or [""] * len(kai_cells))[j])
                (draws if d else problems).append(d or why)
        else:
            d, why = make_draw(game, to_kai(" ".join(kai_cells)), to_date(" ".join(rows.get("抽せん日", []))),
                               " ".join(main_cells), " ".join(rows.get("ボーナス数字", [])))
            (draws if d else problems).append(d or why)
    return draws, problems


def parse_text(game, text):
    """予備: 画面の文字全体から「第N回 … 抽せん日 … 本数字 … ボーナス数字 …」を探す"""
    draws, problems = [], []
    text = norm(text)
    for chunk in re.split(r"(?=第\s*\d+\s*回)", text):
        m = re.match(r"第\s*(\d+)\s*回", chunk)
        if not m or "本数字" not in chunk or "ボーナス数字" not in chunk:
            continue
        i = chunk.index("本数字")
        j = chunk.index("ボーナス数字", i)
        main_txt = chunk[i + 3:j]
        m2 = re.match(r"[\s()\uff08\uff09]*((?:\d{1,2}[\s()\uff08\uff09]*)+)", chunk[j + 6:])
        d, why = make_draw(game, int(m.group(1)), to_date(chunk), main_txt, m2.group(1) if m2 else "")
        (draws if d else problems).append(d or why)
    return draws, problems


def parse_page(game, tables, text):
    draws, problems = parse_tables(game, tables)
    how = "表"
    if not draws:
        draws, problems2 = parse_text(game, text)
        problems += problems2
        how = "文字全体"
    uniq = {}
    for d in draws:
        uniq[d["kai"]] = d
    return [uniq[k] for k in sorted(uniq)], problems, how


# ======================================================================
# ブラウザ(Playwright)でページを開く
# ======================================================================
JS_TABLES = """() => Array.from(document.querySelectorAll('table')).map(t =>
    Array.from(t.querySelectorAll('tr')).map(tr =>
      Array.from(tr.querySelectorAll('th,td')).map(c => c.innerText.trim())))"""

# 表のどこかに「数字が6個以上ならんだセル(本数字)」が出るまで待つ(出ない = まだ読み込み中 or 空)
JS_READY = """() => Array.from(document.querySelectorAll('td,th')).some(c =>
    /^\\d{1,2}([\\s,、・]+\\d{1,2}){5,}$/.test(c.innerText.replace(/[\\u3000\\xa0]/g, ' ').trim()))"""


class Browser:
    def __init__(self, debug_dir=None, show_network=False):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch()
        ctx = self.browser.new_context(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        self.page = ctx.new_page()
        self.debug_dir = debug_dir
        self.show_network = show_network
        self.net = []
        self.page.on("response", self._on_response)
        self._last = 0.0
        self.dumped = 0

    def _on_response(self, res):
        try:
            ct = (res.headers or {}).get("content-type", "")
            if any(k in ct for k in ("json", "text/plain", "csv", "javascript")) or res.url.endswith((".json", ".txt", ".csv")):
                self.net.append((res.status, ct.split(";")[0], res.url))
        except Exception:
            pass

    def close(self):
        try:
            self.browser.close()
            self._pw.stop()
        except Exception:
            pass

    # ---- ページの住所 ----
    def page_urls(self, game, start):
        end = start + PAGE_SIZE - 1
        tmpl = os.environ.get("LOTO_URL_TEMPLATE")           # テスト用(ふだんは使わない)
        if tmpl and not (game == "loto6" and start <= OLD6_LAST_KAI):
            return [tmpl.format(game=game, start=start, end=end)]
        if game == "loto6" and start <= OLD6_LAST_KAI:
            old = os.environ.get("LOTO_OLD_TEMPLATE")
            if old:
                return [old.format(game=game, start=start, end=end)]
            return [u.format(start=start) for u in OLD6]
        return [f"{BASE}?fromto={start}_{end}&type={game}"]

    def month_url(self, game, year, month):
        tmpl = os.environ.get("LOTO_MONTH_TEMPLATE") or MONTH
        return tmpl.format(game=game, year=year, month=month)

    # ---- 1ページ読む ----
    def _open(self, game, url, tag):
        gap = time.time() - self._last
        if gap < WAIT_SECONDS:
            time.sleep(WAIT_SECONDS - gap)
        log(f"ページを開きます: {url}")
        self.net.clear()
        ready = True
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_function(JS_READY, timeout=PAGE_TIMEOUT_MS)
            self.page.wait_for_timeout(1500)
        except Exception as e:
            ready = False
            log(f"  表が出ませんでした({type(e).__name__})")
        self._last = time.time()
        try:
            tables = self.page.evaluate(JS_TABLES)
            text = self.page.evaluate("() => document.body.innerText")
        except Exception as e:
            log(f"  ページの中身を読めません: {e}")
            tables, text = [], ""
        draws, problems, how = parse_page(game, tables, text)
        log(f"  読み取り: {len(draws)} 回分({how}から)" + (f" / 問題 {len(problems)} 件" if problems else ""))
        for p in problems[:5]:
            log("   ・", p)
        if self.show_network:
            log(f"  通信の記録(JSON/テキストのみ) {len(self.net)} 件:")
            for st, ct, u in self.net[:30]:
                log(f"   {st} {ct} {u}")
        if self.debug_dir and (self.show_network or ((not draws or not ready) and self.dumped < 12)):
            self._dump(tag, tables, text)
        return draws, problems

    def fetch(self, game, start):
        """20回ぶんのページを開いて (当せん番号のリスト, 問題のリスト) を返す(住所の候補を順に試す)"""
        draws, problems = [], []
        for url in self.page_urls(game, start):
            draws, problems = self._open(game, url, f"{game}_{start}")
            if draws:
                break
        return draws, problems

    def fetch_month(self, game, year, month):
        return self._open(game, self.month_url(game, year, month), f"{game}_{year}{month:02d}")

    def _dump(self, tag, tables, text):
        os.makedirs(self.debug_dir, exist_ok=True)
        base = os.path.join(self.debug_dir, tag)
        try:
            with open(base + "_text.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open(base + "_tables.json", "w", encoding="utf-8") as f:
                json.dump(tables, f, ensure_ascii=False, indent=1)
            with open(base + "_page.html", "w", encoding="utf-8") as f:
                f.write(self.page.content())
            self.dumped += 1
            log(f"  確認用に保存: {base}_*.txt/.json/.html")
        except Exception as e:
            log("  確認用の保存に失敗:", e)


# ======================================================================
# CSV
# ======================================================================
def csv_header(game):
    g = GAMES[game]
    cols = ["回号", "抽せん日"] + [f"本数字{i}" for i in range(1, g["n_main"] + 1)]
    if g["n_bonus"] == 1:
        cols.append("ボーナス数字")
    else:
        cols += [f"ボーナス数字{i}" for i in range(1, g["n_bonus"] + 1)]
    return cols


def draw_to_row(d):
    return [d["kai"], d["date"]] + d["main"] + d["bonus"]


def load_csv(game, outdir):
    path = os.path.join(outdir, GAMES[game]["csv"])
    out = {}
    if not os.path.exists(path):
        return out
    n_main, n_bonus = GAMES[game]["n_main"], GAMES[game]["n_bonus"]
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for row in rd:
            if len(row) < 2 + n_main + n_bonus or not row[0].strip().isdigit():
                continue
            nums = [int(x) for x in row[2:2 + n_main + n_bonus]]
            out[int(row[0])] = {"kai": int(row[0]), "date": row[1], "main": nums[:n_main], "bonus": nums[n_main:]}
    return out


def save_csv(game, outdir, rows):
    path = os.path.join(outdir, GAMES[game]["csv"])
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(csv_header(game))
        for k in sorted(rows):
            w.writerow(draw_to_row(rows[k]))
    os.replace(tmp, path)
    return path


def check_gaps(rows):
    ks = sorted(rows)
    if not ks:
        return []
    return [k for k in range(ks[0], ks[-1] + 1) if k not in rows]


# ======================================================================
# 取り方(ページをたどる)
# ======================================================================
def page_start(kai):
    return ((kai - 1) // PAGE_SIZE) * PAGE_SIZE + 1


def page_complete(rows, start):
    return all(k in rows for k in range(start, start + PAGE_SIZE))


class Clock:
    def __init__(self, minutes):
        self.end = time.time() + minutes * 60

    def up(self):
        return time.time() >= self.end


def months_between(first, last):
    y, m = first
    while (y, m) <= last:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def run_test(br, game, outdir):
    """古いページ・新しいページ・月ごとのページを1つずつ試して、結果を表にして見せる"""
    g = GAMES[game]
    results = []
    checks = [("20回ページ(いちばん古い 第1〜20回)", lambda: br.fetch(game, 1)),
              ("20回ページ(新しめ)", lambda: br.fetch(game, g["short_start"])),
              ("月ごとのページ(今月)", lambda: br.fetch_month(game, datetime.now(JST).year, datetime.now(JST).month)),
              ("月ごとのページ(第1回の月)", lambda: br.fetch_month(game, *g["first_month"]))]
    for name, fn in checks:
        draws, _p = fn()
        results.append((name, len(draws), draws))
        for d in draws[:2]:
            log("   例:", d)
    log(f"--- {game} テスト結果 ---")
    for name, n, draws in results:
        rng = f"(第{draws[0]['kai']}〜{draws[-1]['kai']}回 / {draws[0]['date']}〜{draws[-1]['date']})" if draws else ""
        log(f"  {'OK' if n else 'NG'}  {name}: {n} 回分 {rng}")
    return any(n for _, n, _ in results)


def run_daily(br, game, outdir, clock):
    rows = load_csv(game, outdir)
    if not rows:
        log("  保存済みのデータがありません。先に init か init-short を実行してください。")
        return False
    latest = max(rows)
    start = page_start(latest)
    got, empties = {}, 0
    while not clock.up():
        draws, _p = br.fetch(game, start)
        for d in draws:
            got[d["kai"]] = d
        if len(draws) >= PAGE_SIZE:
            start += PAGE_SIZE
            continue
        break
    if not got:
        log("  ページから1回分も読み取れませんでした(サイトの作りが変わった可能性があります)")
        return False
    new = {k: v for k, v in got.items() if k not in rows}
    log(f"  最新の保存済み: 第{latest}回 → 新しく {len(new)} 回分" + (f"(第{min(new)}〜{max(new)}回)" if new else ""))
    rows.update(new)
    save_csv(game, outdir, rows)
    return True


def run_init(br, game, outdir, mode, clock):
    g = GAMES[game]
    rows = load_csv(game, outdir)
    start = g["short_start"] if mode == "init-short" else 1
    log(f"  保存済み {len(rows)} 回分。第{start}回のページから始めます" + ("(続きから再開)" if rows and mode == "init" else ""))
    short_run, stopped_by_time = 0, False
    while True:
        if clock.up():
            stopped_by_time = True
            log("  時間切れ: ここまでを保存して止まります。もう一度同じモードで実行すれば、続きから再開します。")
            break
        if page_complete(rows, start) and start + PAGE_SIZE - 1 < max(rows):
            start += PAGE_SIZE                       # すでに取れているページは飛ばす
            continue
        draws, _p = br.fetch(game, start)
        for d in draws:
            rows[d["kai"]] = d
        if draws:
            save_csv(game, outdir, rows)             # 1ページごとに保存(途中で止まっても残る)
        in_middle = bool(rows) and start + PAGE_SIZE - 1 < max(rows)      # 後ろの回がすでにある = 最新の端ではない
        short_run = 0 if (len(draws) >= PAGE_SIZE or in_middle) else short_run + 1
        if short_run >= 3:                           # 満ちたページが3回続けて来なければ、いちばん新しい所まで来たと見なす
            break
        start += PAGE_SIZE
    missing = check_gaps(rows)
    # 月ごとのページで、抜けた回を補う(20回ページで取れなかった所だけ)
    if missing and mode == "init" and not stopped_by_time:
        lo, hi = min(missing), max(missing)
        log(f"  抜けている回 {len(missing)} 件(第{lo}〜{hi}回)を、月ごとのページで補います")
        dates = sorted(d["date"] for d in rows.values())
        first = g["first_month"]
        last = (int(dates[-1][:4]), int(dates[-1][5:7])) if dates else first
        # 抜けた回の前後の日付から、探す月の範囲をしぼる
        near_lo = next((rows[k]["date"] for k in range(lo - 1, 0, -1) if k in rows), None)
        near_hi = next((rows[k]["date"] for k in range(hi + 1, max(rows) + 1) if k in rows), None)
        a = (int(near_lo[:4]), int(near_lo[5:7])) if near_lo else first
        b = (int(near_hi[:4]), int(near_hi[5:7])) if near_hi else last
        for (y, m) in months_between(a, b):
            if clock.up():
                log("  時間切れ(月ごとの補い)")
                break
            draws, _p = br.fetch_month(game, y, m)
            added = 0
            for d in draws:
                if d["kai"] not in rows:
                    rows[d["kai"]] = d
                    added += 1
            if added:
                save_csv(game, outdir, rows)
            if not check_gaps(rows):
                break
        missing = check_gaps(rows)
    if not rows:
        log("  1回分も取れませんでした")
        return False
    ks = sorted(rows)
    path = save_csv(game, outdir, rows)
    log(f"  保存: {path}({len(rows)} 回分 / 第{ks[0]}〜{ks[-1]}回 / {rows[ks[0]]['date']}〜{rows[ks[-1]]['date']})")
    if missing:
        log(f"  ※ まだ抜けている回: {len(missing)} 件 {missing[:20]}{' …' if len(missing) > 20 else ''}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["test", "init-short", "init", "daily"])
    ap.add_argument("--game", choices=["loto6", "loto7", "both"], default="both")
    ap.add_argument("--out", default=".")
    ap.add_argument("--debug-dir", default="debug")
    ap.add_argument("--max-minutes", type=float, default=100)
    args = ap.parse_args()
    games = ["loto6", "loto7"] if args.game == "both" else [args.game]
    os.makedirs(args.out, exist_ok=True)

    clock = Clock(args.max_minutes)
    br = Browser(debug_dir=args.debug_dir, show_network=(args.mode == "test"))
    ok_all = True
    try:
        for game in games:
            log(f"=== {game}: モード {args.mode} ===")
            if args.mode == "test":
                ok = run_test(br, game, args.out)
            elif args.mode == "daily":
                ok = run_daily(br, game, args.out, clock)
            else:
                ok = run_init(br, game, args.out, args.mode, clock)
            ok_all = ok_all and ok
    finally:
        br.close()
    if not ok_all:
        log("うまく取れなかったものがあります。debug フォルダの中身を確認してください。")
        sys.exit(1)
    log("完了")


if __name__ == "__main__":
    main()
