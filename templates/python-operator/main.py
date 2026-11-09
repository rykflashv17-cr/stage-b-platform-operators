from pathlib import Path
from operator_sdk.v2.startup import main
from quality_gate.operator import QualityGateOperator

if __name__ == "__main__":
    main(declaration_path=Path(__file__).with_name("operator.yaml"),execute=QualityGateOperator().execute)
