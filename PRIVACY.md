# Privacy policy

Last updated: 31 October 2026

**English** · [中文摘要](#中文摘要)

Monash Study Kit (also used under the name "Monash Student MCP") is open-source, self-hosted software. It is not a hosted service: each person runs it on their own computer or their own server, with their own accounts and credentials. The maintainer does not operate a shared server for other users and does not receive anyone's data.

This policy explains what the software accesses, where that data goes, and how to remove it.

## What the software accesses

Depending on which parts you set up, the software signs in as you and reads:

- **Moodle** (learning.monash.edu): courses, files, assignments, grades, feedback, announcements, messages and calendar events.
- **Ed** (edstem.org): posts, replies, lessons and lesson progress.
- **Gmail** (optional): if you connect a Google account, messages, threads, labels and attachments, through the Gmail API.
- **Attendance** (optional): your attendance records on the university attendance portal.

Gmail access is requested only when you choose to connect it. The default permission is read-only (`gmail.readonly`). The broader permission (`gmail.modify`) is requested only if you opt in to mailbox actions such as creating drafts, sending a draft or changing labels. Those actions run only after you review and confirm the specific change. The software never permanently deletes email.

## How the data is used

Data is used only to provide the features you use: syncing a private local copy, full-text search, to-do and deadline summaries, and answering questions you ask through an AI app connected to your own instance.

The software does not use your data for advertising, does not sell it, and does not use it to build profiles or train machine-learning models.

## Where the data is stored

- Synced data (course files, posts, email bodies and attachments) is stored on the machine that runs your instance: your own computer, or a server you control.
- Sign-in state (Moodle session, Ed token, Google OAuth tokens) is stored on that same machine in a private, owner-only directory. Passwords are never stored; Google sign-in happens in your own browser.
- Nothing is sent to the maintainer or to any analytics or telemetry service.

## Sharing

The software sends data to:

- the services you connected (Moodle, Ed, Google, the attendance portal), to read your data and, only after you confirm, to carry out an action you asked for;
- the AI app you connect to your instance (for example Claude or Codex), and only the results of the queries that app makes on your behalf. That app's own privacy policy applies to what it receives.

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
- Gmail 为可选功能，默认只读；邮箱操作（草稿、发信、标签）需要额外授权，并在你确认具体内容后才执行，永不彻底删除邮件。
- 数据只发给你连接的服务（Moodle、Ed、Google、考勤门户）和你接入的 AI 应用（仅限它代你查询的结果）；不用于广告、不出售、不用于训练模型。
- 使用 Google API 获取的数据遵守 Google API Services User Data Policy 及其 Limited Use 要求。
- 可随时在 <https://myaccount.google.com/permissions> 撤销 Google 授权，删除数据目录即可清除全部本地数据。
