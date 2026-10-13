# Reproducible Python dependencies

The project keeps human-edited requirement inputs (`requirements*.txt` and
`requirements*.in`) separate from generated, hash-pinned lock files. CI and
packaging scripts install the lock files with `pip --require-hashes`.

Regenerate locks with uv 0.13.0 from the repository root:

```powershell
uv pip compile --universal --python-version 3.10 --generate-hashes requirements.txt -o requirements.lock
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-dev.in -o requirements-dev.lock
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-build.in -o requirements-build.lock
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-translator-only-build.in -o requirements-translator-only-build.lock
```

Review all four generated files in the same change as edits to their input
files. The development lock includes runtime requirements and test/lint tools;
the build locks include PyInstaller and its community hooks.
