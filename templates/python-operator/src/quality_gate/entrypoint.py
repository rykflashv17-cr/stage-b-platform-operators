from .operator import QualityGateOperator


def execute(ctx):
    QualityGateOperator().execute(ctx)
