---
description: Run researcher-python's offline test suite and Nix checks
---

Run the full offline verification for this repo, in this order. Do not hit
the network; everything below must pass from a clean checkout.

1. **Offline tests** (preferred — uses the flake dev shell):

   ```sh
   nix develop --command pytest tests/ -q
   ```

   If `nix` is unavailable, fall back to `python -m pytest tests/ -q` from the
   repo root with `PYTHONPATH=$PWD`.

2. **Package builds:**

   ```sh
   nix build .#researcher-python
   ```

   Confirm `./result/bin/researcher-python --version` prints the same version
   as `pyproject.toml`.

3. **Flake evaluates cleanly:**

   ```sh
   nix flake check
   ```

Report each command's result. If a test fails, fix it before reporting done —
do not summarize around a red suite.

Context: this is a scaffold (purpose TBD). If you are adding a feature, first
read `README.md`'s *Extending* section — every new runtime dependency lands in
three places (`pyproject.toml`, `nix/default.nix`, the `flake.nix` dev shell).
