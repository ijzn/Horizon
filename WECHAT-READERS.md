# 微信多人日报

当前实现是每位读者独立扫码、各自私聊收件。现有自己的绑定以 `owner` 自动保留，无需重新扫码；朋友绑定不会覆盖它。

运行目录为本项目目录。命令既可使用 `horizon-wechat-users`，也可使用 `.venv/bin/python -m src.services.wechat_subscribers_cli`。

## 添加朋友

```sh
.venv/bin/horizon-wechat-users add "朋友甲" --login
```

记下输出的收件人 ID，将输出路径的二维码图片发给这位朋友。请对方用自己的微信扫码确认，再给机器人发送“开始日报”。二维码会过期；需要时重新运行下面的登录命令。每人使用不同 ID，不能把自己的二维码用于添加其他人。

```sh
.venv/bin/horizon-wechat-users list
.venv/bin/horizon-wechat-users login 收件人ID
.venv/bin/horizon-wechat-users status 收件人ID --refresh
.venv/bin/horizon-wechat-users test --to 收件人ID
```

列表只显示昵称、ID 和绑定状态，不显示微信平台账号、Token 或会话凭据。

以后管理自己也统一使用 `horizon-wechat-users login owner`、`status owner`；旧的单人 `horizon-wechat` 入口为兼容保留，不要与多人入口同时修改同一人的登录会话。

## 手动推送与群内分享

简报只需生成一次，已有 Markdown 可直接手动推送，不需要再次调用 AI，也不要求它是今天的日报。

```sh
# 先预览：不连接微信、不发送，可复制正文手动转发到普通微信群
.venv/bin/horizon-wechat-users send --file 简报.md --dry-run

# 推送给全部启用的订阅者
.venv/bin/horizon-wechat-users send --file 简报.md

# 只发给一位；多个 --to 可选多人
.venv/bin/horizon-wechat-users send --file 简报.md --to 收件人ID

# 明确要求重新发送相同内容
.venv/bin/horizon-wechat-users send --file 简报.md --to 收件人ID --force
```

正常重试跳过已成功收件人，只补发失败者。新增朋友即使当天较晚绑定，也能收到此前已经生成的简报。同一个微信误绑两个 ID 时，同一轮投递只发送一份。发送后崩溃但未保存成功记录，或微信长消息只发出部分分段时，重试仍可能重复，请留意提示。

```sh
.venv/bin/horizon-wechat-users disable 收件人ID
.venv/bin/horizon-wechat-users enable 收件人ID
.venv/bin/horizon-wechat-users remove 收件人ID
```

remove 表示停用推送，保留本机历史和会话，方便恢复。停用不能撤回已发消息。

## 管道与状态

原有 `horizon` 日报管道及本地 `daily-local.py deliver` 均接入同一多人投递层；手动和定时入口共用去重记录。飞书已暂停。云端定时任务尚未验证或启用，不能仅凭本地安装认定云端已部署此修改。

会话按人保存在 `data/wechat_subscribers/<ID>/wechat_session.json`；自己的历史会话仍在 `data/wechat_session.json`。昵称/状态和发送记录也只保存在本机 data 目录，均不应提交仓库。

微信当前接入有回复次数限制，读者需要不定期再给机器人发消息刷新额度。当前通道未实现普通微信群机器人；群内可先手动转发预览正文，将来接入群通道时可复用简报生成与投递结果处理。
