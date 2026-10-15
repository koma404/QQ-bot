# qq-llm-bot

让 LLM 代你的 QQ 大号回复**白名单好友的私聊**。NapCat（OneBot v11）+ OpenRouter，纯本机运行。

```
朋友 ↔ QQ ↔ NapCat（登录你的号）──WebSocket(127.0.0.1:6700)──> bot.py ──> OpenRouter
```

## ⚠ 风险
NapCat 属于非官方协议，大号有被风控/封禁的可能。已内置的缓解：仅白名单私聊、不碰群聊、
拟人延迟、限频、静默时段、你亲自回复时自动让位。不要给太多人开、不要 24 小时无人值守长期挂。

## 一、安装 NapCat（Windows，约 10 分钟）
1. 到 <https://github.com/NapNeko/NapCatQQ/releases> 下载 Windows 一键包（`NapCat.Shell.Windows.OneKey.zip` 之类），解压。
2. 运行其中的启动脚本，扫码或快速登录你的 QQ。
3. 浏览器打开 NapCat WebUI（启动日志里会打印地址和 token，一般是 `http://127.0.0.1:6099/webui`）。
4. 「网络配置」→ 新建 **WebSocket 客户端**：
   - URL：`ws://127.0.0.1:6700`
   - 消息格式：**Array**
   - **勾选「上报自身消息」**（reportSelfMessage，让 bot 能发现你亲自回复了）
   - 启用并保存。（如果设置了 token，同样填进 `config.yaml` 的 `onebot_token`）

> NapCat 版本更新较快，界面文字可能略有差异；找不到选项就把截图发给我。

## 二、配置 bot
1. 装 Python 3.10+（安装时勾选 *Add to PATH*）。
2. `copy config.example.yaml config.yaml`，填入 OpenRouter key、白名单 QQ 号、模型。
3. 编辑 `persona.md`（人设，每次回复都会重新读取，改完立即生效）。

## 三、启动
双击 `start.bat`（首次会自动创建虚拟环境）。想一起拉起 NapCat：编辑 `start.bat` 填 `NAPCAT_BAT`。
看到 `NapCat 已连接` 后，让白名单里的朋友（或小号）发条消息试试。

## 控制
在 QQ 里给**自己**（"我的手机/我的电脑"）发，或直接在 bot 控制台输入（不带 `/ai`）：

| 命令 | 作用 |
|---|---|
| `/ai on` / `/ai off` | 总开关 |
| `/ai status` | 查看状态、哪些人在让位暂停 |
| `/ai friends [关键字]` | 列出好友 QQ 号（可按昵称或备注搜索） |
| `allow <QQ号或名字> [备注]` | 加入白名单，立即生效，保存在 data/friends.json |
| `deny <QQ号或名字>` | 移出白名单 |
| `whitelist` | 查看当前白名单 |
| `say <QQ号或名字> <内容>` | 以 AI 的身份发一条消息给好友，**不会**触发暂停，会记入历史 |
| `pause <QQ号或名字> [分钟]` | 暂停对某人的 AI 回复；不写分钟则一直暂停 |
| `resume [QQ号或名字]` | 恢复某人的 AI 回复（若他刚发了消息会补回）；不写人则恢复所有人 |
| `ask <QQ号或名字> <内容>` | **试聊**：只在终端显示 AI 会怎么回，不发送、不入历史 |
| `/ai clear <QQ号>` | 清空某人的历史 |

## 行为说明
- 对方连发几条会合并成一次回复（`debounce_seconds`）。
- 你在别处（手机 QQ 等）亲自给白名单好友发消息 → 对该好友 AI 暂停 `takeover_minutes` 分钟，你的话也会写入历史。不想要这个行为就把 `takeover_minutes` 设为 0。终端的 `say` 不触发暂停。
- 图片/语音/表情不会被理解，以 `[图片]` 等占位交给模型。
- 历史存在 `data/chat.db`（SQLite），重启不丢。
- OpenRouter 调用失败时不回复也不重试风暴，日志里会有原因。

## 测试
`python -m unittest discover -s tests`（用假 NapCat + 假 LLM 走完整流程，不需要 QQ 或 key）。
