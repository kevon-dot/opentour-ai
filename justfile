# OPE-122 owns setup only. OPE-123 takes exclusive ownership after reviewed handoff.
set shell := ["sh", "-eu", "-c"]

setup:
    python3 tools/bootstrap.py
