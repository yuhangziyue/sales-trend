"""demo.sh 用：把趋势响应压成每个平台一行，错误体原样缩进打印。"""
import json
import sys

b = json.load(sys.stdin)
if isinstance(b.get("data"), dict) and "dates" in b["data"]:
    d, m = b["data"], b["meta"]
    n = len(d["dates"])

    def cut(xs):
        return xs if n <= 8 else xs[:4] + ["..."] + xs[-3:]

    def j(x):
        return json.dumps(x, ensure_ascii=False)

    print("dates       ", j(cut(d["dates"])))
    for s in d["series"]:
        print(f"{s['platform']:<12}", j(cut(s["values"])))
    print("total       ", j(cut(d["total"])))
    for k in ("days_in_bucket", "days_covered", "partial", "daily_avg"):  # 仅 week / month
        if k in d:
            print(f"{k:<12}", j(cut(d[k])))
    print("sum         ", j(m["sum"]))
    print("plan        ", j([f'{p["segment"]} {p["from"]}..{p["to"]}' for p in m["plan"]]))
    print(f'meta         tz={m["tz"]} rule={m["rule_version"]} data_through={m["data_through"]} '
          f'partial={m["partial_dates"]} cache={m["cache"]}')
    print("through/src ", j(m["data_through_by_platform"]))
    print(f'source_tz    assumed={j(m["source_tz_assumed"])} warnings={j([w["code"] for w in m["warnings"]])}')
else:
    print(json.dumps(b, ensure_ascii=False, indent=1))
