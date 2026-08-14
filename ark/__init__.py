"""The Ark's foundation layer — code every app shares.

Apps reach this package the same way they reach `arkver`: the repo root is
appended to sys.path by each entry point, and a relocated copy of one app
(the isolation container copies apps/r4t alone) degrades gracefully at each
import site rather than requiring the package.
"""
