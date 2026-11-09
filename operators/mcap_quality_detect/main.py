from pathlib import Path
from operator_sdk.v2.startup import main
from mcap_quality_detect.operator import QualityDetectionOperator

if __name__ == "__main__":
    main(declaration_path=Path(__file__).with_name("operator.yaml"),execute=QualityDetectionOperator().execute)
