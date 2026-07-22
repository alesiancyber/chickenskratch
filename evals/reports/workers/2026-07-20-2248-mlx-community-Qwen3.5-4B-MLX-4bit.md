# Worker eval — extract_tasks — mlx-community/Qwen3.5-4B-MLX-4bit — 2026-07-20-2248

**6/8 cases passed all 3 runs** — 18/24 runs clean

| case | clean runs | time | first failure |
|---|---|---|---|
| urgent-priority-mapping | 3/3 | 3.7s |  |
| someday-item-kept | 0/3 | 2.5s | '(?i)pricing page': priority='low', expected None |
| relative-date-two-weeks | 3/3 | 2.9s |  |
| completed-item-excluded | 3/3 | 3.2s |  |
| no-tasks-pure-status | 3/3 | 1.1s |  |
| decision-is-not-a-task | 0/3 | 3.1s | expected exactly 0 task(s), got 1; extra: ['Execute the database migration from SQLite to Postgres using the plan drafte |
| multiple-tasks-mixed | 3/3 | 6.8s |  |
| date-not-repeated-in-text | 3/3 | 3.2s |  |
