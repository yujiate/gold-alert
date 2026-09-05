# 上海金 T+D 价格异动监控

每 5 分钟检查一次上海黄金交易所「黄金延期」（Au T+D，人民币元/克），
出现 **1% 以上急涨急跌** 时，通过企业微信机器人推送告警。

- 数据源：新浪财经公开接口，无需鉴权
- 运行环境：GitHub Actions，不占用本地机器，关机也能跑
- 依赖：仅 Python 标准库，无需 `pip install`

---

## 快速开始

### 1. 创建仓库并推送

在 GitHub 网页新建仓库（例如 `gold-alert`，建议选 Private），然后把本目录推上去：

```bash
cd gold-alert
git init
git add .
git commit -m "feat: 上海金 T+D 异动监控"
git branch -M main
git remote add origin git@github.com:<你的用户名>/gold-alert.git
git push -u origin main
```

### 2. 获取企业微信机器人 Webhook

1. 在企业微信中建一个群（可以只有你自己）
2. 群设置 → 群机器人 → 添加机器人
3. 复制 Webhook 地址，形如：

   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

### 3. 配置为仓库 Secret

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Name | Value |
| --- | --- |
| `WECOM_WEBHOOK` | 上一步复制的完整 Webhook 地址 |

密钥不会出现在运行日志中，脚本通过环境变量读取。

### 4. 验证

`Actions` → 选「上海金 T+D 异动监控」→ `Run workflow` → 手动触发一次。

首次运行只会记录基准价、不会告警，之后每 5 分钟自动检查。

想立刻确认机器人是否连通，可在本机执行：

```bash
WECOM_WEBHOOK=<你的地址> python3 monitor.py --test-webhook
```

看到企业微信群收到「金价监控已连通」即配置成功。

---

## 告警规则

三个条件同时满足才推送：

1. 距上次采样的**真实间隔**在 2~30 分钟内
2. 区间涨跌幅绝对值 **≥ 1%**
3. 该方向不在 30 分钟冷却期内

推送内容示例：

```
⚠️ 上海金 T+D 价格异动
快速下跌 ▼ -1.47%（约 5 分钟内）

现价：956.71 元/克
起价：971.00 元/克

今日累计：-0.86%（昨收 965.00）
日内区间：940.00 ~ 967.00
报价时间：2026-09-05 00:29:46
```

---

## 调整参数

全部集中在 `monitor.py` 顶部常量区，改完推送即生效，无需改动 Actions 配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `THRESHOLD_PCT` | `1.0` | 触发阈值（%），取绝对值 |
| `MIN_ELAPSED` | `120` | 最短有效间隔（秒），短于此视为重复运行 |
| `MAX_ELAPSED` | `1800` | 最长有效间隔（秒），跨休市或调度漂移则不判定 |
| `COOLDOWN` | `1800` | 同方向冷却期（秒），防止连续刷屏 |
| `STALE_QUOTE` | `900` | 行情陈旧阈值（秒），超过则判定休市，只记录不告警 |

改调度频率请编辑 `.github/workflows/monitor.yml` 里的 `cron`（注意是 **UTC 时间**）。

---

## 已知限制

- **调度不绝对准时**：GitHub 的 `schedule` 在负载高时可能延迟 5~15 分钟，
  极端情况会跳过。脚本按真实间隔判定并在消息中写明实际跨度，
  所以延迟只会造成漏报，不会误报。
- **仓库 60 天无活动会停用调度**：GitHub 会对长期无提交的仓库自动禁用
  scheduled workflow。本仓库每次运行都会提交 `state.json`，
  正常运行期间不会触发；若长期闲置后需要恢复，手动进 Actions 页面跑一次即可。
- **提交记录较多**：`state.json` 每次运行都会提交（约 288 次/天），
  这是跨运行保存状态所必需的，无法避免。
- **上海金无分钟级历史**：已验证东财无该标的代码、新浪分钟线接口已下线，
  因此只能用实时快照做跨运行对比，无法回溯盘中轨迹。

---

## 本地调试

```bash
python3 monitor.py              # 正常运行（读状态 → 判定 → 推送）
python3 monitor.py --dry-run    # 只打印不推送
python3 monitor.py --test-webhook   # 发送测试消息
```
