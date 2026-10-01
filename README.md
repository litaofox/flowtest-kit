# flowtest-kit —— 业务流程功能测试工具包

从「制作 mydatama 讲解视频」的实践中沉淀的通用测试资产库：**像拍视频一样测系统**——
先按真实用户动线把业务状态「演」出来，再站在用户视角逐屏验证。它是一套
「方案方法 + 流程定义模板 + 可执行测试程序 + 范例」四位一体的端到端功能测试工具包，
设计目标是在未来任意应用项目中**复制即用**。

## 为什么需要它

在 mydatama 项目中，仅跑一遍真实业务动线（上传→治理→数据集→产品→合规→挂牌）就发现
并拦截了 4 个单元测试/静态分析无法发现的集成层缺陷：

1. DB 约束与写入顺序矛盾导致上传 500（持久层）
2. ORM 审计字段自动填充静默失效导致建数据集失败（持久层）
3. 不继承基类的实体漏填操作者导致合规记录落库失败（持久层）
4. 接口 200 但 snake_case/枚举不一致导致页面渲染全空（前后端契约 + UI）

这说明：**沿着真实业务流程做用户视角测试，是发现集成层缺陷性价比最高的手段。**
本工具包把那次动线驱动、登录态注入、三层断言、截图取证、反面流程、空态/满态时序控制等
做法全部固化下来。

## 核心理念：数据靠 API 造，画面靠浏览器验

| 通路 | 工具 | 职责 | 特点 |
|---|---|---|---|
| 造数通路 | HTTP API（requests） | 按业务动线精确制造系统状态 | 快、稳、可重复，不模拟点击 |
| 验证通路 | 无头浏览器（Playwright） | 打开真实页面做 DOM/视觉断言 | 与用户看到的完全一致 |
| 取证通路 | 截图 + 响应留档 + 报告 | 每个观察点留存证据 | 可审计、可回归 |

不推荐让浏览器逐一点击按钮造数据（慢且脆）；也不推荐只断言接口 200
（缺陷 D 接口就是 200，页面却是空的）。

## 目录结构

```
flowtest-kit/
├── README.md                         ← 本文件（3 分钟了解全貌）
├── VERSION                          ← 0.1.0
├── docs/
│   └── 方案设计说明书.md             ← 九大核心内容（框架/梳理/视角/模板/规范/场景/步骤/指标/迭代）
├── templates/
│   ├── flow.template.yaml           ← 业务流程定义模板（测试程序的输入）
│   ├── testcase.template.md         ← 标准化测试用例模板
│   ├── report.template.md           ← 测试报告模板
│   └── project-checklist.md         ← 新项目接入清单
├── kit/                             ← 可复用测试程序（无需改动即可被各项目引用）
│   ├── requirements.txt
│   ├── api.py                       ← HTTP 黑盒客户端（鉴权/上传/重试/no_auth）
│   ├── browser.py                   ← 浏览器会话（登录态注入/DOM 断言/截图/视觉回归）
│   └── runner.py                    ← YAML 流程执行器（步骤编排/三层断言/证据/报告）
├── tests/
│   └── test_flow.py                 ← pytest 入口（FLOW_FILE/FLOW_GLOB）+ 零依赖自检
└── examples/mydatama/               ← 首个落地范例（源自真实出片动线）
    ├── flow-mydatama-main.flow.yaml     ← 主链路：上传治理→数据集→产品挂牌
    ├── flow-mydatama-negative.flow.yaml ← 反面路径：密级倒挂必须 BLOCKED
    ├── env.example                  ← 运行范例所需环境变量
    └── defect-lessons.md            ← 4 个真实缺陷如何被本方案逐层拦截
```

## 30 秒快速接入一个新项目

```bash
# 1. 安装（Python 3.10+）
pip install -r kit/requirements.txt
python -m playwright install chromium

# 2. 拷贝模板
cp templates/flow.template.yaml 你的项目/flows/主链路.flow.yaml
# 按模板注释填写：base_url、登录方式、业务步骤、UI 观察点

# 3. 执行（在你的项目仓库内）
export BASE_URL=http://localhost:8090
export FLOW_GLOB="flows/*.flow.yaml"
pytest /path/to/flowtest-kit/tests/test_flow.py
```

产物输出到 `./test-output/<流程名>-<时间戳>/`：result.json、逐屏截图、Markdown 报告。
**任一断言失败，pytest 退出码非零**，可直接作为 CI 门禁。

## 适用边界（先看这个，避免误用）

- 适用：多服务 Web 应用、有明确业务动线与状态机、需要验证「真实用户走得通、页面看得到」。
- 不适用：算法正确性、性能/压测、富交互创意 UI、无法独立起环境的生产系统。
- 定位：测试金字塔**塔尖**。只保留 7~12 条关键链路，边界值/异常组合下沉到 API 契约测试与单测。

详细方法论见 [docs/方案设计说明书.md](docs/方案设计说明书.md)。
