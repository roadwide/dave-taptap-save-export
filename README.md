# 戴夫 TapTap 云存档导出

从自己的 TapTap 账号导出《潜水员戴夫》iPhone 云存档，无需购买 TapPC 版，也无需制作 iPhone 整机备份。

Python 3.9+，仅使用标准库。工具只查询和下载云存档，不提供上传、覆盖或删除存档的功能。

## 实测范围

2026 年 11 月，在 iOS 游戏版本 1.0.49、TapSDK 4.10.2 下完成过真实导出：云存档接口返回成功，ZIP 长度与元数据一致，全部条目 CRC 校验通过，GD/PZ 存档成对齐全。未验证游戏内读档、跨平台导入或未来 SDK 版本。

仓库不包含任何真实 HAR、账号令牌、Client Token、设备或用户标识、下载链接签名、游戏安装包、程序元数据及个人存档。测试使用合成数据。

## 为什么需要两套签名

存档列表入口：

```text
GET https://tapsdk.tapapis.cn/cloud-save/v1/get-saves?client_id=<Client ID>
```

这条请求有两层认证：

| 请求头 | 使用的信息 | 作用 |
| --- | --- | --- |
| `Authorization: MAC …` | 当前账号 SDK Token 的 `kid` 和 `mac_key` | 账号认证，HMAC-SHA1 |
| `X-Tap-Sign` | 应用的 Client Token | 应用请求签名，HMAC-SHA256 |

`client_id` 和账号令牌可以从自己设备的登录 HAR 中取得；Client Token 通常是游戏客户端初始化参数。账号的 `mac_key` 不能替代它，其他路径的旧 `X-Tap-Sign` 也不能直接重用，因为签名包含请求方法、路径、请求头和正文。

本文提供的办法是：从自己合法取得的客户端中静态读取元数据，在本地用已经抓到的请求签名验证 Client Token 候选，然后重新签算存档查询请求。匹配过程不向服务器批量尝试密钥。

## 使用流程

### 1. 准备自己的登录 HAR

先在游戏内手动上传要备份的最新进度。云存档只代表最后一次上传的版本。

使用自己设备上的 HTTPS 抓包工具，例如 Stream，记录一次游戏登录和初始化。保存为本地 HAR，至少需要以下两条记录：

- `POST /auth/v1/login-by-token`：响应正文包含 `data.sdk_token.kid` 和 `data.sdk_token.mac_key`。
- `POST /sdk-core/v1/gatekeeper`：包含完整请求 URL、`X-Tap-*` 请求头、`X-Tap-Sign` 和原始请求正文。

需要先按抓包工具说明完成设备端的 HTTPS 解密设置。仅有域名或 CONNECT 记录不够。如果客户端不允许解密，本文没有提供绕过证书固定的方法。

HAR 含有可访问账号数据的凭据，请保存在本地私有目录，不要上传 GitHub、网盘或公开聊天。以下命令使用 `~/private-dave/` 举例，这个目录应放在仓库外：

```sh
mkdir -m 700 ~/private-dave
# 将自己的 session.har 和合法取得的 demo.apk 放入此目录。
chmod 600 ~/private-dave/session.har
```

### 2. 提取客户端元数据

从自己合法持有的客户端安装包中提取，整个过程不安装或运行 APK：

```sh
python3 taptap_save.py metadata \
  --apk ~/private-dave/demo.apk \
  --output ~/private-dave/global-metadata.dat
```

本次成功流程使用了公开试玩 Android 1.0.8 的元数据，与 iOS 登录 HAR 的应用签名匹配。它不意味着所有版本、所有平台的初始化参数都相同。工具目前仅支持实测过的 IL2CPP 元数据 v27，且要求客户端仍保留可读取的字符串常量。

如果有自己有权访问的 HTTPS APK 下载地址，可以将地址保存在 `~/private-dave/apk-url.txt`，只分段读取 ZIP 目录和元数据条目：

```sh
python3 taptap_save.py metadata \
  --url-file ~/private-dave/apk-url.txt \
  --output ~/private-dave/global-metadata.dat
```

服务器必须支持正确的 HTTP Range 响应。工具不会在 Range 不可用时退回整包下载，累计读取上限为 150 MB。分段读取不能证明整个 APK 的签名或完整性，工具也不附带游戏包或默认第三方下载地址。

### 3. 在本机验证并保存 Client Token

```sh
python3 taptap_save.py recover-token \
  --har ~/private-dave/session.har \
  --metadata ~/private-dave/global-metadata.dat \
  --output ~/private-dave/client-token.txt
```

工具解析元数据中的字符串常量，用 HAR 中的 gatekeeper 原始请求签名进行离线匹配。只有得到唯一匹配才会写入令牌文件；令牌值不会打印到终端。

如果 HAR 包含多个游戏，可加 `--client-id <Client ID>` 选择应用。若包含同一游戏的多个账号，请重新导出只包含目标账号的 HAR。

### 4. 下载自己的云存档

```sh
python3 taptap_save.py export \
  --har ~/private-dave/session.har \
  --client-token-file ~/private-dave/client-token.txt \
  --output ~/private-dave/export-001
```

输出目录必须不存在。每个云存档会保存为 `save-001.zip` 或 `.bin`，并生成只含文件大小、SHA-256、云端修改时间和 ZIP 校验结果的 JSON。不会输出或保存账号令牌、存档描述、完整接口响应或带签名的存储 URL。

下载 ZIP 不会自动解压，也不会写入游戏目录。请先保留原始文件，再按目标平台要求操作。ZIP 内 `.sav` 文件通常需要完整保留 GD/PZ 配对。

## 常见问题

| 现象 | 检查项 |
| --- | --- |
| 找不到唯一账号 | HAR 是否包含登录响应正文；是否混入多个游戏或账号 |
| Client Token 无唯一匹配 | 客户端是否与 HAR 属于同一应用；请求正文和头是否完整；元数据是否受保护或版本不同 |
| HTTP 400 | Client Token、系统时间、签名格式、SDK 兼容性；账号 MAC 密钥不等于 Client Token |
| HTTP 401/403 | 登录凭据是否过期、是否对应目标账号，以及云存档和存储 URL 是否仍有效 |
| 没有云存档 | 游戏中是否手动上传过；登录的是否为正确 TapTap 账号 |

工具不会读取浏览器 Cookie、钥匙串或其他账号，也不会搜索本机其他文件。所有输入文件都必须显式提供。出现错误时，不打印可能含凭据的原始异常或完整 URL。

`.gitignore` 只是辅助保护；上传自己的改动前仍应检查 `git diff --cached`。请勿在 Issue 中粘贴 HAR、令牌、原始元数据或存档下载地址。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
```

测试不联网，覆盖 HAR 选择、签名规范、离线令牌匹配、元数据边界、文件防覆盖、存储来源校验及导出信息脱敏。真实接口验证与测试结果应分开理解：服务端或 SDK 更新后，历史实测不保证仍可使用。

## 参考资料

- [TapTap 官方云存档指南](https://developer.taptap.cn/docs/sdk/tap-cloudsave/development-guide/unity/)
- [官方 SDK 4.10.2 应用签名实现](https://github.com/taptap/tapsdk-unity-dist/blob/4.10.2/Core/Standalone/Runtime/Internal/Http/TapHttpSign.cs)
- [官方 SDK 4.10.2 账号 MAC 实现](https://github.com/taptap/tapsdk-unity-dist/blob/4.10.2/Login/Standalone/Runtime/Internal2/LoginService.cs)
- [TapTap 官方账户认证说明](https://developer.taptap.cn/docs/sdk/taptap-login/taptap-oauth/)

这是独立的社区工具，与 TapTap、心动及游戏开发商没有隶属关系。仅对本项目原创代码和文档授予 MIT 许可；游戏文件与第三方 SDK 的权利仍归各自权利人。
