#!/bin/sh
# flowtest-kit 自检：语法编译 + 全部 YAML 可解析 + 执行器零依赖自检（本地 HTTP 桩，无需浏览器/外部系统）
set -e
pip install -q --retries 5 --timeout 60 requests pyyaml pytest
python -m py_compile kit/api.py kit/browser.py kit/runner.py tests/test_flow.py
python - <<'PY'
import glob, yaml
files = glob.glob("templates/*.yaml") + glob.glob("examples/**/*.yaml", recursive=True)
for f in files:
    yaml.safe_load(open(f, encoding="utf-8"))
print(f"YAML_OK ({len(files)} files)")
PY
pytest tests/test_flow.py -k selfcheck -q
