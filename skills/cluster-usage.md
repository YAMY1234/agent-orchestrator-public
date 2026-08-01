# Cluster Usage Guidelines

- 使用 `squeue -u $USER` 查看当前提交的 Slurm 任务
- 同一时间运行的 Slurm 任务不要超过 2 个，提交前先检查
- 使用 `scancel <job_id>` 取消不需要的任务
- 日志通常在 `outputs/<job_id>-<name>/logs/` 下
- 长时间运行的任务请使用 `squeue --me -o "%.18i %.9P %.50j %.8T %.10M"` 检查状态
