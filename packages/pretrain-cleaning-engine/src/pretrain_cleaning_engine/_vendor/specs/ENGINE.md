# Engine mount

`Dataclean/engine` is a symlink to `qwen-manip-preprocess/src`.

Import path:

```python
sys.path.insert(0, "Dataclean/engine")
from robot_data_processing.phases.orchestrator import run_dataset_phases
```

New shared modules:

| Path | Role |
|------|------|
| `robot_data_processing/normalize/` | StandardEpisode, geometry helpers |
| `robot_data_processing/gates/` | P2 ManualGate |
| `robot_data_processing/phases/` | P0 ingest, P5 media, P6 params, orchestrator |
| `robot_data_processing/export_standard.py` | P7 standard LeRobot export |
| `robot_data_processing/stages/` | Existing Stage1–5 (P3) |
