# api-relay-checker

一个检查 OpenAI / Anthropic 兼容 API 端点的小工具。适用于任何 API 中转站，也适用于模型厂商的官方接口。

由 Beijing-LINYIAI 开发。我们自己运营一个 API 中转站（林易API，linyiapi.ai），这个工具同样可以用来检测我们。

**工具是中立的**：对每个端点都跑同一套检查，不内置任何站点名单或排名，不对任何站点做特殊处理。代码只有一个文件，可以自己读一遍再用。

## 它检查什么

| 检查 | 方法 | 合格标准 |
| --- | --- | --- |
| 接口是不是真的按协议实现 | 用无效 Key 请求 `/v1/messages` 与 `/v1/chat/completions` | 返回 401 / 403 和 JSON 错误；返回网页 HTML 或接受了无效 Key 都算不合格 |
| 计费数字是否合理 | 用你的 Key 发一句很短的话，读响应里的 `usage` | 输入 token 数与请求大小大致相符，输出不超过 `max_tokens` |
| 流式输出 | 用你的 Key 发一个 `stream: true` 请求 | 返回 `text/event-stream`，测出首 token 延迟；文字一次性全部到达会提示「可能被缓冲」 |

最后输出一份报告（文字或 JSON）。有任何一项不合格时退出码为 1。

这些检查说明的是端点的行为，**不能证明是哪个上游模型在回答**。计费是否准确，最终要拿 `usage` 和服务商控制台的扣费记录逐条核对。

## 安装

只需要 Python 3.8 及以上，不依赖任何第三方库。

```bash
git clone https://github.com/Beijing-LINYIAI/api-relay-checker.git
cd api-relay-checker
```

## 用法

### 1. 不需要账号：只测无效 Key

```bash
python3 relay_check.py https://linyiapi.ai
```

把地址换成任何你想检查的端点即可，`/v1` 写不写都可以：

```bash
python3 relay_check.py https://api.example.com/v1
```

只测一种协议：

```bash
python3 relay_check.py https://api.example.com --protocol openai
```

### 2. 有 Key：再测 usage 与流式

Key 建议放在环境变量里，免得留在 shell 历史中：

```bash
export RELAY_CHECK_KEY="sk-xxxx"
python3 relay_check.py https://api.example.com \
  --anthropic-model claude-sonnet-4-5 \
  --openai-model gpt-4o-mini
```

模型 ID 填该端点实际支持的。每个协议会各发一次普通请求和一次流式请求，消耗很少的额度。

### 3. JSON 报告

```bash
python3 relay_check.py https://api.example.com --json > report.json
```

报告里不包含你的 Key。

## 输出示例

```text
api-relay-checker 0.1.0
endpoint: https://api.example.com

[PASS] invalid key -> /v1/messages
       HTTP 401 with a JSON error
[PASS] invalid key -> /v1/chat/completions
       HTTP 401 with a JSON error
[SKIP] usage / stream
       no --key given; only the invalid-key checks ran

summary: PASS 2, WARN 0, FAIL 0, SKIP 1
These checks show how the endpoint behaves; they do not prove which upstream model answered.
```

## 证书错误

在 macOS 上用 python.org 安装包装的 Python 时，可能看到 `CERTIFICATE_VERIFY_FAILED`。这是因为 Python 没有找到系统根证书。处理方法任选一种：

- 运行 Python 安装目录里的 `Install Certificates.command`
- 或者临时指定证书文件：`SSL_CERT_FILE=/etc/ssl/cert.pem python3 relay_check.py ...`

## 开发

```bash
python3 -m unittest discover -s tests
```

测试在本地起一个模拟服务器，模拟正常端点、返回 HTML 的端点、接受任意 Key 的端点、虚报 usage 的端点和缓冲流式输出的端点，不访问外网。

## 许可

MIT
