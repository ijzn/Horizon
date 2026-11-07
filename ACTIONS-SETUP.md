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

## 初次验收

1. 创建两个Secrets；密钥与状态不发聊天。
2. 在main手动run + bootstrap=true，验证真实Linux采集、模型生成、owner微信投递以及加密artifact保存。
3. 新run以bootstrap=false恢复刚保存的状态，使用deliver验证重试不重复发送。
4. 验证手动入口后，观察下一次自动08:07/09:00事件与本人实收。只有这一步也通过，才可发送全部完成通知。

模型授权失效、订阅额度不足或微信回复额度耗尽时仍可能需要人工重新授权/给机器人发“开始日报”。这些是渠道限制，不能通过定时器保证永久免交互。
