# Lightroom Web 自动化：上传图片 → 公开共享 → 修改照片 Title

纯 Python 标准库实现，用浏览器里已经登录的 cookies 直接调用 Lightroom Web 的接口。

## 用法

```bash
# 上传 + 创建公开共享链接 + 修改照片自身的 Title
python3 lightroom_auto.py --image "/path/to/photo.jpg" --title "黄昏落日2"

# 只改已有照片的 Title（不上传、不共享）
python3 lightroom_auto.py --asset-id 9d26e55495601fd510186ccf2a378121 --title "黄昏落日2"

# 同时把共享页标题（共享相册名称）也改掉
python3 lightroom_auto.py --image photo.jpg --title "黄昏落日2" --share-title "我的共享相册"

# 只上传并改 Title，不创建共享
python3 lightroom_auto.py --image photo.jpg --title "黄昏落日2" --no-share

# 只查询当前账号的存储配额
python3 lightroom_auto.py --check-quota
```

常用可选参数：

```bash
--cookies captures/cookies-adobe.json   # 默认值
--catalog <catalogId>   # 不填会自动获取
```

## 参数含义

| 参数 | 含义 |
| --- | --- |
| `--title` | **照片自身的 Title**，写入 `payload.xmp.dc.title` |
| `--share-title` | 共享页标题（共享相册名称），不填则保留默认日期名 |
| `--asset-id` | 对已有照片操作，只改 Title |
| `--no-share` | 只上传 + 改 Title，不创建公开共享 |
| `--check-quota` | 只查询存储配额后退出 |
| `--account-id` | 指定账号 id（配额检查用；不填则从已有照片自动获取） |
| `--skip-quota-check` | 上传前不检查剩余存储 |

成功后会输出：

```
asset id  : ...
album id  : ...
space id  : ...
共享链接  : https://lightroom.adobe.com/shares/<spaceId>
照片 Title: 黄昏落日2
```

## GUI 界面（Tkinter，单文件版 main.py）

macOS / Linux：

```bash
cd /path/to/auto_lightroom
python3 -m venv venv
source venv/bin/activate
python main.py
```

Windows 10（macOS/Linux 的 venv 不能直接复制到 Windows，需要新建一个）：

```bat
cd /d <项目目录>
py -3 -m venv venv
venv\Scripts\activate
python main.py
```

- 路径选择使用 Windows 原生目录对话框，支持盘符、反斜杠、中文目录
- Python 安装时需包含 tkinter（python.org 官方安装包默认包含）
- 状态文件在 `C:\Users\<用户名>\.lightroom_auto_gui\state.json`

> `main.py` 是单文件 GUI 版，包含 Lightroom API 客户端和界面。
> `lightroom_auto.py` 是 CLI 版；`lightroom_gui.py` 是旧版拆分 GUI，已被 `main.py` 取代，可删除。

界面功能：

- 选择图片目录、标题目录、结果输出目录
- 选择标题目录后列出所有 `.txt`；按**勾选顺序**编号，取消再勾选可调整顺序
- 每次从每个选中的 txt 中随机取一行标题，按勾选顺序**直接拼接**（不加分隔符）
- 生成的标题会同时写入：
  - 照片自身的 Title（XMP `dc:title`）
  - 共享相册名称（共享页面浏览器标签标题，显示为 `标题 by 作者`）
- Cookie 输入框：固定一个账号，粘贴 Cookie 字符串或一个 JSON cookie dump
- 上传前按浏览器同款规则检查重复：
  - **同一内容 sha256 且同一文件名**才算重复 → 计入「重复跳过」
  - 内容相同但文件名不同 → 正常上传（与浏览器行为一致）
  - 服务端返回 **HTTP 412** 仍作为兜底按重复处理
  - 设置标题失败 → 换一个随机标题重试（照片 Title 最多 50 次，共享页标题最多 3 次）
  - 配额不足 → **停止全部流程**（不再继续后面的图片）
- 勾选「成功后删除源图片」：上传 + 共享 + 改标题全部成功后删除本地图片
- 输出到结果目录：
  - `结果链接.txt`：每条共享链接一行，创建成功后立即写入
  - `results.csv`：成功记录（含最终标题、asset/album/space id、共享链接）
  - `run.log`：完整运行日志
- 设置自动保存到 `~/.lightroom_auto_gui/state.json`（含 cookies；macOS/Linux 权限 600，Windows 位于当前用户目录）
- 「停止」会在当前图片处理完后生效

注意：

- 实测 Lightroom 接口**不会因为 Title 重复而报错**，同一个 Title 可以设置到多张照片上。
  因此「换标题重试」只会在接口真正报错时触发。如果需要在单次运行内保证标题不重复，
  需要额外加一层内存去重（当前按“去重逻辑去掉”的要求未加）。
- 共享页面浏览器标签显示的是**共享相册名称**，不是照片的 XMP Title；GUI 现在会把两者
  都设置为最终生成的标题。
- 旧的 `~/.lightroom_auto_gui/history.json` 已不再使用，可以删除
- 单文件上传限制与 CLI 相同（≤200 MiB 直传）

## 存储配额检查

每次上传前脚本会：

1. 通过 `GET /v2/accounts/{accountId}` 读取 `entitlement.storage`
2. 打印已用 / 上限 / 剩余空间
3. 如果文件大小超过剩余空间，直接报错退出，不发起上传

示例：

```bash
$ python3 lightroom_auto.py --check-quota
catalog id: <catalogId>
account id: <accountId>
[配额] 已用 134.35 KiB / 5.00 GiB（0.003%），剩余 5.00 GiB
[配额] 预警线 4.00 GiB
```

- 免费账号上限 5 GiB，达到 4 GiB 时提示预警线。
- 账号 id 默认从图库已有照片的 `payload.importSource.importedBy` 自动获取；
  图库为空或操作共享图库时用 `--account-id` 指定。
- 配额查询失败（网络/权限）时只打印警告并继续上传；真正超限时服务端仍会拒绝。
- 删除的照片会在 Deleted 中保留 30 天并继续占用配额，永久删除后才释放。
- 不想检查时加 `--skip-quota-check`。
- GUI 中每次上传前都会重新读取配额；**配额不足会直接停止整轮运行**。

## 依赖

- Python 3.9+，只用标准库（`urllib`、`http.cookiejar`），无需 pip 安装。
- 有效的 lightroom.adobe.com 登录 cookies。

## Cookies 获取方式

GUI 的 Cookie 输入框可以直接粘贴：

- 浏览器请求头里的整串 Cookie（推荐）
- 一个 JSON cookie dump（支持下面三种格式）

CLI 则用 `--cookies captures/cookies-adobe.json` 指定文件。

支持的 JSON 格式：

1. CDP / Cookie-Editor 导出的 `{"cookies": [{"name": ..., "value": ..., "domain": ...}]}`
2. 数组 `[{"name": ..., "value": ..., "domain": ...}]`
3. 简单键值对 `{"cookie_name": "cookie_value"}`（默认按 `.adobe.com` 域发送）

### 方式一：DevTools 手动复制（最简单）

1. 在已登录的 Chrome 里打开 `https://lightroom.adobe.com/`
2. `Cmd + Option + I` 打开 DevTools，切到 **Network**
3. 刷新页面，或随便点一下图库，找到一条发往 `lightroom.adobe.com` 的请求（路径一般是 `/v2/...`）
4. 点开该请求 → **Headers** → **Request Headers** → 找到 `cookie:`，复制冒号后面的完整字符串
5. 粘贴到 GUI 的 Cookie 输入框（或保存为文本给 CLI 用）

> 不要用 Console 里的 `document.cookie`：`lrw_token`、`session` 等关键 Cookie 是 HttpOnly，
> `document.cookie` 取不到，会导致登录失败。

如果 Request Headers 里看不到完整 Cookie，可以右键该请求 → **Copy → Copy as cURL**，
里面会有 `-H 'cookie: ...'`，复制后半段即可。

### 方式二：用项目里的 dump_cookies.mjs

Chrome 需要以调试端口启动（普通方式启动的 Chrome 无法被注入）：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-profile" \
  https://lightroom.adobe.com/
```

Windows 下 Chrome 路径一般为：

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%CD%\.chrome-profile" ^
  https://lightroom.adobe.com/
```

在那个窗口登录后执行：

```bash
node dump_cookies.mjs 9222 captures/cookies-adobe.json
```

把生成的 JSON 内容粘贴到 GUI，或让 CLI 通过 `--cookies` 读取。

### 方式三：浏览器插件

用 Cookie-Editor 等插件导出 JSON，格式选上面第 1 种或第 2 种，直接粘贴到 GUI 即可。

Cookie 就是登录凭证，不要发给别人或提交到仓库。

## 实测接口流程

所有请求都发往 `https://lightroom.adobe.com`，认证只依赖 cookies，另外需要
`x-requested-with: XMLHttpRequest`。

| 步骤 | 请求 |
| --- | --- |
| 获取 catalog | `GET /v2/catalogs?role=any&subtype=lightroom%3BX-lightroom`（含 `x-all-account-states: true`） |
| 查询配额 | `GET /v2/accounts/{accountId}` → `entitlement.storage.used / display_limit` |
| 上传 | `POST /v2/catalogs/{catalogId}/assets`，`multipart/form-data`：`revision`（JSON 元数据）+ `master`（文件） |
| 等待处理 | `GET /v2/catalogs/{catalogId}/assets/{assetId}` 轮询 |
| 创建共享相册 | `PUT /v2/catalogs/{catalogId}/albums/{albumId}` |
| 共享页样式 | `POST /v2/catalogs/{catalogId}/albums/{albumId}` |
| 加入照片 | `PUT /v2/catalogs/{catalogId}/albums/{albumId}/assets` |
| 创建公开空间 | `PUT /v2/catalogs/{catalogId}/spaces/{spaceId}`（`private: false`） |
| 取共享链接 | `GET /v2/catalogs/{catalogId}/spaces/{spaceId}?embed=...` → `links./rels/web_space` |
| **改照片 Title** | `PUT /v2/catalogs/{catalogId}/assets/{assetId}/revisions/{latestRevisionId}/children/{childRevisionId}`，body 为 `{"payload":{"xmp":{"dc":{"title":"..."}}, "userUpdated":"..."}}` |
| 改共享页标题（可选） | `POST /v2/catalogs/{catalogId}/albums/{albumId}`，`{"payload":{"name":"..."}}` |

改 Title 的本质是在最新 revision 下创建一个 child revision，并合并已有的
`xmp.dc` 对象（不会覆盖 description、subject 等其他字段）。`albumId` 和
`spaceId` 由客户端生成（32 位 hex），和网页端行为一致。

## 注意事项

- `--title` 修改的是**照片自身的 Title 元数据**；`--share-title` 才是共享页标题
  （浏览器标签页上显示的 `标题 by 作者`）。
- 上传响应里的 `id` 是 revision id；asset id 要从 `Location` 头里取
  （`.../assets/{assetId}/revisions/{revisionId}`）。脚本已经处理。
- 小于 200MB 的文件走本文档的 multipart 直传；更大的文件网页端会改用 chunked upload，
  本脚本暂未实现。
- `captures/` 里的抓包日志和 cookies 都包含登录凭证，**不要提交到 git 或分享**。
