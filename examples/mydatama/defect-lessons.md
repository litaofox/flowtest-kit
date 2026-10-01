# 缺陷拦截复盘：4 个真实缺陷如何被本方案逐层拦下

> 这些缺陷是 mydatama v0.1.0 在**真实业务动线执行**中发现的（而非代码审查），
> 已全部修复（mydatama 提交 e38a31d、7c3e9cf）。下表说明：
> 如果当时已有 flowtest-kit，每个缺陷会在哪一步、被哪一层断言拦下。
> 这也是方案「缺陷 → 断言闭环」机制的首批回归资产。

## 一、缺陷与拦截位对照

| 编号 | 缺陷 | 触发动作 | 根因层次 | 拦截步骤（flow） | 拦截断言 |
|---|---|---|---|---|---|
| A | 上传文件 HTTP 500 | 上传 CSV | DB 约束 × 写入顺序矛盾 | `upload_0901` / `wait_processed` | 契约：code=0 且轮询至 READY；落库：`raw_path` 非空 |
| B | 建数据集报 `create_by` 非空违反 | 创建数据集 | ORM 自动填充静默失效 | `create_dataset` | 契约：code=0；落库：`create_by = admin` |
| C | 合规记录落库失败 | 合规校验 | 实体不继承基类、漏赋操作者 | `compliance_passed` | 契约：status=PASSED；落库：4 条记录 `create_by` 全非空 |
| D | 文件详情页字段全空、状态标签不亮 | 打开详情页 | 前后端契约漂移（snake_case / SUCCEEDED 枚举） | `page_file_detail` | 呈现层：文件名文本、`.el-tag--success` 可见、2 个 `.mask-tag` |

## 二、关键启示

### 1. 集成缺陷只能沿业务流程暴露
A/B/C 都发生在服务/ORM/DB 边界：单测里 Repository 可能被 mock，
契约测试可能只验 happy JSON——只有真实写库、真实走状态机才会撞 NOT NULL。
这对应方案中「不 mock 内部服务」「状态机是黄金断言依据」两条原则。

### 2. 「接口 200」不等于「用户能用」——呈现层不可替代
缺陷 D 最典型：后端返回 200、数据也在，只是字段名（snake_case vs camelCase）
与状态枚举（SUCCEEDED vs SUCCESS）和前端契约不一致，页面渲染全空。
只做 API 断言必然漏放；只有真实浏览器打开页面断言 DOM 才能拦下。
本工具包因此强制三层断言，并在指标里统计「三层断言分布」防退化。

### 3. 反面路径是安全设计的验收单
密级倒挂场景中，平台 configure 接口**硬拦截倒挂**（正确行为）。
为了在合规引擎阶段复现红屏，范例使用显式开关 `DEMO_TWEAK_DSN` 造数
（默认关闭、仅限演示库、SQL 白名单 + allow_write 双控制），
最终断言 R1 行 `passed=false`、UI 显示「合规拦阻」。
预期拒绝（401/403/BLOCKED）一律计为 PASS——「系统该拦的拦住了」才是质量。

### 4. 修复必须沉淀为永久回归
四个缺陷修复后，对应断言已写入两份范例 flow：
- 主流程 `flow-mydatama-main.flow.yaml`：A/B/C/D 的正路径回归位；
- 反面流程 `flow-mydatama-negative.flow.yaml`：密级倒挂拦截回归位。
今后同类回归（字段改名、枚举漂移、审计列漏填、约束回退）会在发版门禁直接变红。

## 三、在 mydatama 上运行范例

```powershell
# 1. 起平台
cd d:\ProgramData\mytraeprojects\mydatama; docker compose up -d
# 2. 备样例
python d:\ProgramData\mytraeprojects\mydatama-video\assets\fixtures\generator\generate_all.py
# 3. 设环境（参考 env.example）后执行
$env:BASE_URL="http://localhost:8090"; $env:ADMIN_USER="admin"; $env:ADMIN_PASS="Admin@123"
$env:SAMPLES_DIR="d:\ProgramData\mytraeprojects\mydatama-video\assets\fixtures"
python d:\ProgramData\mytraeprojects\flowtest-kit\kit\runner.py `
  d:\ProgramData\mytraeprojects\flowtest-kit\examples\mydatama\flow-mydatama-main.flow.yaml
```
