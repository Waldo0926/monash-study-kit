# Privacy policy

Last updated: 3 November 2026

**English** · [中文摘要](#中文摘要)

Monash Study Kit (also used under the name "Monash Student MCP") is open-source, self-hosted software. It is not a hosted service: each person runs it on their own computer or their own server, with their own accounts and credentials. The maintainer does not operate a shared server for other users and does not receive anyone's data.

This policy explains what the software accesses, where that data goes, and how to remove it.

## What the software accesses

Depending on which parts you set up, the software signs in as you and reads:

- **Moodle** (learning.monash.edu): courses, files, assignments, grades, feedback, announcements, messages and calendar events.
- **Ed** (edstem.org): posts, replies, lessons and lesson progress.
- **Google Drive** (optional, separate from Gmail): course-linked Google Docs, Slides, Sheets and Drive files that your account can access, exported to your own machine.
- **Gmail** (optional): if you connect a Google account, messages, threads, labels and attachments, through the Gmail API.
- **Attendance** (optional): your attendance records on the university attendance portal.

Gmail access is requested only when you choose to connect it. The package includes a Google OAuth client (a "desktop app" registered in the maintainer's Google Cloud project) so you can sign in without creating your own. It only tells Google which app is asking; your mail goes from Google straight to your own computer and never passes through the maintainer, who cannot see your tokens or messages. You can use your own client instead with `--client`. By default the software asks for `gmail.modify`, which lets it read mail and, when you ask for it, create drafts, send a draft you reviewed or change labels (archive, mark as read, star). It cannot permanently delete email. If you only want reading, log in with `--read-only` and it asks for `gmail.readonly` instead. Every mailbox action and every attendance check-in runs only after you review the full preview and confirm that specific action.

## How the data is used

### Google course files and materials-only profiles

`monash google login` requests only `https://www.googleapis.com/auth/drive.readonly`, using a separate `google-drive-token.json` from Gmail's `gmail-token.json`. This scope permits reading all Drive files accessible to the account; the course exporter uses it for files linked from the selected courses. It cannot edit Drive files and does not grant Gmail access. Docs and Slides are exported as PDF (Docs can instead use DOCX); Sheets as XLSX; other Drive files retain their original format. If an API export is blocked or too large, the exporter may try the dedicated login browser's Google session; those cookies stay in memory. An unavailable file remains a link with its failure reason in the export index. `monash google logout` revokes the authorization before deleting the local token; if revocation fails, the token is kept so you can retry.

Named `--profile NAME` accounts isolate credentials, databases, materials and browser sessions and always use materials-only mode. This mode skips Gmail, attendance and Ed discussions; named profiles cannot be served remotely or configured into an AI app through `monash connect`. A whole-course export contains teaching materials, including assignment instructions and visible marking guides, but excludes submissions, personal grades, feedback, mail, messages and attendance. Exported files are ordinary local files: review them before sharing.

Data is used only to provide the features you use: syncing a private local copy, full-text search, to-do and deadline summaries, and answering questions you ask through an AI app connected to your own instance.

The software does not use your data for advertising, does not sell it, and does not use it to build profiles or train machine-learning models.

## Where the data is stored

- Synced data (course files, posts, email bodies and attachments) is stored on the machine that runs your instance: your own computer, or a server you control.
- Sign-in state (Moodle session, Ed token, Google OAuth tokens) is stored on that same machine in a private, owner-only directory. Passwords are never stored; Google sign-in happens in your own browser.
- Nothing is sent to the maintainer or to any analytics or telemetry service.

## Sharing

The software sends data to:

- the services you connected (Moodle, Ed, Google, the attendance portal), to read your data and, only after you confirm, to carry out an action you asked for;
- the AI app you connect to your instance (for example Claude or Codex), and only the results of the queries that app makes on your behalf. That app's own privacy policy applies to what it receives. With the optional remote mode, an app receives mail or attendance results only if you ticked that feature when pairing it.

No other party receives your data.

## Google user data

Use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements. Specifically, Google user data is used only to provide or improve the user-facing features described above; it is not transferred to others except as described in "Sharing", as needed to provide those features, to comply with law, or with your explicit consent; it is not used for advertising; and humans do not read it unless you ask for help with a specific message, it is needed for security or to comply with law, or the data is aggregated and anonymised for internal operations.

## Keeping and deleting your data

Data stays until you delete it. You can at any time:

- revoke Google access at <https://myaccount.google.com/permissions>;
- delete the local data and credentials by removing the instance's data directory (`MONASH_KIT_HOME`, or `MONASH_KIT_DATA` and `MONASH_KIT_SECRETS` if you set them separately);
- uninstall the software.

Copies already downloaded (for example an attachment) remain on your machine until you delete them, even if the original is later deleted at the source.

## Security

Credentials and private data are kept in owner-only files and directories, are never written to logs, and are not placed in the course-file web directory. Remote access to a self-hosted server requires OAuth sign-in. Details are in [docs/security.md](docs/security.md). To report a problem, see [SECURITY.md](SECURITY.md).

## Changes and contact

Changes to this policy are published in this file, with the history visible in the repository. Questions can be raised through [GitHub issues](https://github.com/Waldo0926/monash-study-kit/issues).

## 中文摘要

- 这是开源、自托管的软件：每个人在自己的电脑或自己的服务器上运行，维护者不运营共享服务，也收不到任何人的数据。
- 数据（课件、帖子、邮件正文与附件）和登录凭据只保存在运行实例的那台机器上，凭据存放在仅本人可读的目录，不保存密码。
- Gmail 为可选功能；普通 `monash gmail login` 默认申请 `gmail.modify`，可以读邮件、创建草稿、发送已审阅的草稿和修改标签。只需要读取时用 `monash gmail login --read-only`，申请 `gmail.readonly`。邮箱操作在你确认具体内容后才执行，软件不提供彻底删除邮件的操作。
- Google 课程文件另用 `monash google login` 授权，独立保存令牌，只申请 `https://www.googleapis.com/auth/drive.readonly`；该权限允许读取账号有权访问的全部 Drive 文件，课程导出器实际处理所选课程中链接的文件，不能修改文件，也不授予 Gmail 权限。Docs、Slides 默认导出 PDF（Docs 可改 DOCX），Sheets 导出 XLSX，其他文件保留原格式。API 受限或文件过大时会尝试专用登录浏览器的 Google 会话，Cookie 只在内存中；失败项在索引中保留链接和原因。`monash google logout` 先吊销授权再删除本机令牌，失败则保留令牌供重试。
- 命名 `--profile NAME` 隔离凭据、数据库、课件和浏览器会话，强制只导课程资料，跳过 Gmail、考勤和 Ed 讨论区；不能远程服务或用 `monash connect` 配置进 AI 软件。整课导出包含作业说明和可见评分标准，但排除个人提交、成绩、反馈、邮件、私信和考勤；导出是普通本地文件，分享前请检查内容。
- 数据只发给你连接的服务（Moodle、Ed、Google、考勤门户）和你接入的 AI 应用（仅限它代你查询的结果）；不用于广告、不出售、不用于训练模型。
- 使用 Google API 获取的数据遵守 Google API Services User Data Policy 及其 Limited Use 要求。
- 可随时在 <https://myaccount.google.com/permissions> 撤销 Google 授权，删除数据目录即可清除全部本地数据。
