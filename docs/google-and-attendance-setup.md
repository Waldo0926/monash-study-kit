# 接入 Gmail 和考勤：从零开始的设置步骤

这份文档写给想在**自己的电脑或自己的服务器**上运行学生 MCP 的人。每个人用自己的 Google Cloud 项目、自己的凭据，数据只留在自己的机器上。

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

## 二、用学校邮箱授权 Gmail

在你自己的电脑上运行（会打开系统浏览器）：

```sh
MONASH_KIT_GMAIL_ACCOUNT=你的学号邮箱@student.monash.edu \
python student.py login gmail --client /私有路径/google-client.json --manage
```

- `--manage` 同时授权起草、发信、改标签。只想读邮件就去掉它。
- 浏览器里选**学校邮箱**，走 Monash 的 Okta 登录和 MFA。
- 脚本会核对授权的账号是不是 `MONASH_KIT_GMAIL_ACCOUNT`，选错账号会拒绝保存。
- 如果看到 “Monash University has not granted access”，说明学校管理员拦截了第三方应用，这条路走不通。

在服务器上运行时，加 `--push --host 你的SSH主机名`（或设置 `MOODLE_SYNC_HOST`），令牌会通过 SSH 传到服务器的私有目录。

## 三、考勤

```sh
python student.py login attendance --push --host 你的SSH主机名
```

- 会打开浏览器让你登录考勤门户（Okta + MFA），保存的是门户的登录状态，不保存密码。
- 默认是**马来西亚校区**门户 `https://attendance.monash.edu.my`，时区 `Asia/Kuala_Lumpur`。其他校区可以设置 `MONASH_ATTENDANCE_ORIGIN` 和 `MONASH_ATTENDANCE_TZ`，但页面解析只在马来西亚校区验证过，换校区前要先确认页面结构。
- 门户的日期是用页面上的下拉框 `#daySel` 切换的（URL 里的日期会被忽略），大约只能选今天前后两周。
- 登录状态能维持多久还没测准；`attendance_status` 报登录过期时重新运行上面的命令。

## 四、把 Claude / ChatGPT 连上来

1. 在服务器上生成一次性配对码：`python remote_mcp.py pair`。
2. 在 Claude（或 ChatGPT）里添加自定义连接器，地址是你服务器的 `/mcp`。
3. 授权页上选择“邮箱功能”和“考勤功能”，填配对码，提交。
   - 已经连过的客户端**不会自动获得**邮箱和考勤权限。要扩大权限，必须断开后用新配对码重新授权。
   - 如果重连时没出现授权页，说明客户端还在用旧登录：用 `python remote_mcp.py revoke <客户端ID>` 作废旧连接再连。
4. 建议把连接器命名为 “Monash Student MCP” 之类，别叫 “Moodle”，否则同时开着官方 Gmail 连接器时，Claude 不容易想到用它查邮件。

## 五、常见问题

| 现象 | 原因和处理 |
|---|---|
| 后台同步报 `HTTP 403 (rateLimitExceeded)` | 新项目的 Gmail 额度是每用户每分钟 6000 单位，拉一封完整邮件算 5 个。代码会自动等待重试；偶尔出现不用管。 |
| `invalid_client` / 刷新令牌失败 | 客户端密钥被删除或换过。用新的客户端 JSON 重新运行第二步。 |
| Claude 查邮件时用了官方 Gmail | 开新对话（旧对话缓存了旧的工具说明）；问的时候说“学校邮箱”；或在对话里关掉官方 Gmail 连接器。 |
| 工具里看不到 Gmail / 考勤 | 当前连接只有 `moodle` 权限，按第四步用配对码重新授权。 |
| 考勤同步超时 | 门户页面结构变了。先确认 `#daySel` 下拉框和 `dayPanel_<日期>` 区块还在。 |

## 六、写操作的保护

起草、发信、改标签、考勤提交都分两步：先返回完整预览和一次性确认凭据，用户确认后才执行。

- 凭据 10 分钟有效、只能用一次，绑定预览时的内容；发送草稿前会核对草稿在预览后有没有被改过。
- 执行前的检查没通过时记为“已拒绝”（确定没有发出）；执行中出错记为“结果未知”，不会自动重试，需要先去确认实际状态。
- 不提供彻底删除邮件的操作。
