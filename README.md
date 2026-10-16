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
双击 `start.bat`（首次会自动创建虚拟环境并安装依赖，优先选用 Python 3.13/3.12，缺依赖会自动补装）。想一起拉起 NapCat：编辑 `start.bat` 填 `NAPCAT_BAT`。
看到 `NapCat 已连接` 后，让白名单里的朋友（或小号）发条消息试试。

## 控制面板（网页界面）
bot 启动后会自动用浏览器打开 **控制面板**（默认 `http://127.0.0.1:8765`；终端里也会打印完整链接）。

- **左侧**：所有私聊（包括不在 AI 名单里的人），带未读圆点；可搜索，可只看「AI 名单」。
- **中间**：聊天记录，实时更新。气泡下面的小标签区分来源：`AI`（AI 回的）、`手动`（你在面板里发的）、`你（其他设备）`（你在别处发的）。
- **发送**：输入框回车即以你的身份发出，不会触发 AI 暂停，AI 之后会知道你说过什么。
- **试聊 / 起草**：输入框有内容 → 当作“对方说的话”，看 AI 会怎么回；输入框为空 → 让 AI 针对当前聊天起草一条。只预览，满意了可一键以你的身份发出。对非名单好友也能用。
- **暂停 / 恢复 AI**：按人控制，可暂停 30 分钟、2 小时或直到你恢复。顶部有「AI 总开关」。
- **加入好友**：从你的 QQ 好友列表里点选，或输入 QQ 号；好友详情里写称呼和关系备注。
- **人设 / 设置 / 日志**：网页里直接改，保存后立即生效，不用重启。

这些改动（名单、设置、总开关、“一直暂停”）保存在 `data/` 下，重启后仍然保留，不会改动你的 `config.yaml`。

**安全**：面板能以你的账号发消息，所以只监听本机，并且需要令牌。令牌自动生成，保存在 `data/ui_token.txt`，浏览器会记住它。不要把带 `token=` 的链接发给别人，也不要把 `ui_host` 改成非本机地址。

**隐私**：为了让你在面板里看到所有人的消息，非白名单好友的私聊也会存进本机的 `data/chat.db`（AI 永远不会回复他们）。不想存就在 `config.yaml` 里设 `log_non_whitelisted: false`。

### 备用：在 QQ 里给自己发命令
给自己（“我的手机/我的电脑”）发 `/ai on|off|status|resume|pause <谁> [分钟]|resume [谁]|friends [关键字]|whitelist|allow <谁> [备注]|deny <谁>|say <谁> <话>|ask <谁> <话>|clear <QQ号>`。
旧的终端命令模式需要在 `config.yaml` 里设 `console_input: true`（默认关闭，面板已经取代它）。

## 行为说明
- 只有**白名单**好友会被 AI 回复；其他人的消息只显示、不回复。
- 对方连发几条会合并成一次回复（`debounce_seconds`）。
- 你在别处（手机 QQ 等）亲自给白名单好友发消息 → 对该好友 AI 暂停 `takeover_minutes` 分钟，你的话也会写入历史。不想要这个行为就把 `takeover_minutes` 设为 0。在控制面板里发消息不触发暂停。
- 图片/语音/表情不会被理解，以 `[图片]` 等占位交给模型。
- 历史存在 `data/chat.db`（SQLite），重启不丢。
- OpenRouter 调用失败时不回复也不重试风暴，日志里会有原因。

## 测试
`python -m unittest discover -s tests`（用假 NapCat + 假 LLM 走完整流程，包括控制面板的接口，不需要 QQ 或 key）。
