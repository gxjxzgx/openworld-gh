# Openworld 免费 VPS 自动抢 (GitHub Actions, 每 10 分钟)

`openworld_grab.py` 每 10 分钟查一次 `Free FREE PROXMOX` 库存:
- 缺货 → 静默结束, 只更新 cookie
- 有货 → Telegram 立刻推送, 并尝试自动领取(默认选 Debian 13)
- 领取被 CAPTCHA/人机验证挡住 → Telegram 推送告警, 人工去抢

## 部署步骤

### 1. 建仓库并上传文件
新建一个 GitHub 仓库, 把本目录所有文件传上去
(`openworld_grab.py` 放仓库根目录, workflow 放 `.github/workflows/watch.yml`)。
可用网页直接上传, 不用敲命令。

> 建议设为 **Public**: 公开仓库 Actions 不限时长;
> 私有仓库每月只有 2000 分钟免费额度, 每 10 分钟一轮跑满一个月约需 13000 分钟, 会超。
> 代码里没有任何密钥, 密钥全在 Secrets 里, 公开也安全。

### 2. 加 Secrets
仓库 Settings → Secrets and variables → Actions → New repository secret,
逐个添加:

| Secret 名 | 内容 |
|---|---|
| `OPENWORLD_EMAIL` | openworld 登录邮箱 |
| `OPENWORLD_PASSWORD` | openworld 登录密码 |
| `GMAIL_APP_PASSWORD` | Gmail 应用专用密码(见下) |
| `TG_BOT_TOKEN` | Telegram Bot Token(见下) |
| `TG_CHAT_ID` | Telegram Chat ID(见下) |

### 3. Gmail 应用专用密码 (用于自动读 Clerk 验证码)
1. Google 账号必须先开两步验证
2. 打开 https://myaccount.google.com/apppasswords
3. 随便起个名字 (如 openworld), 生成 16 位密码
4. 把这 16 位填进 `GMAIL_APP_PASSWORD`
> 不配这个也行, 但 Clerk 要验证码时脚本会自动放弃并 TG 告警。

### 4. Telegram 推送
1. 跟 [@BotFather](https://t.me/BotFather) 说 `/newbot`, 按提示起名字,
   拿到 `123456:ABC-DEF...` 格式的 token → 填 `TG_BOT_TOKEN`
2. 给你刚建的 bot 发一条任意消息 (比如 `hi`)
3. 浏览器打开 `https://api.telegram.org/bot<你的token>/getUpdates`,
   找到 `"chat":{"id": 123456789` 里的数字 → 填 `TG_CHAT_ID`

### 5. 开跑
Actions 标签页 → 点 `openworld-stock-watch` → `Run workflow` 手动跑一次,
看日志确认登录和查库存正常。之后每 10 分钟自动跑。

## 说明
- cookie 存在 Actions 缓存里跨轮复用, 避免每轮都重新登录
- GitHub 定时任务实际会有几分钟延迟, 这是正常的
- 免费套餐 7 天有效、每 24 小时可手动续期一次, 抢到后记得去面板续期
