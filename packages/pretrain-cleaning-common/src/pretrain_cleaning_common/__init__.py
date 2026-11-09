"""Fixed pretraining data cleaning data types, profiles and durable handoffs."""


def require(condition, message):
    if not condition:
        raise ValueError(message)
