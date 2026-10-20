# nix/default.nix — Nix package for researcher-python
#
# The dependency set is not written here: pyproject.toml + uv.lock are the
# source, and flake.nix derives both the package venv and the dev shell from
# them via uv2nix.  `appDeps` is `workspace.deps.optionals`, i.e. every runtime
# extra the project declares (currently mcp and pdf).
#
# `mkApplication` wraps the venv, hiding the interpreter and activation scripts
# while exposing the console entry points from pyproject.toml.

{ pkgs
, python
, pythonSet
, pyproject-nix
, appDeps
}:

let
  inherit (pkgs.callPackages pyproject-nix.build.util { python3 = python; })
    mkApplication;
in
(mkApplication {
  venv = pythonSet.mkVirtualEnv "researcher-python-env" appDeps;
  package = pythonSet."researcher-python";
}).overrideAttrs (old: {
  # `mkApplication` only inherits meta from the Python package, and
  # pyproject.nix does not infer a mainProgram from `[project.scripts]`.
  meta = (old.meta or { }) // {
    mainProgram = "researcher-python";
    license = pkgs.lib.licenses.mit;
  };
})
