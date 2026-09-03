"""ar3's foundation layer — code every app shares.

Apps reach this package the same way they reach `ar3ver`: each entry point
puts `<repo>/lib` at the front of sys.path, ahead of site-packages, so an
unrelated distribution named `ar3` cannot answer the import. A relocated copy
of one app (the isolation container copies apps/r4t alone) has no `lib` beside
it and degrades gracefully at each import site rather than requiring the
package.
"""
