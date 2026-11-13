import json
import sys
from pathlib import Path

first = json.loads(Path(sys.argv[1]).read_text())
replay = json.loads(Path(sys.argv[2]).read_text())
required = ['cohort_run_id', 'funnels', 'time_to_convert', 'identity_effects', 'comparability', 'sources']
missing = [field for field in required if field not in first]
if missing:
    raise SystemExit("缺少分析字段：" + ", ".join(missing))
if not first['cohort_run_id'] or first['cohort_run_id'] != replay.get('cohort_run_id'):
    raise SystemExit("同一输入重放未引用同一分析记录")
print("分析输出结构和重放标识核验完成")
