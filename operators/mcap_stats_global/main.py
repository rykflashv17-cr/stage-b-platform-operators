from pathlib import Path
from operator_sdk.v2.startup import main
from mcap_stats_global.operator import GlobalStatsOperator

if __name__ == "__main__":
    main(declaration_path=Path(__file__).with_name("operator.yaml"),execute=GlobalStatsOperator().execute)
