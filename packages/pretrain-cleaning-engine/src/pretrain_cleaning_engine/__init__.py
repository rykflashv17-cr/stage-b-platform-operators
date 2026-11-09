from pathlib import Path

REVISION = '0e694dbc9706b2a5bac8e550f8cd9de18f5b7473'

def root():
    return Path(__file__).resolve().parent / "_vendor"
