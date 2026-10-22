# Lumi v2 可运行版本（Part B 加分项）

把原型做成真正能跑的产品：AI 对话由大模型通过 function calling 自主调用工具；免费额度在后端生效；埋点真实写入数据库；从广告 UTM、注册、生成、点短链、下单、归因到付费，整条链路可以亲手跑一遍。

## 启动（3 步）

需要 Python 3.9 及以上。

```bash
cp .env.example .env   # 1. 打开 .env，填入 LLM_API_KEY
pip install -r requirements.txt   # 2. 安装依赖（建议先 python3 -m venv .venv && source .venv/bin/activate）
python main.py   # 3. 启动后打开 http://127.0.0.1:8000
```

## 模型与 key

默认使用 DeepSeek（`deepseek-chat`），任何支持 function calling 的 OpenAI 兼容接口都可以，只需改 `.env` 中的三项：

| 平台 | 申请地址 | LLM_BASE_URL | LLM_MODEL |
|---|---|---|---|
| DeepSeek（默认） | https://platform.deepseek.com/api_keys | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi（月之暗面） | https://platform.moonshot.cn/console/api-keys | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | https://open.bigmodel.cn/usercenter/apikeys | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |

仓库中不包含任何真实 key，`.env` 已加入 `.gitignore`。

## 建议的体验路径（约 3 分钟）

1. 用带广告参数的地址打开落地页：`http://127.0.0.1:8000/?utm_source=tiktok&utm_campaign=spring_sale&ttclid=TT123`，页面会提示已捕获来源，直接注册。
2. **店铺连接**：点“连接 Shopify”（模拟授权），导入 4 个商品和近 30 天的 5 条历史帖子。
3. **内容创作**（Free 账户）：说“给防晒霜写一条 IG 帖子”。可以看到模型依次调用工具（查商品 → 分析评价 → 查效果 → 生成帖子），回复流式输出，帖子以卡片展示；Free 账户的帖子不带追踪。
4. **付费墙**：Free 每月 10 次。额度用完后后端直接拒绝生成并弹出付费墙；点「效果分析」页也会看到锁定提示。升级 Pro（模拟支付）。
5. **Pro 生成**：再生成一条，卡片上会自动挂好追踪短链和专属优惠码，点「发布到 Instagram」。
6. **归因闭环**：点卡片下的“打开短链”（或「效果分析」页表格里的短链），跳转到模拟的 Shopify 商品页，点“模拟下单”（可填优惠码）。回到「效果分析」点“刷新数据”，订单已算到这条帖子上。
7. **埋点**：左侧「埋点记录」打开 `/events`，可以看到每一步上报的事件、上报端和属性。

## 实现说明

### Agent 与工具

`agent.py` 把 5 个工具的 JSON Schema 交给模型（`tool_choice="auto"`），由模型决定调用哪个、调用几个；后端执行工具并把结果交回模型，循环直到模型作答。没有任何 if/else 意图路由。

| 工具 | 作用 |
|---|---|
| `get_products` | 按关键词查商品 |
| `analyze_reviews` | 统计评价里的卖点、顾虑和代表性原话 |
| `get_post_performance` | 查帖子的点击、订单、营业额和各风格转化率（Pro） |
| `generate_post` | 扣额度后单独调用一次模型写正文；Pro 且已接入店铺时自动挂短链和优惠码 |
| `create_tracked_link` | 给已有帖子补追踪链接（Pro） |

- 多轮对话：每个用户的对话（含工具调用记录）保存在内存中，模型能接着上一轮说话，例如“重新生成刚才那条”。
- 工具连续调用超过 6 轮会强制停止，直接作答。
- 界面上可以看到：流式输出、每一步工具调用及结果摘要、帖子卡片和效果表格、错误提示。

### 规则（与 docs/PRD.md 第 7 节一致）

- **额度**：在调用模型前用条件 UPDATE 原子占用，并发也不会超额；生成失败（超时、5xx、输出为空或格式错误）会自动重试 1 次，仍失败就退回额度；重新生成也计 1 次；按自然月（UTC）重置。效果数据和追踪功能在后端按套餐拦截，不依赖前端。
- **内容安全**：命中夸大功效的词（如“根治”“100%”）时改写一次，仍不通过则不扣额度。
- **短链**：`/l/<code>` 记录点击后 302 跳转到商品页，并带上 `utm_campaign=<post_id>`；过滤爬虫和链接预览抓取，同一 IP 10 秒内重复点击只计一次。
- **订单归因**：`POST /webhooks/shopify/orders`（模拟 Shopify webhook，按 `X-Shopify-Shop-Domain` 识别店铺）。优惠码优先，其次是 landing_site 中的 UTM（点击后 7 天内）；按 order_id 幂等；`/webhooks/shopify/refunds` 处理退款。
- **广告来源**：落地页由服务端把 UTM 和 ttclid 写入第一方 cookie（首触与末触），注册时写入用户表；`signup_completed` 和 `subscription_started` 都带上来源。

### 埋点

全部写入 SQLite 的 `events` 表。服务端事件包括 `signup_completed`、`store_connected`、`post_generated`、`tracked_post_published`、`link_clicked`、`order_attributed`、`subscription_started`；前端事件包括 `dashboard_viewed`、`paywall_viewed`，经 `/api/events` 上报，并且只接受白名单内的事件，服务端事件不能由前端伪造。

### 测试

`tests/test_app.py` 用脚本化的假模型代替真实大模型，其余全部走真实代码，覆盖：工具自主调用、Free 额度用尽、Pro 追踪、重新生成沿用短链、点击去重与爬虫过滤、UTM 和优惠码归因及冲突、幂等、退款、生成失败退额度、违禁词改写、6 轮上限、模型异常回滚。

```bash
pip install pytest && python -m pytest tests -q
```

## 演示环境的简化

- Shopify 授权、支付、订单 webhook 都是模拟的，接口形态按真实流程设计。
- 登录用一个 httponly cookie 标识用户，没有做密码体系。
- 对话历史保存在内存中，重启后清空；业务数据保存在 `lumi.db`，删除该文件即可重置。

## 目录

```
main.py        FastAPI 路由：落地页、账户、对话（SSE）、发布、效果、付费、埋点、短链、webhook
agent.py       Agent 循环、工具定义与实现
store.py       SQLite 表结构、额度、帖子、点击、订单归因、效果统计
data/          mock 的商品、评价、历史帖子
static/        前端（对话、效果、付费墙、模拟商品页、埋点记录页）
tests/         端到端测试
```
