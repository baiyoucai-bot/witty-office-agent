---
name: mail-desk
description: 在内网用 IMAP/SMTP 分析、起草、发送邮件和附件。用户提到收件箱、回信、转发、附件、草稿、周报邮件、发信时使用。不连接公网邮箱品牌。
network: intranet
---

# 邮件桌面

这是业务技能，不是内核循环。通道来自 `config/runtime.toml` 的 `[email]` 和 `WITTY_MAIL_USER` / `WITTY_IMAP_PASSWORD` / `WITTY_SMTP_PASSWORD`。不要写死 QQ / 163 / Gmail。

工具：

- `mail_status` 看 IMAP/SMTP 是否已配、本地有几封草稿
- `mail_list` 列标题，可带 `query`
- `mail_read` 按 uid 读全文
- `mail_analyze` 从 uid / 草稿 / 粘贴正文提炼待办和附件
- `mail_draft` 建或改本地草稿
- `mail_attach` 把用户给出的本地路径挂到草稿
- `mail_save` 把附件落到本机路径
- `mail_reply` 按已读邮件生成回复草稿
- `mail_send` 经 SMTP 发出草稿（危险，须批准）

## 路由

1. 先 `mail_status`。主机为空就说明缺配置，停止假装收发。
2. 看信：`mail_list` → `mail_read` → `mail_analyze`。分析只根据读到的正文。
3. 写信：`mail_draft` 反复改；附件只用用户点名的本地文件。
4. 发出前向用户复述收件人和主题，再 `mail_send`。
5. 处理完可用 `diary_write` 记一行。

## 纪律

- 不要调用公网邮件 API 或网页邮箱。
- 不要编造未读到的制度、数字或附件。
- 密码不会出现在工具结果里；不要向用户回显口令。
