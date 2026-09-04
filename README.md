# Grok 注册机面板 - Windows 终极保姆级图文教程

> **写在前面**：
> 这个项目最初是为 Linux 系统（Ubuntu 服务器等）设计的，由于底层依赖 `/proc` 和 Linux 管道通信机制，原本在 Windows 上直接运行会频繁报出 `[WinError 3]` 或 `10093` 的错误。
> **但是请放心！** 本仓库已经对核心底层代码进行了 Windows 专属适配修改，您现在**不需要**安装 WSL2 虚拟机，也**不需要**去复制改名 `.venv/bin/python`，直接跟着下面的步骤，就能在纯原生 Windows 电脑上完美流畅运行！

---

## 目录
1. [第一部分：在 Windows 电脑上启动注册机（核心必看）](#第一部分在-windows-电脑上启动注册机核心必看)
2. [第二部分：为什么要自建 Cloudflare 邮箱？（原理必读）](#第二部分为什么要自建-cloudflare-邮箱原理必读)
3. [第三部分：手把手教你部署 Cloudflare 专属邮局](#第三部分手把手教你部署-cloudflare-专属邮局)
4. [第四部分：解决在 Cloudflare 中“找不到 Worker”的常见大坑](#第四部分解决在-cloudflare-中找不到-worker的常见大坑)

---

## 第一部分：在 Windows 电脑上启动注册机（核心必看）

这部分教你如何把注册机的“图形化网页控制台”在电脑上跑起来。

### 第 1 步：配置专属 Python 环境
1. 按键盘 `Win + R`，输入 `cmd` 并回车，打开黑色的命令提示符窗口。
2. 使用 `cd` 命令进入到你下载的这个项目的根目录，比如：
   ```cmd
   cd C:\你的路径\grok-register-panel
   ```
3. 在此文件夹内，创建一个 Python 专属虚拟环境（**这一步只需要做一次**）：
   ```cmd
   python -m venv .venv
   ```
4. **激活虚拟环境**（关键！运行后，命令行的最左边会出现 `(.venv)` 这个标志，代表你进去了）：
   ```cmd
   .venv\Scripts\activate
   ```
5. **安装依赖和浏览器内核**：
   ```cmd
   pip install -r requirements.txt
   python -m camoufox fetch
   ```

### 第 2 步：启动控制台面板
当你环境装好后，每次想启动面板，只需在这个文件夹下的 cmd 里执行：

1. **设置面板的安全密码**（这是为了防止你的面板在公网被别人扫到偷用，密码自己随便编）：
   ```cmd
   set MONITOR_TOKEN=admin123
   ```
2. **启动面板服务**：
   ```cmd
   .venv\Scripts\python.exe webui/monitor.py
   ```
   *(注意：请务必使用带 `.venv\Scripts` 前缀的命令，这样才能确保调用的是刚装好依赖的虚拟环境！)*

3. 启动成功后，直接在浏览器打开：[http://127.0.0.1:8787](http://127.0.0.1:8787)。输入刚才设的密码 `admin123` 即可登录。

### 可选：使用 MailPoolHub 聚合邮箱

MailPoolHub 必须作为独立服务运行，注册面板只连接它，不会自动启动或停止它。
先在 MailPoolHub 管理后台创建客户端 API Key，然后在本面板“邮箱服务”中选择
`MailPoolHub`，填写：

- API Base：默认 `http://127.0.0.1:8080/api/v1`
- API Key：MailPoolHub 管理后台生成的 `mph_live_...`
- 内部渠道：默认留空，由 MailPoolHub 自动选择健康渠道；需要固定时可填 `mailgw`

保存前可点击“测试当前提供商”。测试会读取 `/providers`，确认鉴权和健康渠道，
不会创建邮箱。注册时 MailPoolHub API 始终直连，不会经过注册代理；创建的邮箱在
收到验证码或等待超时后自动删除，提交邮箱前失败的实例由 900 秒 TTL 回收。

### 可选：注册成功后同步到 Go 版 Grok2API

控制台提供“Grok2API 云端同步”配置卡。开启后填写服务根地址、管理员用户名和密码，
保存前可测试登录鉴权。账号完成本地保存后，面板会复用管理员会话，在后台调用
`POST /api/admin/v1/accounts/web/import`，以 Multipart 文件把邮箱和原始 SSO 导入为
Grok Web 账号。同步异常只写入日志，不会把已注册账号计为失败；不会继续转换 Build
或同步 Console。该功能与 `cpa_auto_add`、本地 `grok2api_auth_dir` 互相独立。

### 配置 Resin UUID 代理模板

在 Web 面板“外部代理池”或桌面界面中，将代理来源切换为 `Resin UUID 模板`，
输入完整代理地址，例如：

```text
http://temp.{uuid}:proxy-token@127.0.0.1:9200
```

模板必须且只能在用户名中包含一个 `{uuid}`。程序会为每个新账号或代理重试生成
不同 UUID，同一账号的注册、Turnstile、SSO 和 OAuth 全程复用同一个具体出口。
保存后的模板只显示脱敏端点；测试操作使用临时 UUID，不会把实际 UUID 写入代理池。

代理来源支持“直连 / 静态代理池 / Resin UUID”显式切换。切换不会删除其它来源，
但运行时只会使用当前选中的来源。旧版本 `config.proxy` 中的 Resin 模板会自动识别，
首次在面板保存后转为显式模式。
Web 面板会以明文回显当前保存的 Resin 模板，刷新页面后无需重新输入。

---

## 第二部分：为什么要自建 Cloudflare 邮箱？（原理必读）

在面板的“邮箱服务”里，你可以选择用公共的免费邮箱（如 DuckMail 等）。但是，**免费的公共邮箱极容易被 xAI 官方风控拦截**。

如果你想达到**一天注册 250 个号以上的超高成功率**，强烈建议你花十几块钱买个自己的域名（比如 `xxx.asia`），然后托管在 Cloudflare 上，搭建属于你的“专属临时邮局”。

搭建它，本质上你需要三样东西：
1. **接收邮件机**：用来接外面发进来的验证码（对应 Cloudflare 的 Email Routing）。
2. **存邮件数据库**：验证码得存下来等程序去取（对应 Cloudflare 的 D1 数据库）。
3. **对外开放 API**：让本地的注册机能连上来拿验证码（对应 Cloudflare 的 Worker 接口）。

开源项目 `cloudflare_temp_email` 就是把这三样东西打包写好的一套代码。我们需要把它上传部署到你的 Cloudflare 账号里。下面教你具体怎么做。

---

## 第三部分：手把手教你部署 Cloudflare 专属邮局

### 第 1 步：安装前端环境（Node.js）
Cloudflare 的官方上传工具 `wrangler` 需要 Node.js 环境。
- 去 [Node.js 中文官网](https://nodejs.org/zh-cn) 下载 Windows 的 LTS 版（.msi文件）。
- 一路点击“下一步”安装完成，保持全部默认即可。
- **装完后，请务必关掉之前所有的 cmd 窗口，重新开一个新的 cmd。**

### 第 2 步：安装上传工具并登录 Cloudflare
在新开的 cmd 窗口里运行：
```cmd
npm install -g wrangler pnpm
```
*(如果国内网络卡住下载很慢，可以先换源：`npm config set registry https://registry.npmmirror.com`)*

安装完后，执行登录命令：
```cmd
wrangler login
```
这会自动弹出一个网页，**请确保网页里登录的，是你托管了域名的那个 Cloudflare 账号！** 然后点击「Allow/允许」。回到 cmd 看到 `Successfully logged in` 即可。

### 第 3 步：下载源码与创建 D1 数据库
去 GitHub 下载 `cloudflare_temp_email` 开源项目的代码解压。
1. 在 cmd 里，`cd` 进入它里面的 `worker` 文件夹（比如 `cd C:\Desktop\cloudflare_temp_email-main\worker`）。
2. 运行命令，创建一个用来存验证码的数据库：
   ```cmd
   wrangler d1 create temp-email-db
   ```
3. 运行成功后，屏幕上会有一段类似于这样的输出：
   ```toml
   database_name = "temp-email-db"
   database_id = "ebec7735-a491-4ac9-ae86-xxxxxxxxxx"
   ```
   **务必把 `database_id` 后面那串长长的乱码复制保存下来！**

### 第 4 步：修改配置文件（最容易填错的一步）
在 `worker` 文件夹里，找到 `wrangler.toml.template` 这个文件，复制一份并改名为 `wrangler.toml`。
用记事本打开 `wrangler.toml`，修改以下几处：

1. **数据库 ID**：把最后面 `database_id = ""` 里填入你刚才复制的那串乱码。（注意：`binding` 必须保持为 `"DB"`，千万别改成系统提示的 `"temp_email_db"`）。
2. **DOMAINS**：改成你自己的域名，例如 `DOMAINS = ["yourdomain.com"]`。
3. **DEFAULT_DOMAINS**：同上，改成 `DEFAULT_DOMAINS = ["yourdomain.com"]`。
4. **JWT_SECRET**：随便脸滚键盘打一长串密码，例如 `JWT_SECRET = "a3f7c2e9b4d18f6a0c5e2b7d"`。**这段密码等下要在注册机面板里填！**

### 第 5 步：上传部署！
确认配置无误后，继续在 cmd 的 `worker` 文件夹下执行：
```cmd
pnpm install
pnpm run deploy
```
看到绿色的 `Deployed cloudflare_temp_email triggers`，说明你的 Worker 代码已经成功长在 Cloudflare 上了！

---

## 第四部分：解决在 Cloudflare 中“找不到 Worker”的常见大坑

很多人卡在设置邮件路由（Email Routing）时，在下拉框里找不到自己刚刚部署的 Worker。**这是因为在 Cloudflare 的机制里，如果你的代码没有先部署好“接收邮件”的逻辑，系统就不会承认它是收信机。**

现在你已经通过上面的步骤把代码 `deploy` 部署上去了，接下来去 Cloudflare 网页后台操作，绝对顺畅：

### 1. 配置邮件路由（Catch-all）
1. 登录 Cloudflare 后台首页，点击你的域名（如 `yourdomain.com`）。
2. 左侧菜单点击 **Email (电子邮件) -> Email Routing (电子邮件路由)**。
3. 如果是首次进，点自动添加 DNS 记录。
4. 找到 **Routing rules (路由规则)** 下的 **Catch-all address (全域地址)**。
5. 点击编辑，**Action (操作)** 选择 **Send to a Worker (发送给 Worker)**。
6. 这时你在下拉菜单里，就能清楚地看到名为 `cloudflare_temp_email` 的 Worker 了！选中并保存。
7. **巨坑提醒**：保存后，**请一定要把这行规则最右侧的灰色 Disabled 开关，点击拨到蓝色的 Enabled（已启用）状态！** 否则无法收信！

### 2. 为你的 Worker 绑定直连域名（解决国内连不上）
默认部署出来的网址是 `*.workers.dev`，由于特殊的网络原因，国内直接用面板连过去会报错连接失败。我们需要给它绑定一个你自己的子域名。

1. 在 Cloudflare 后台最左侧大菜单，点击 **Workers & Pages (工人与页面)**。
2. 找到你的 Worker 项目（通常就叫 `cloudflare_temp_email`，如果你之前手贱点过新建，它可能叫类似 `raspy-mountain-0281` 这样的随机名字，看哪个最近更新就点哪个）。
3. 点进去后，顶部菜单点击 **Settings (设置)**，然后左侧点击 **Triggers (触发条件)**。
4. 往下滚动找到 **Custom Domains (自定义域)**，点击 **Add Custom Domain (添加自定义域)**。
5. 输入前缀，比如 `mail`，这样它会拼接成 `mail.yourdomain.com`。
6. 点击 Add domain 保存。Cloudflare 会自动帮你做好 DNS 解析。以后这就是你国内直连的专属 API 接口了。

### 3. 最后一步：去面板填写！
回到你的电脑，打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)。
- 切换到 **“邮箱服务”** 标签页。
- 提供商选择：`Cloudflare`。
- API Base 填入：`https://mail.yourdomain.com`（一定要带 https:// 前缀）。
- API Key：填入你刚才在配置文件里写的那个 `JWT_SECRET`（如 `a3f7c2e...`）。
- 固定收信域名：填主域名 `yourdomain.com`。
- 点击 **[保存配置]**，然后再点击 **[测试当前提供商]**。

如果左下角弹出绿色的 `Cloudflare 可达 HTTP 200`，恭喜你，最难配置的专属域名邮箱收信服务已经彻底通关了！你可以去“任务控制”里开启并发注册了！
感谢 [LINUX DO](https://linux.do) 社区的支持。
