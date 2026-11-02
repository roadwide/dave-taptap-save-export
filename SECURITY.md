# 敏感数据处理

不要在 Issue、Pull Request、Actions 日志或仓库提交中附带以下内容：

- 真实 HAR、Cookie、Authorization、账号 Token 或 MAC 密钥。
- 应用 Client Token、原始客户端元数据。
- 带签名的存档下载 URL、个人存档、设备及用户标识。

排查问题时可提供工具版本、Python 版本、命令的子命令名和 HTTP 状态码。输入文件请留在仓库外的本地私有目录。

若发现自己已公开凭据，应先删除公开内容，并通过相应平台撤销或更新凭据；单纯从最新提交删除文件不会清除 Git 历史。
