# 接入 Gmail 和考勤：从零开始的设置步骤

这份文档写给想让 monash 也能查学校邮箱和考勤的人。两部分都是可选的，默认不装也不开。每个人用自己的 Google Cloud 项目、自己的凭据，数据只留在自己的电脑上。

只想要考勤、不用 Gmail 的话，第一步不用做，第二步只做开头的安装（可选依赖写 `[attendance]` 就行），然后看第三步。

步骤来自 2026-10-31 在真实 Monash 账号上的第一次完整接入，踩过的坑都写在对应位置。

## 一、Google Cloud：创建项目和 OAuth 客户端

用你的**个人** Google 账号登录 <https://console.cloud.google.com>，不要用学校账号。学校账号所在的 Workspace 组织通常不允许学生建项目。

1. **新建项目**，名字随意，例如 `Monash Student MCP`。
2. **启用 Gmail API**：API 和服务 → 库 → 搜索 Gmail API → 启用。
3. **Google Auth Platform → 品牌**：
   - 应用名称、用户支持邮箱、开发者联系邮箱：填你自己的。
   - **应用首页**和**隐私政策链接**：必须填，否则第 5 步的“发布应用”按钮是灰的。可以指向你的公开仓库和仓库里的 `PRIVACY.md`。
   - **已获授权的网域**：填上面两个链接所在的域名（例如 `github.com`）。
4. **受众**：用户类型选“外部”。“设为内部”是灰的，这是正常的：内部应用只能建在 Workspace 组织下。
5. **发布应用**，把状态从“测试”切到“正式版”。
   - 停在“测试”状态时，Gmail 授权的 refresh token **每 7 天失效**，后台同步会突然停止。
   - 只给自己用，不需要提交 Google 审核。授权时会看到“Google 尚未验证此应用”，点“高级”→“继续”即可。未经验证的应用最多 100 个用户。
6. **客户端** → 创建客户端 → 类型选“桌面应用”，然后**立刻下载 JSON**。
   - Google 不再允许事后查看或下载已有密钥。如果没保存下来，只能在客户端页面“Add secret”新建一个，再停用、删除旧的。
   - JSON 放在私有目录（权限 600），**不要放进任何仓库**。

## 二、安装并用学校邮箱授权 Gmail

这部分还没发布到 PyPI，现在要从 GitHub 源码装（`gmail` 和 `attendance` 两个可选依赖，只要一个就只写一个）：

```sh
uv tool install --reinstall "monash-study-kit[gmail,attendance] @ git+https://github.com/Waldo0926/monash-study-kit"
```

然后授权（会打开系统浏览器）：

```sh
MONASH_KIT_GMAIL_ACCOUNT=你的学校邮箱 monash gmail login --client /私有路径/google-client.json
```

Windows PowerShell 里先 `$env:MONASH_KIT_GMAIL_ACCOUNT="你的学校邮箱"`，再运行 `monash gmail login --client ...`。

- 浏览器里选**学校邮箱**，走 Monash 的 Okta 登录和 MFA。
- 设了 `MONASH_KIT_GMAIL_ACCOUNT` 时，会核对授权的账号是不是它，选错账号会拒绝保存。
- 如果看到 “Monash University has not granted access”，说明学校管理员拦截了第三方应用，这条路走不通。
- 默认只申请只读权限 `gmail.readonly`，不会改邮箱，连未读状态都不动。想让 AI 帮你起草、发草稿、改标签，加 `--manage` 再授权一次，见第五步。

授权成功后本地 MCP 会自动打开只读邮件工具（设置 `gmail_enabled`）。接着下载邮件：

```sh
monash gmail sync          # 默认先下最近 90 天，一次最多 5 页，没下完再运行一次
monash gmail todo          # 邮件待办：最近几天收件箱里没读的学校邮件
monash gmail search 交换    # 离线全文搜索
```

AI 软件里查的是本地这份副本。现在还没有后台自动同步邮件，想看最新的就先运行 `monash gmail sync`。同步中途出错不用担心，已经下好的页都保存了，再运行一次会接着下。

## 三、考勤

```sh
monash attendance login
monash attendance sync
monash attendance status
```

- 第一次用要先给 playwright 装浏览器：macOS/Linux 运行 `"$(uv tool dir)/monash-study-kit/bin/python" -m playwright install chromium`，Windows 运行 `& "$(uv tool dir)\monash-study-kit\Scripts\python.exe" -m playwright install chromium`。
- `login` 会打开浏览器让你登录考勤门户（Okta + MFA），保存的是门户的登录状态，不保存密码。成功后本地 MCP 会自动打开只读考勤工具（设置 `attendance_enabled`）。
- `sync` 读取最近几天（`--days`，最多 14）和本周还没到的班次及签到状态，存到本地。AI 软件里的 `attendance_overview` 读的就是这份。
- 默认是**马来西亚校区**门户 `https://attendance.monash.edu.my`，时区 `Asia/Kuala_Lumpur`。其他校区可以设置 `MONASH_ATTENDANCE_ORIGIN` 和 `MONASH_ATTENDANCE_TZ`，但页面解析只在马来西亚校区验证过，换校区前要先确认页面结构。
- 门户的日期是用页面上的下拉框 `#daySel` 切换的（URL 里的日期会被忽略），大约只能选今天前后两周。
- 登录状态能维持多久还没测准；`monash attendance status` 或 AI 报登录过期时，重新运行 `monash attendance login`。

## 四、在 AI 软件里用

登录完**重启一下 AI 软件**（Claude Desktop、Codex、Cursor 等），新工具才会出现。之后直接问：

- “我邮箱里有什么要处理的？”
- “找一下交换申请的邮件，把全文给我看。”
- “这周还有哪节课没签到？”

几点说明：

- 同时开着 Claude 官方的 Gmail 连接器时，Claude 有时会用错。monash 会告诉 Claude 查学校邮件优先用它；还是用错的话，开个新对话，问的时候说“学校邮箱”，或者在这个对话里关掉官方 Gmail 连接器。
- 远程 MCP（`monash remote serve`）**不提供**邮件和考勤工具，网页版或手机上的 Claude、ChatGPT 看不到它们。按客户端单独授权是以后的工作。
- `monash help` 和 AI 里的“功能大全”只在开启后才列出邮件和考勤功能。

## 五、可选：让 AI 帮你操作邮箱和签到

默认 AI 只能读。起草、发送草稿、改标签（归档、标已读、加星标）和考勤签到提交，要你在设置里明确打开：

```sh
monash gmail login --client /私有路径/google-client.json --manage   # 邮箱操作要 gmail.modify 权限
monash config gmail_manage_enabled true
monash config attendance_submit_enabled true
```

改完重启 AI 软件。不想要了就把对应设置改回 `false`。

## 六、常见问题

| 现象 | 原因和处理 |
|---|---|
| 同步报 `HTTP 403 (rateLimitExceeded)` | 新项目的 Gmail 额度是每用户每分钟 6000 单位，拉一封完整邮件算 5 个。代码会自动等待重试；偶尔出现不用管。 |
| `invalid_client` / 刷新令牌失败 | 客户端密钥被删除或换过，或者应用还停在“测试”状态（7 天过期）。按第一步检查，再用新的客户端 JSON 重新运行第二步的登录。 |
| `This cache belongs to another mailbox` | 本地副本属于另一个邮箱。一个数据目录只放一个邮箱，第二个邮箱要另设 `MONASH_KIT_HOME`。 |
| Claude 查邮件时用了官方 Gmail | 见第四步。 |
| AI 软件里看不到邮件或考勤工具 | 还没登录成功，或者没重启 AI 软件。`monash config` 看一下 `gmail_enabled`、`attendance_enabled` 是不是 `True`。 |
| 考勤报找不到 playwright 或浏览器 | 没装 `[attendance]`，或者没运行第三步里的 `playwright install chromium`。 |
| 考勤同步超时 | 门户页面结构变了。先确认 `#daySel` 下拉框和 `dayPanel_<日期>` 区块还在。 |

## 七、写操作的保护

起草、发信、改标签、考勤提交都分两步：先返回完整预览和一次性确认凭据，用户确认后才执行。

- 本地 MCP 默认不注册这些工具，要按第五步打开。
- AI 必须把预览原样给你看，你确认了这一项，它才能带着 `user_confirmed=true` 去执行，不能自己替你确认。
- 凭据 10 分钟有效、只能用一次，绑定预览时的内容；发送草稿前会核对草稿在预览后有没有被改过。
- 执行前的检查没通过时记为“已拒绝”（确定没有发出）；执行中出错记为“结果未知”，不会自动重试，需要先去确认实际状态。
- 考勤提交只在你明确说自己实际到课时才会准备，不会给未来或已经签过的班次提交。这一步还没在真实账号上验证过。
- 不提供彻底删除邮件的操作。
