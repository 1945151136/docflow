# docflow

> 可插拔的文档转换工具：多云端文档智能 API + 本地 OCR 引擎，外加 Markdown → Word 导出。
> 把扫描件 / PDF / 图片一键转成干净的 Markdown 和 `.docx`。

![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)
![tests](https://img.shields.io/badge/tests-124_passing-brightgreen)
![backends](https://img.shields.io/badge/backends-paddleocr%20%7C%20somark%20%7C%20mistral%20%7C%20firecrawl-informational)

```
PDF / 图片 ──▶ [ SoMark(云端) | PaddleOCR(本地,默认) | Mistral(云端) | Firecrawl* ] ──▶ Markdown ──▶ Word(.docx)
                                                                                    （* TODO 骨架）
```

---

## 为什么会有这个项目

把一本 500 页的扫描教材变成能编辑的 Markdown，踩过的坑很具体：

- 云端 API 按页计费，**跑到一半断了，钱花了、结果没了**；
- 大文件超过单次上传限制，得手动切、手动拼，还得自己清理切片注释；
- API Key 一不留神就写进了代码里；
- Markdown 里的 `<` `&` 直接塞进 Word，文档直接报错打不开。

docflow 把这些做成一次封装：**换后端只改一个参数**，计费安全、断点续跑、切片合并、XML 转义全部内置。

---

## 特性

- **多后端、统一接口** —— `convert()` 一个方法，换后端只改 `--backend`。
- **默认本地、隐私优先** —— 默认走本地 PaddleOCR（Apache-2.0、CPU 可跑），不联网、不消耗额度；云端 API 按需 opt-in。
- **断点续跑、不重复计费** —— 异步任务把 `task_id` 落盘到 `.taskid.json`，重跑同一文件**只轮询、不重新提交**。
- **大文件无忧** —— 超 `chunk_pages`（默认 150 页）或超 `max_bytes`（默认 200MB）的 PDF 自动切片，跑完自动合并。
- **Key 永不进源码** —— 只从 `.env` / 环境变量读取，且 `.env` 已在 `.gitignore` 里。
- **转 Word 全程 XML 安全** —— 特殊字符转义，远程图片自动下载内嵌。
- **有 GUI** —— `docflow-gui`，转换前会先查云端余额，不够会拦下来问你。

---

## 技术路线

### 流水线

```
                 ┌──────────────────────────────────────────────────────┐
  PDF ──────────▶│ split_pdf()  按页数/字节双阈值切片，确定性命名为        │
                 │              part01_p1-150.pdf, part02_p151-300.pdf  │
                 └───────────────────────┬──────────────────────────────┘
                                         ▼
                 ┌──────────────────────────────────────────────────────┐
                 │ Backend.convert()   ← 唯一的抽象边界                  │
                 │   ├─ APIBackend  异步：submit → poll（云端，要计费）   │
                 │   └─ LocalBackend 同步：本地引擎直接出结果            │
                 └───────────────────────┬──────────────────────────────┘
                                         ▼
                 ┌──────────────────────────────────────────────────────┐
                 │ merge_results()  合并分片、清理切片注释、丢弃空块      │
                 └───────────────────────┬──────────────────────────────┘
                                         ▼
              Markdown ──▶ export_markdown()  ──▶ md_to_docx() ──▶ Word
```

### 五个关键设计

**1. Backend 抽象只切两刀**

云端和本地的差异被收敛到 `APIBackend` / `LocalBackend` 两个基类。新增一个云端后端，只需要实现两个方法：

```python
def _submit_file(self, file_path: str, **opts) -> str:   # 返回 task_id
def _poll(self, task_id: str, **opts) -> str:            # 返回 markdown
```

切片、限流、重试、续跑、合并全部由基类提供 —— 后端作者不用重复处理，也不会各写一套。

**2. `task_id` 落盘 = 断点续跑的地基**

`APIBackend.convert()` 的核心就这一段（`backends/api/base_api.py`）：

```python
for part in parts:
    tid = self._load_taskid(part)
    if not tid:                          # ← 盘上已有就跳过提交
        tid = self._submit_file(part, **opts)
        self._save_taskid(part, tid)
    md = self._poll(tid, **opts)
```

`.taskid.json` 以**切片文件路径**为 key。因为切片是确定性的（同一个 PDF + 同一组阈值 ⇒ 同名切片），重跑时会命中同一条记录，于是**只轮询、不重新上传**。这是"禁止重复计费"的实现方式，也是 `--work-dir` 必须保持稳定的原因。

> 注意：只有**异步**后端（`somark`）有这条路径。`mistral` 走同步单发接口，服务端不返回可复用的任务号，**重跑会重新计费**。

**3. 双阈值切片**

`split_pdf()` 同时受页数和字节数约束：先按 `chunk_pages` 切，若某片超过 `max_bytes` 就**对半回退重试**，直到落在限制内。这样无论 API 卡的是"最多多少页"还是"最大多少 MB"，同一份代码都成立。

**4. 限流内建在 `_post()`**

```python
wait = 1.0 / max(self.qps, 0.1) - (time.time() - self._last_call)
if wait > 0:
    time.sleep(wait)
```

所有请求都走这一个出口，`qps` 从配置读，避免各端点分别实现限流。

**5. GUI 的线程模型**

Tkinter 不是线程安全的。GUI 里所有网络 / 重活都在 worker 线程跑，**worker 绝不碰 Tk**（连读一个 `StringVar` 都不行 —— 会抛 `main thread is not in main loop`）。worker 只往 `queue.Queue` 里投消息，主线程用 `root.after(100, ...)` 轮询消费。

唯一需要反向通信的是"弹窗确认"：worker 投 `("confirm", ...)` 后在一个 `threading.Event` 上等待，主线程在轮询里弹窗、把答案写回再 `set()`。

---

## 安装

需要 **Python ≥ 3.9**。

```bash
git clone https://github.com/1945151136/docflow.git
cd docflow

# 最小安装：SoMark 云端后端 + Docx 导出 + GUI（不含重型本地 OCR）
pip install -e ".[cloud,dev]"

# 再加本地 PaddleOCR 引擎（会下载 paddlepaddle，体积较大，按需装）
pip install -e ".[paddle,dev]"
```

> Tkinter 随 CPython 官方安装包一起提供；Linux 上若缺失，装 `python3-tk`。

---

## 启动步骤

### 第 0 步：确认装好了

```bash
python -m pytest -q          # 应显示 124 passed
docflow --help               # 打印 convert / to-docx / split 三个子命令
```

### 第 1 步：配置 Key（只走云端时需要）

```bash
cp .env.example .env
```

编辑 `.env`，填你要用的 key：

```ini
SOMARK_API_KEY=sk-你的key
DOCFLOW_DEFAULT_BACKEND=paddleocr   # 可选：不写 --backend 时的默认后端
```

Key 只从环境变量读，**不会**进源码；`.env` 也已在 `.gitignore` 里。

### 第 2 步：跑一次本地转换（零成本，先验证链路）

```bash
docflow convert book.pdf -o book.md
```

默认后端是本地 PaddleOCR，不联网、不花钱、文档不出本机。第一次跑会加载模型，稍慢。

### 第 3 步：转 Word

```bash
docflow to-docx book.md -o book.docx
```

Markdown 里的远程图片会下载到 `book/images/` 并内嵌进 docx。

### 第 4 步（可选）：改用云端后端

**建议先查额度，避免误触付费。** 有两条路：

- 打开 `docflow-gui`，点「查询免费额度」，只读、不计费；
- 或者直接用带前置检查的脚本，它会**先**比对免费余额和 PDF 实际页数，不够就停下：

```bash
PYTHONPATH=src python examples/run_somark.py book.pdf -o book.md
```

额度确认够、或想跳过检查时再直接调 CLI：

```bash
docflow convert book.pdf --backend somark -o book.md
```

> `run_somark.py` 内置 `/usage` 前置检查，免费不足会自动停下并提示 `--force`；
> 若 `/usage` 本身查询失败，它也会停下而不是盲目开跑。
> **直接调 `docflow convert --backend somark` 没有这道检查**，点了就提交。

### 第 5 步（可选）：打开图形界面

```bash
docflow-gui
```

界面里依次是：选后端 → 填 Key 并「保存到 .env」→「查询免费额度」→ 选 PDF 和输出 → 「开始转换」→ 「转 Word」。

> 云端后端转换前会先查余额：免费页数不够会弹确认框告诉你「剩余 X 页 / 需要 Y 页」，由你决定是否继续。也可以直接按 `Ctrl+Enter` 开始。

---

## Backend 对比

| Backend | 类型 | 需要 Key | 联网 | 计费 | 断点续跑 | 状态 |
|---|---|---|---|---|---|---|
| `paddleocr` | 本地 OCR | 否 | 否 | 免费 | ✅ 有（按页缓存） | ✅ 默认实装 |
| `somark` | 云端文档智能（异步） | 是 | 是 | 按页，有免费额度 | ✅ 有 | ✅ 实装 |
| `mistral` | 云端 OCR（同步） | 是 | 是 | 按页 | ❌ 无 | ✅ 实装 |
| `firecrawl` | 云端解析 | 是 | 是 | 按页 | — | 🚧 TODO 骨架 |

> `firecrawl` 接口已对齐 `base_api.py`，实装后即可用。
> `mistral` 是同步接口，没有 task_id 可复用 —— **重跑会重新上传并重新计费**，大文件仍会自动切片但切片不提供续跑。
> 本地 `paddleocr` 的续跑**不涉及计费**，纯粹为了省时间：文件先按页拆开，逐页 OCR 并把每页 Markdown 落到 `<work-dir>/<文件名>.pages/page_NNNN.md`，中断后重跑只处理没做过的那几页。全命中的重跑约 4 秒（不再加载 1.5GB 模型）。

---

## CLI 参考

```bash
docflow [--config PATH] <command> ...
```

| 命令 | 参数 | 说明 |
|---|---|---|
| `convert` | `file`、`--backend`、`-o/--output`、`--work-dir` | PDF/图片 → Markdown |
| `to-docx` | `md`、`-o/--output` | Markdown → Word |
| `split` | `pdf`、`--chunk-pages`、`-o/--output-dir` | 只切片 |

常用：

```bash
docflow convert book.pdf -o book.md                              # 本地默认
docflow convert book.pdf --backend somark -o book.md             # 云端
docflow convert book.pdf --backend somark --work-dir ./.work     # 固定续跑目录
docflow to-docx book.md -o book.docx
docflow split book.pdf --chunk-pages 150 -o chunks/
```

> `--work-dir` **必须保持稳定**。换目录会让 `.taskid.json` 找不到，导致重新提交、重新计费。

---

## 配置文件

`.env` 放密钥；行为参数走 YAML。默认读**当前目录**的 `config.yaml`（存在才读）。

```yaml
default_backend: paddleocr     # somark | paddleocr | mistral | firecrawl

splitter:
  chunk_pages: 150             # 每片页数
  max_bytes: 209715200         # 每片字节上限（200MB）

api:
  qps: 4                       # 每秒请求数上限
  poll_interval: 4             # 异步轮询间隔（秒）
  timeout: 60                  # 单请求超时（秒）

images:
  dir: _images                 # 图片目录（相对输出文件）
  width_inch: 6.0              # Word 中图片宽度

markdown:
  strip_split_comments: true   # 合并时清掉切片注释
```

优先级：`--config 指定文件` > 当前目录 `config.yaml` > 内置默认值。`.env` 里的 `DOCFLOW_DEFAULT_BACKEND` 会覆盖 YAML 的 `default_backend`。

```bash
docflow convert book.pdf --config ./my.yaml -o book.md
```

---

## 图形界面

```bash
docflow-gui
# 或：PYTHONPATH=src python -m docflow.gui
```

- 后端下拉；Key 掩码输入，一键写入 `.env`（原地替换，保存后 `load_dotenv(override=True)` 热加载）。
- 「查询免费额度」只读不计费，结果显示在标题行。
- 转换前自动查余额，免费页数不够会弹确认框。
- worker 线程执行 + `queue`/`after()` 刷 UI；运行中所有控件禁用、显示进度条，杜绝重复提交。
- 日志按级别着色（成功绿 / 错误红），可一键清空；`Ctrl+Enter` 或 `F5` 开始转换。

> 关闭窗口不会取消后台任务。`task_id` 已落盘，下次对同一文件重跑会走续跑。

---

## 项目结构

```
src/docflow/
├── core/
│   ├── base.py            BaseConverter / ConversionResult 抽象
│   ├── splitter.py        双阈值 PDF 切片
│   ├── config.py          .env + YAML 配置合并（deepcopy 隔离，互不串扰）
│   ├── envfile.py         .env 纯函数改写（保注释/保顺序/不误伤子串）
│   └── image_fetcher.py   远程图片下载
├── backends/
│   ├── api/
│   │   ├── base_api.py    异步提交 + 轮询 + 限流 + task_id 续跑
│   │   ├── somark.py      ✅ 异步实装
│   │   ├── mistral.py     ✅ 同步实装
│   │   └── firecrawl.py   🚧 TODO 骨架
│   └── local/
│       ├── base_local.py
│       └── paddleocr.py   ✅ 默认离线引擎
├── exporters/
│   ├── markdown.py        merge_results 合并清洗 + 落盘
│   └── docx.py            MD → Docx，XML 安全
├── gui.py                 Tkinter 界面（worker 线程 + queue/after）
└── cli.py                 convert / to-docx / split

tools/
├── recover_somark_task.py  抢救已提交但未落盘的任务（只读，不再计费）
└── offline_replay.py       用已有 markdown 离线跑合并/导出，0 API 成本
                           用法：offline_replay.py <分片目录> <参照全文.md> [-o 输出.md]

examples/                   可运行的用法脚本
tests/                      124 个用例，全部离线（mock 网络）
```

---

## 开发与测试

```bash
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest -q           # 124 passed
```

全部测试**不触网**：网络层一律 mock，GUI 测试在无显示器环境下自动跳过。

新增一个云端后端：

1. 继承 `backends/api/base_api.py` 的 `APIBackend`；
2. 实现 `_submit_file()` 和 `_poll()`；
3. 在 `cli.py` 的 `_build_backends` 和 `gui.py` 的 `make_backend` 注册名字。

细节见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

---

## 常见问题

**大文件怎么处理？**
云端路径自动按 `--chunk-pages`（默认 150）和 200MB 双阈值切片；也可先 `docflow split` 手动切。

**断点续跑怎么用？**
`--work-dir` 指向一个固定目录，重跑同一文件会复用落盘的 `task_id`，不重复计费。**work-dir 必须稳定**，否则会重新提交。

**跑到一半断了，钱白花了吗？**
不一定。若任务已在服务端提交完成，可以用 `tools/recover_somark_task.py` 把结果抢救回来（只读查询，不再计费）：

```bash
PYTHONPATH=src python tools/recover_somark_task.py \
    --from-work-dir /path/to/.docflow_work -o recovered.md
```

**图片去哪了？**
云端后端：`to-docx` 时远程图片下载到 `<输出名>/images/`，再内嵌进 docx；纯 Markdown 输出保留原始图片 URL。
本地 PaddleOCR：引擎把图片以 PIL 对象直接给出来，转换时落盘到 `<源文件名>_images/`，Markdown 里用**绝对路径**引用（因为 `.md` 落点由你决定，只有绝对路径在哪都解析得到）。

**本地 PaddleOCR 第一次跑很慢？**
首次会下载并缓存约 1.5GB 模型（到 `~/.paddlex/official_models/`），之后离线复用。实测扫描版教材约 **2 分钟/页**（纯 CPU），长文档请估好时间——474 页的教材大约要十几个小时。建议先用 `docflow split` 切几页试跑。

**`pip install 'docflow[paddle]'` 装好了，转换却报 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`？**
这是 paddlepaddle 3.3.1 + oneDNN 新 PIR 执行器的坑，崩在版面检测模型（`predict()` 的第一步），不是 docflow 的问题。docflow 默认已关闭 oneDNN 绕过它；若你的机器上 oneDNN 正常，可用 `DOCFLOW_PADDLE_MKLDNN=1` 或 `backend.convert(..., mkldnn=True)` 打开以换取速度。

**会不小心扣钱吗？**
CLI 的 `--backend somark` 不做前置检查（`examples/run_somark.py` 会查）。GUI 会在转换前查余额并在不足时拦下。两者都不会重复提交已完成的切片。

---

## License

Apache-2.0，见 [`LICENSE`](LICENSE)。各云端服务（SoMark / Mistral / Firecrawl）的商业条款由其各自提供方约束，与本项目无关。
