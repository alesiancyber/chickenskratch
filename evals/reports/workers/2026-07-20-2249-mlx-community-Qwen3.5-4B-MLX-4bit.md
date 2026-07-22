# Worker eval — extract_tasks — mlx-community/Qwen3.5-4B-MLX-4bit — 2026-07-20-2249

**7/8 cases passed all 3 runs** — 21/24 runs clean

| case | clean runs | time | first failure |
|---|---|---|---|
| urgent-priority-mapping | 3/3 | 3.7s |  |
| someday-item-kept | 3/3 | 2.4s |  |
| relative-date-two-weeks | 3/3 | 3.0s |  |
| completed-item-excluded | 3/3 | 3.2s |  |
| no-tasks-pure-status | 3/3 | 1.1s |  |
| decision-is-not-a-task | 0/3 | 3.0s | expected exactly 0 task(s), got 1; extra: ['Execute the database migration from SQLite to Postgres using the plan drafte |
| multiple-tasks-mixed | 3/3 | 6.6s |  |
| date-not-repeated-in-text | 3/3 | 3.0s |  |
