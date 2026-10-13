# 贡献指南 (Contributing)

感谢关注 docflow！欢迎提 Issue、PR 或新 backend 实现。

## 提 Issue
- 转换质量问题、bug、文档建议都欢迎，请附上**可复现的最小步骤**和预期/实际输出。
- 涉及具体云端服务的计费异常，请脱敏后附上 `/usage` 与错误返回。

## 提交 PR
1. Fork 并切出特性分支（`feat/xxx` / `fix/xxx`）。
2. 本地跑通：`pip install -e ".[dev]" && PYTHONPATH=src python -m pytest -q`。
3. 保持核心抽象不被破坏：新增能力优先用插件方式接入 `backends/` 或 `exporters/`。
4. 不要提交 `.env`、`.docflow_work/`、密钥或大型二进制。

## 新增一个 Backend
**云端 API（异步/轮询类）**，继承 `backends/api/base_api.py` 的 `APIBackend`：
- 实现 `_submit_file(file_path, **opts) -> task_id` 与 `_poll(task_id, **opts) -> markdown`。
- 在 `_post()` 基础上调用你的端点；基类已处理限流、task_id 落盘与续跑。
- 在 `cli.py` 的 `_build_backends` 注册名字与构造函数。

**本地引擎（离线类）**，继承 `backends/local/base_local.py` 的 `LocalBackend`：
- 实现 `convert(src, **opts) -> ConversionResult`（自行切片或直接解析）。
- 重依赖用懒导入，避免未安装时阻断整个包导入。

有意向但尚未实装的本地引擎：**RapidOCR**（ONNX、轻量，纯文字快，表格/公式弱）、
**MinerU**（中文与 LaTeX 公式强，依赖重）、**Marker**（多格式、GPU 快，模型权重带营收阈值许可）。

## 代码规范
- 风格跟随现有代码（4 空格、类型标注、`from __future__ import annotations`）。
- 公共函数加 docstring；中文注释随意，但 README/文档以中文为准。

## 许可证
所有贡献默认以 Apache-2.0 提交（见 LICENSE）。
