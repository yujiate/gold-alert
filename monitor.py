#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海金 T+D（gds_AUTD）价格异动监控 —— GitHub Actions 版
============================================================================
数据源
    新浪财经实时接口 hq.sinajs.cn，无需鉴权，任意网络环境可调用。
    标的 gds_AUTD = 上海黄金交易所「黄金延期」，人民币元/克，
    是国内金条、首饰定价的主要参照。

为什么不用 westock
    westock CLI 依赖本机 127.0.0.1 代理与会话级 token，云端无法使用。
    （已验证：东财无上海金 secid，新浪分钟线接口已下线，故只能用实时快照。）

跨运行状态持久化
    GitHub Actions 每次运行都是全新容器，本地文件不留存。因此把
    「上一次采样的价格与时间」写入 state.json 并提交回仓库，下次运行读取，
    两次采样之差即为 N 分钟涨跌幅。

    ⚠️ 关键设计：GitHub 的 schedule cron 并不准时，常有 5~15 分钟漂移。
    因此判定时会校验「真实间隔」是否落在有效窗口内，并在消息里写明实际跨度，
    绝不把一次 40 分钟的漂移当成 5 分钟异动报出去。

告警规则（三个条件同时满足）
    1. 距上次采样 elapsed ∈ [MIN_ELAPSED, MAX_ELAPSED] 秒
    2. |区间涨跌幅| >= THRESHOLD_PCT
    3. 该方向不在冷却期内

环境变量（至少配一个；都配则两渠道都发）
    PUSHPLUS_TOKEN  推送加 PushPlus token（默认渠道，消息直达个人微信）
    WECOM_WEBHOOK   企业微信机器人 Webhook（可选，另发一份到企业微信群）

用法
    python3 monitor.py                 正常运行：读状态 → 判定 → 推送
    python3 monitor.py --dry-run       只打印不推送，用于验证
    python3 monitor.py --test-webhook  向所有已配置渠道发送测试消息
============================================================================
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
CST = ZoneInfo("Asia/Shanghai")
SINA_URL = "https://hq.sinajs.cn/list=gds_AUTD"
SYMBOL = "gds_AUTD"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

THRESHOLD_PCT = 1.0      # 触发阈值（%），取绝对值
MIN_ELAPSED = 120        # 最短有效间隔（秒）：短于此说明调度异常，不判定
MAX_ELAPSED = 1800       # 最长有效间隔（秒）：跨休市或调度漂移过大则不判定
COOLDOWN = 1800          # 同方向冷却期（秒）
STALE_QUOTE = 900        # 行情陈旧阈值（秒）：超过视为休市，只记录不判定
MAX_RETRY = 3            # 行情获取重试次数


# --------------------------------------------------------------------------
# 1. 行情获取与解析
# --------------------------------------------------------------------------
def fetch_quote():
    """获取实时行情，失败自动重试。"""
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            req = urllib.request.Request(SINA_URL, headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": USER_AGENT,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("gbk", errors="replace")
            return parse_quote(text)
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            if attempt < MAX_RETRY - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"行情获取失败（已重试 {MAX_RETRY} 次）：{last_err}")


def parse_quote(text):
    """解析新浪 gds_ 系列行情。

    字段顺序（实测）：
        0 现价 / 2 买价 / 3 卖价 / 4 最高 / 5 最低 / 6 时间
        7 昨收 / 8 今开 / 9 成交量 / 12 日期 / 13 名称
    """
    match = re.search(r'var hq_str_%s="([^"]*)"' % SYMBOL, text)
    if not match:
        raise RuntimeError("响应中未找到行情数据（可能接口变更或被限流）")
    fields = match.group(1).split(",")
    if len(fields) < 14:
        raise RuntimeError(f"行情字段数异常，期望 >=14 实际 {len(fields)}")

    def num(value):
        try:
            return float(value)
        except ValueError:
            return None

    return {
        "name": fields[13],
        "price": num(fields[0]),
        "bid": num(fields[2]),
        "ask": num(fields[3]),
        "high": num(fields[4]),
        "low": num(fields[5]),
        "prev_close": num(fields[7]),
        "open": num(fields[8]),
        "volume": num(fields[9]),
        "quote_date": fields[12],
        "quote_time": fields[6],
    }


def quote_epoch(quote):
    """把行情自带的日期+时间转成 epoch（按北京时间）。"""
    try:
        naive = datetime.strptime(f"{quote['quote_date']} {quote['quote_time']}",
                                  "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=CST).timestamp()
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------
# 2. 状态持久化
# --------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        state = {}
    state.setdefault("last", None)
    state.setdefault("last_trigger", {"up": 0.0, "down": 0.0})
    state.setdefault("runs", 0)
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 3. 判定
# --------------------------------------------------------------------------
def evaluate(quote, state, now, threshold):
    """返回 (verdict, detail)。verdict: alert / skip / cooldown"""
    prev = state.get("last")
    if not prev or not prev.get("price"):
        return "skip", "首次运行，已记录基准价"

    elapsed = now - prev["epoch"]
    if elapsed < MIN_ELAPSED:
        return "skip", f"间隔过短（{elapsed:.0f}s < {MIN_ELAPSED}s），疑似重复运行"
    if elapsed > MAX_ELAPSED:
        return "skip", f"间隔过长（{elapsed / 60:.0f}分钟），跨越休市或调度漂移，不判定"

    prev_price = prev["price"]
    price = quote["price"]
    if not prev_price or not price:
        return "skip", "价格缺失"

    pct = (price - prev_price) / prev_price * 100.0
    if abs(pct) < threshold:
        return "skip", f"波动 {pct:+.2f}% 未达阈值 ±{threshold}%"

    direction = "up" if pct > 0 else "down"
    last_fired = state["last_trigger"].get(direction, 0.0)
    if now - last_fired < COOLDOWN:
        remain = (COOLDOWN - (now - last_fired)) / 60
        return "cooldown", f"{direction} 方向冷却中，剩余 {remain:.0f} 分钟"

    state["last_trigger"][direction] = now
    return "alert", {
        "direction": direction,
        "pct": pct,
        "elapsed": elapsed,
        "prev_price": prev_price,
    }


# --------------------------------------------------------------------------
# 4. 告警推送（PushPlus 个人微信 / 企业微信，可并存）
# --------------------------------------------------------------------------
def build_message(quote, detail):
    pct = detail["pct"]
    direction_cn = "快速上涨" if pct > 0 else "快速下跌"
    arrow = "▲" if pct > 0 else "▼"
    minutes = detail["elapsed"] / 60
    prev_close = quote.get("prev_close") or 0
    day_pct = (quote["price"] - prev_close) / prev_close * 100 if prev_close else 0.0

    return "\n".join([
        "## ⚠️ 上海金 T+D 价格异动",
        f"**{direction_cn} {arrow} {pct:+.2f}%**（约 {minutes:.0f} 分钟内）",
        "",
        f"> 现价：**{quote['price']:.2f}** 元/克",
        f"> 起价：{detail['prev_price']:.2f} 元/克",
        "",
        f"今日累计：{day_pct:+.2f}%（昨收 {prev_close:.2f}）",
        f"日内区间：{quote['low']:.2f} ~ {quote['high']:.2f}",
        f"报价时间：{quote['quote_date']} {quote['quote_time']}",
    ])


def send_wecom(webhook, content):
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    result = json.loads(body)
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误：{body}")
    return result


def _message_title(content):
    """从 markdown 正文提取标题（取第一个 # 开头的行）。"""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return "上海金 T+D 价格异动"


PUSHPLUS_API = "https://www.pushplus.plus/send"


def send_pushplus(token, title, content):
    payload = {"token": token, "title": title,
               "content": content, "template": "markdown"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    result = json.loads(body)
    if result.get("code") != 200:
        raise RuntimeError(f"PushPlus 返回错误：{body}")
    return result


def send_alert(content):
    """按配置把告警推送到所有渠道。

    返回 (成功渠道列表, 失败原因列表)。任一渠道失败都只记录、不抛错，
    保证状态照常回写（推送失败不应影响下次采样基准）。
    """
    sent, errors = [], []
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    if token:
        try:
            send_pushplus(token, _message_title(content), content)
            sent.append("PushPlus(微信)")
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"PushPlus：{exc}")
    if webhook:
        try:
            send_wecom(webhook, content)
            sent.append("企业微信")
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"企业微信：{exc}")

    if not token and not webhook:
        raise RuntimeError("未配置任何推送渠道（请设置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK）")
    return sent, errors


def test_webhook():
    """向所有已配置的渠道发送测试消息。"""
    now_str = f"{datetime.now(CST):%Y-%m-%d %H:%M:%S}"
    content = "\n".join([
        "## ✅ 金价监控已连通",
        "这是一条测试消息，收到说明推送渠道配置正确。",
        "",
        f"> 发送时间：{now_str}",
    ])
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not webhook and not token:
        print("[错误] 未配置推送渠道：请设置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK。",
              file=sys.stderr)
        return 1

    ok = True
    if token:
        try:
            send_pushplus(token, _message_title(content), content)
            print("[成功] PushPlus(微信) 测试消息已发送，请查看个人微信。")
        except Exception as exc:                      # noqa: BLE001
            ok = False
            print(f"[失败] PushPlus：{exc}", file=sys.stderr)
    if webhook:
        try:
            send_wecom(webhook, content)
            print("[成功] 企业微信测试消息已发送，请检查企业微信群。")
        except Exception as exc:                      # noqa: BLE001
            ok = False
            print(f"[失败] 企业微信：{exc}", file=sys.stderr)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 5. 主流程
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="上海金 T+D 价格异动监控")
    parser.add_argument("--state", default="state.json", help="状态文件路径")
    parser.add_argument("--threshold", type=float, default=THRESHOLD_PCT,
                        help=f"触发阈值百分比（默认 {THRESHOLD_PCT}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    parser.add_argument("--test-webhook", action="store_true",
                        help="向所有已配置的推送渠道发送测试消息后退出")
    args = parser.parse_args()

    if args.test_webhook:
        return test_webhook()

    now = datetime.now(CST)
    now_epoch = now.timestamp()

    quote = fetch_quote()
    if not quote["price"]:
        raise RuntimeError("行情返回价格为空")

    quote_ts = quote_epoch(quote)
    staleness = (now_epoch - quote_ts) if quote_ts else 0
    state = load_state(args.state)
    state["runs"] = state.get("runs", 0) + 1

    print(f"[行情] {quote['name']}  现价 {quote['price']:.2f} 元/克"
          f"  昨收 {quote['prev_close']:.2f}"
          f"  区间 {quote['low']:.2f}~{quote['high']:.2f}")
    print(f"[行情] 报价时间 {quote['quote_date']} {quote['quote_time']}"
          f"（距今 {staleness:.0f} 秒）")

    # 休市判断：行情长时间不刷新则只记录、不判定，避免用陈旧价格误报
    if quote_ts and staleness > STALE_QUOTE:
        print(f"[休市] 行情已 {staleness / 60:.0f} 分钟未更新，仅记录不判定。")
        state["last"] = {"price": quote["price"], "epoch": now_epoch,
                         "quote_time": f"{quote['quote_date']} {quote['quote_time']}"}
        save_state(args.state, state)
        return 0

    verdict, detail = evaluate(quote, state, now_epoch, args.threshold)

    if verdict == "alert":
        message = build_message(quote, detail)
        print(f"[告警] {detail['direction']} {detail['pct']:+.2f}%"
              f"（{detail['elapsed'] / 60:.0f} 分钟）")
        print("-" * 50)
        print(message)
        print("-" * 50)
        if args.dry_run:
            print("[跳过] --dry-run 模式，未推送。")
        else:
            try:
                sent, errors = send_alert(message)
                print(f"[推送] 已发送：{', '.join(sent) if sent else '无'}")
                for err in errors:
                    print(f"[推送失败] {err}", file=sys.stderr)
            except RuntimeError as exc:
                print(f"[警告] {exc}", file=sys.stderr)
    else:
        print(f"[正常] {detail}")

    # 无论是否告警都记录本次采样，作为下次比较的基准
    state["last"] = {"price": quote["price"], "epoch": now_epoch,
                     "quote_time": f"{quote['quote_date']} {quote['quote_time']}"}
    save_state(args.state, state)
    print(f"[状态] 已写入 {args.state}（累计运行 {state['runs']} 次）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                          # noqa: BLE001
        print(f"[致命错误] {exc}", file=sys.stderr)
        sys.exit(1)
