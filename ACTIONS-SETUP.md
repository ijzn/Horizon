# 免费 GitHub Actions 微信日报

运行位置改为 GitHub 的临时 Ubuntu runner，不依赖 Mac 开机；Codex 负责开发维护。公开仓库使用标准runner的计算免费，模型仍消耗已授权订阅额度；artifact存储有额度限制。

计划：北京时间08:07准备日报，09:00运行（已有当日已验证日报就直接投递，否则补生成后再发）。GitHub可能延迟或丢弃定时事件，不能保证九点准点。公开仓库连续60天无活动，GitHub会停用定时任务，需关注Actions状态并恢复。手动 Actions → Personal Horizon WeChat → Run workflow 可选run/generate/deliver；本地手动CLI也保留。

## 私密状态

仅保存运行必需的Horizon本项目模型授权、微信绑定和去重记录，以及当日报告；不复制其他应用的认证文件。状态只以Fernet认证加密包上传artifact，不上传data目录、明文授权、日志或二维码。密钥放仓库Actions Secret，不能进入代码、artifact或聊天。

需要两个仓库Secret（只授权ijzn/Horizon）：

- HORIZON_STATE_KEY：随机生成的Fernet密钥。
- HORIZON_BOOTSTRAP_STATE：首次加密包的base64。

首次或有意恢复时，手动运行并勾选bootstrap=true；常规定时必须恢复本workflow/main最新artifact，缺失、过期、损坏、下载失败均停止，绝不偷偷恢复旧bootstrap。生成失败后仍加密保存已刷新的授权；恢复失败时禁止上传空状态，打包失败也禁止上传旧包。

初始迁移须等本机生成停止后快照，之后不要让Mac与Actions同时刷新同一模型授权。工作流共用并发组且不取消正在运行的任务。上传artifact保留90天；每次成功保存都产生新的最新状态。

## 工作实践分类与详情

当前按前端、产品、运营、AI四类筛选“工作如何使用AI”，每类最多一条，无合格内容不凑数。微信只发具体用途、一个可开始的动作和简短证据说明；完整报告、操作条件、代码、来源放到手机版详情页。

首次在仓库 Settings → Pages 将 Source 设为 GitHub Actions。日报工作流仅上传单独生成的 `data/pages-site/` HTML 白名单；任何模型/微信凭据、状态JSON或日志均不进入公开站点。页面按日期和报告hash区分，同一日期重新生成不会覆盖已经分享的链接，保留最近90天。

工作流先生成完整报告和绑定其hash的导读，再发布详情，最后核对公开URL的完整内容hash一致才推微信。页面/导读失败会停止推送，仍加密保存最新授权。8:07只准备并发布，9:00发送；手动 `generate` 重新生成但不发送，`run`复用当日成功报告后发送，`deliver`只发送。之后bootstrap始终false。

手动 `format` 可仅重排当日成功报告并发布详情，不重新抓取或评分、不发微信。零入选时使用明确的无合格内容说明，禁止为了生成导读编造条目。长文评分读取开头、中段和结尾；私有加密状态保留最近一次候选评分快照用于诊断，公开站点不含该快照。

旧Deploy Docs仅向gh-pages分支写项目文档；Pages source为Actions后，日报由Personal Horizon WeChat的官方deploy-pages动作发布。

## 初次验收步骤

1. 创建两个Secrets；密钥与状态不发聊天。
2. 在main手动run + bootstrap=true，验证真实Linux采集、模型生成、owner微信投递以及加密artifact保存。
3. 新run以bootstrap=false恢复刚保存的状态，使用deliver验证重试不重复发送。
4. 验证手动入口后，观察下一次自动08:07/09:00事件与本人实收。只有这一步也通过，才可发送全部完成通知。

模型授权失效、订阅额度不足或微信回复额度耗尽时仍可能需要人工重新授权/给机器人发“开始日报”。这些是渠道限制，不能通过定时器保证永久免交互。
