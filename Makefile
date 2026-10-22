.PHONY: up seed ingest rollup demo report test test-unit perf perf-p5 perf-p6 openapi down logs

API ?= http://localhost:8000
RUN  := docker compose exec -T api uv run --no-dev salestrend

up:            ## 起 ClickHouse、Redis、API 三个容器
	docker compose up -d --build --wait

seed:          ## 重建表 → 导入 xlsx → 分类 → 汇总重算
	$(RUN) seed

ingest:        ## 同一文件再导一次：演示跨批去重（fact 行数不变）
	$(RUN) ingest

rollup:        ## 汇总重算（幂等）
	$(RUN) rollup

demo:          ## 打印典型请求与真实响应
	@bash scripts/demo.sh $(API)

report:        ## 生成数据质量报告（不需要数据库）
	uv run salestrend report

openapi:
	uv run salestrend openapi

test:          ## 单元 + 集成（集成测试需要 make up && make seed）
	uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

perf:          ## k6 固定到达率压测（P1–P4、P6）
	bash perf/run.sh $(API)

perf-p5:       ## 千万级实测：独立库 p5 合成 1000 万行，测存储、重算、三段查询；测完 DROP（KEEP=1 保留）
	uv run python perf/p5.py $(if $(KEEP),--keep,)
	uv run python perf/report.py perf/results docs/perf-report.md

perf-p6:       ## 只跑 P6 任意区间命中率（含 20 热点 key 对照）
	SCENS=range bash perf/run.sh $(API)

down:
	docker compose down -v

logs:
	docker compose logs -f api
