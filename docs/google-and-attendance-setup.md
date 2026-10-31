# 接入 Gmail 和考勤：从零开始的设置步骤

这份文档写给想让 monash 也能查学校邮箱和考勤的人。两部分都是可选的，默认不装也不开。邮件和登录状态只存在你自己的电脑上，不经过维护者。

**先想一想要不要用这里的 Gmail。** 如果只是想在 Claude 或 ChatGPT 里问问邮箱里有什么，用它们自带的 Gmail 连接器最简单，点一下连接就行。这里的 Gmail 多出来的是：

- 和考勤连在一起，找签到码时会顺便搜学校邮件；
- 邮件存在自己电脑上，能离线搜，附件里 PDF 和图片的文字也能搜；
- Codex、Cursor 这些没有自带 Gmail 的软件也能用。

只想要考勤、不用 Gmail 的话，第一步不用做，第二步只做开头的安装（可选依赖写 `[attendance]` 就行），然后看第三步。

步骤来自 2026-10-31 在真实 Monash 账号上的第一次完整接入，踩过的坑都写在对应位置。

## 一、（可选）用你自己的 Google 应用

安装包里自带了一个 Google 应用，**大多数人可以跳过这一步**，直接看第二步。下面这些情况才需要自己建：

- 登录时 Google 提示这个应用的用户已满（没经过 Google 审核的应用最多 100 个用户）；
- 你想完全用自己的项目和凭据；
- 你在自己的服务器上部署，想和别人分开。

建好以后，第二步登录时加上 `--client 你下载的JSON文件`。

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
monash gmail login
```

- 浏览器里选**学校邮箱**，走 Monash 的 Okta 登录和 MFA。
- 授权页上会显示“Google 尚未验证此应用”。这是因为个人项目过不了 Google 那套收费的安全审核，点“高级”，再点“继续”就行。
- 用的是第一步自己建的应用的话，改成 `monash gmail login --client 你下载的JSON文件`。macOS 上可以先输入 `monash gmail login --client ` 和一个空格，再把文件从访达拖进终端。
- 想让程序核对你选的是不是学校邮箱，可以在命令前面加 `MONASH_KIT_GMAIL_ACCOUNT=你的学校邮箱 `（Windows PowerShell 里先运行 `$env:MONASH_KIT_GMAIL_ACCOUNT="你的学校邮箱"`），选错账号会拒绝保存。
- 如果看到 “Monash University has not granted access”，说明学校管理员拦截了第三方应用，这条路走不通。
- 默认申请 `gmail.modify`：能读邮件，也能让 AI 帮你起草、发草稿、改标签（归档、标已读、加星标），不能彻底删除邮件。Google 授权页上会写“查看和修改邮件”。每次操作都要你看过预览、确认后才执行，见第七步。
- 只想让它读邮件，加 `--read-only`，只申请 `gmail.readonly`，邮箱里什么都不会变，连未读状态都不动。

授权成功后，邮件工具和邮箱操作在本地 AI 软件里都会自动打开（设置 `gmail_enabled`、`gmail_manage_enabled`；用了 `--read-only` 就只开前一个）。接着下载邮件：

```sh
monash gmail sync          # 默认先下最近 90 天，一次最多 5 页，没下完再运行一次
monash gmail todo          # 邮件待办：最近几天收件箱里没读的学校邮件
monash gmail search 交换    # 离线全文搜索
```

之后 AI 软件开着的时候，后台每 30 分钟同步一次邮件（`monash config gmail_sync_minutes 60` 改间隔，`0` 关掉）。第一次下载没下完时大约一分钟后接着下。想马上看到最新的，就手动运行 `monash gmail sync`。同步中途出错不用担心，已经下好的页都保存了，再运行一次会接着下。

## 三、考勤

```sh
monash attendance login
monash attendance sync       # 读门户，要开浏览器，一两分钟
monash attendance overview   # 看已保存的班次，不联网
monash attendance codes      # 从 Moodle、Ed、学校邮件里找签到码
monash attendance status
```

- 第一次用要先给 playwright 装浏览器：macOS/Linux 运行 `"$(uv tool dir)/monash-study-kit/bin/python" -m playwright install chromium`，Windows 运行 `& "$(uv tool dir)\monash-study-kit\Scripts\python.exe" -m playwright install chromium`。
- `login` 会打开浏览器让你登录考勤门户（Okta + MFA），保存的是门户的登录状态，不保存密码。成功后本地 AI 软件里的考勤工具和签到提交都会自动打开（设置 `attendance_enabled`、`attendance_submit_enabled`）。
- `sync` 读取最近几天（`--days`，最多 14）和本周还没到的班次及签到状态，存到本地。AI 软件里也能让它自己读（“刷新一下我的考勤”），在后台跑，读完再告诉你。
- `codes` 找到的只是候选签到码，每个都标了来自哪条公告、帖子或邮件。同一节课找到两个不同的码会标成冲突，一定要自己核对。
- 默认是**马来西亚校区**门户 `https://attendance.monash.edu.my`，时区 `Asia/Kuala_Lumpur`。其他校区可以设置 `MONASH_ATTENDANCE_ORIGIN` 和 `MONASH_ATTENDANCE_TZ`，但页面解析只在马来西亚校区验证过，换校区前要先确认页面结构。
- 门户的日期是用页面上的下拉框 `#daySel` 切换的（URL 里的日期会被忽略），大约只能选今天前后两周。
- 登录状态能维持多久还没测准。程序会自己记：每次登录、每次读成功、第一次发现过期都记一笔，`monash attendance status` 里的 `session_lifetime` 会显示这次登录已经撑了多久，以及以前几次是在几小时到几小时之间失效的。报登录过期时重新运行 `monash attendance login`。

### 考勤的后台读取和三个设置

AI 软件开着时，考勤按课表自动读：工作日早上 8 点读最近三天（看这几天的班次），每节还没签到的课开始 15 分钟后再读当天（看签上没有）。已经签过的课、晚上、周末都不跑。电脑睡着错过了，醒来只补一次。

| 设置 | 默认 | 作用 |
|---|---|---|
| `attendance_auto_sync` | `true` | 按课表自动读；`false` 就只在你或 AI 要的时候读 |
| `attendance_fast_read` | `true` | 不开浏览器，直接带着登录 cookie 请求门户页面，一次读完两周，几秒钟。页面结构对不上会自动退回浏览器 |
| `attendance_keepalive_minutes` | `0` | 每隔多少分钟用一次轻量请求给登录续期，不开浏览器；`0` 不续期 |

想确认快速读法在你的门户上也没问题，运行：

```sh
monash attendance probe
```

它会用两种方法各读一遍门户（不开浏览器的 Python 版，和原来浏览器里的读法），逐天对照班次数量、签到状态和有没有签到链接。输出里只有数量和“对不对得上”，没有课程、时间、链接或 cookie，可以直接发给维护者。2026-10-31 在马来西亚校区门户上，15 天全部一致。如果你那边显示不一致，先 `monash config attendance_fast_read false` 改回浏览器，再把输出发给维护者。

`attendance_keepalive_minutes` 建议先保持 `0` 用几天，看 `session_lifetime` 里登录自然能撑多久；如果撑不过一天，再设成 `30` 试试续期有没有用。

## 四、在 AI 软件里用

登录完**重启一下 AI 软件**（Claude Desktop、Codex、Cursor 等），新工具才会出现。之后直接问：

- “我邮箱里有什么要处理的？”
- “找一下交换申请的邮件，把全文给我看。”
- “这周还有哪节课没签到？”

几点说明：

- 同时开着 Claude 官方的 Gmail 连接器时，Claude 有时会用错。monash 会告诉 Claude 查学校邮件优先用它；还是用错的话，开个新对话，问的时候说“学校邮箱”，或者在这个对话里关掉官方 Gmail 连接器。
- 想在网页版或手机上的 Claude、ChatGPT 里也用，见下一节“远程使用”。
- `monash help` 和 AI 里的“功能大全”只在开启后才列出邮件和考勤功能。

### 远程使用（可选）

`monash remote serve` 的用法见 README。服务器上按第二、三步登录好之后：

1. 在服务器上运行 `monash remote pair` 拿一次性配对码。
2. 在 Claude 或 ChatGPT 里添加连接器，授权页上会多出几个勾选框，只列服务器上已经开了的功能：
   - 学校邮箱（只读）、考勤（只读）默认勾上；
   - 邮箱操作、签到提交默认不勾，要用就自己勾（服务器上也要先按第五步打开）。
   不想让这个软件碰邮箱，就把邮箱那项去掉，Moodle 和 Ed 照样能用。
3. 每个软件分开授权。比如手机上的 Claude 只勾考勤，ChatGPT 只勾邮箱，互不影响。`monash remote clients` 能看到每个软件拿到了哪些功能。
4. 之前已经配对过的软件不会自动多出邮箱和考勤。要加，就在软件里断开再用新配对码连一次；要收回，用 `monash remote revoke --client 客户端ID` 后重新配对。服务器上把某个功能的设置关掉，所有软件马上都用不了它。

## 五、邮箱操作和签到提交

登录以后，起草、发送草稿、改标签和签到提交在本地 AI 软件里默认就能用，不用另外设置。每次都要你确认，见第七步。

不想要哪一项，就关掉它，改完重启 AI 软件：

```sh
monash config gmail_manage_enabled false
monash config attendance_submit_enabled false
```

想连 Google 的写权限都不给，就用 `monash gmail login --read-only` 重新登录一次。远程使用时，这两项还要在配对页上给具体的软件勾上才有。

## 六、常见问题

| 现象 | 原因和处理 |
|---|---|
| 同步报 `HTTP 403 (rateLimitExceeded)` | 新项目的 Gmail 额度是每用户每分钟 6000 单位，拉一封完整邮件算 5 个。代码会自动等待重试；偶尔出现不用管。 |
| `invalid_client` / 刷新令牌失败 | 客户端密钥被删除或换过，或者应用还停在“测试”状态（7 天过期）。按第一步检查，再用新的客户端 JSON 重新运行第二步的登录。 |
| `This cache belongs to another mailbox` | 本地副本属于另一个邮箱。一个数据目录只放一个邮箱，第二个邮箱要另设 `MONASH_KIT_HOME`。 |
| Claude 查邮件时用了官方 Gmail | 见第四步。 |
| AI 软件里看不到邮件或考勤工具 | 还没登录成功，或者没重启 AI 软件。`monash config` 看一下 `gmail_enabled`、`attendance_enabled` 是不是 `True`。远程使用时还要确认配对时勾了这一项，否则重新配对。 |
| 后台同步好像没在跑 | 看 `mcp.log`。授权失效时只记一次“邮件同步要重新授权”，重新 `monash gmail login` 就好。 |
| 考勤报找不到 playwright 或浏览器 | 没装 `[attendance]`，或者没运行第三步里的 `playwright install chromium`。 |
| 不确定哪里没设置好 | `monash doctor` 会多出“学校邮箱”和“考勤”两项，告诉你缺什么、怎么修；输出里不带邮箱地址，可以直接发给别人帮忙看。 |
| `probe` 报 `CERTIFICATE_VERIFY_FAILED` 或“证书校验不过” | 门户服务器可能没把中间证书发全，Chrome 会自己补，Python 不会。新版会用系统的证书校验（`truststore`，装 `[attendance]` 时一起装上）；还是不行就一直用浏览器读，不影响使用。证书校验始终开着，不要设置跳过校验。 |
| 考勤同步超时 | 门户页面结构变了。先确认 `#daySel` 下拉框和 `dayPanel_<日期>` 区块还在。 |

## 七、写操作的保护

起草、发信、改标签、考勤提交都分两步：先返回完整预览和一次性确认凭据，用户确认后才执行。

- 本地登录后默认打开，可以按第五步关掉；远程还要在配对时给那个软件勾上。
- 确认凭据按软件分开，一个软件准备的操作，另一个软件执行不了。
- AI 必须把预览原样给你看，你确认了这一项，它才能带着 `user_confirmed=true` 去执行，不能自己替你确认。
- 凭据 10 分钟有效、只能用一次，绑定预览时的内容；发送草稿前会核对草稿在预览后有没有被改过。
- 执行前的检查没通过时记为“已拒绝”（确定没有发出）；执行中出错记为“结果未知”，不会自动重试，需要先去确认实际状态。
- 考勤提交只在你明确说自己实际到课时才会准备，不会给未来或已经签过的班次提交。这一步还没在真实账号上验证过。
- 不提供彻底删除邮件的操作。
