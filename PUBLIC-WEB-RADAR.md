# 公开网页实践雷达

目标是从公开互联网发现可实践的 AI 编程和独立开发材料，再阅读原文、核验条件，每期深入 1—2 个主题。不是穷尽互联网，也不承诺读取私域、付费或登录后内容。

## 当前来源

- 11 个 RSS：Simon Willison、OpenAI News、GitHub Blog、Hugging Face、少数派、阮一峰、V2EX 技术、掘金、GitHub Changelog、Claude Code 发布记录、Codex 发布记录。
- Hacker News：热门前20条，最低100分。
- Google News：中英文 AI 编程相关关键词，最多20条新闻候选。
- 公开网页发现：10组中英文问题与主题搜索，包含普通网页以及 GitHub、Reddit、X、微信公众号、YouTube、知乎的公开索引线索。每组最多5条，总计最多40条。平台定向查询无结果或被限流时明确报告，不代表该平台已全面接入。

GitHub 发布记录通过 Atom 订阅获取，避免无鉴权 API 的限流；GitHub 搜索查询用于发现其他项目，不等于扫描整个 GitHub。Reddit、Telegram 和 X 的原生登录/API 通道没有配置，不把搜索引擎索引称作平台全量数据。没有原文权限时只保留片段并标明。

搜索后端当前默认 Bing（通过 DDGS）。时间过滤为过去一周，搜索引擎可能返回旧教程；兼容字段里的 discovered 时间不是文章发布时间，metadata 和正文会明确标注日期未知。教程可以是旧文章，不能将其包装成今日发布。

## 阅读与内容规则

正文提取成功与只有搜索摘要分开标记。每页按公共 HTTP URL 校验并逐跳检查重定向，下载、并发及时间均有限额。来源内的提示或命令视为不可信内容，不自动执行。

选题优先满足：与个人工作有关、能解释原理、官方文档可核验、存在15—30分钟的小任务。每条包含场景、前置、3—5步实践、预期输出及验收标准；缺资料时写明待验证，不编造实测、账号资格或收益。企业宣传和吞吐指标不能取代读者自己的实践结果。

## 安装配置与手动入口

从仓库根目录安装：

```sh
uv sync --frozen --extra dev
# 仅第一次创建配置；已有配置时先备份并合并 sources/processing/digest，保留自己的授权设置。
cp profiles/personal-radar/config.public-web.json data/config.json
```

公开配置不包含任何密钥。模型仍为 PENDING_AUTH，必须在运行环境完成适用授权、列出真实模型并通过推理检查后再选择模型。不能把 Codex 本身可对话当成 Horizon 已获模型授权。

已有 Markdown 可直接仅向本人手动投递：

```sh
horizon-wechat-users send --file 简报.md --to owner --dry-run
horizon-wechat-users send --file 简报.md --to owner
```

原 `horizon` 生成管道接入全部启用来源；`daily-local.py generate` 与 `deliver` 分离，后者限定 owner。手动多人 CLI 保留，详见 WECHAT-READERS.md。

## 云端验收条件

代码合入 main 只代表仓库更新；云环境需要更新/重新发布并启动真实任务验证。必须分别证明：云主机身份及代码版本、原文抓取、模型推理、实践简报生成、本人微信收件，以及真正可唤醒该云任务的北京时间09:00调度。

截至本轮代码准备时，Mac 抓取和微信手动投递有证据，云端模型授权和自动调度尚未验证。不能把本地 LaunchAgent、容器内 cron 或后台进程宣称为已完成云调度。全部验收通过后才发送“已完成”微信通知。
