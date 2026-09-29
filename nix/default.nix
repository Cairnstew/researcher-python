# nix/default.nix — Nix package for researcher-python
#
# Builds the Python library + CLI as a single derivation.
# `buildPythonApplication` registers the console_scripts entry point
# from pyproject.toml and wraps the binary with the correct PYTHONPATH.

{ lib
, python3
}:

python3.pkgs.buildPythonApplication {
  pname = "researcher-python";
  version = "0.1.0";
  format = "pyproject";

  # Clean source: exclude git, cache, venv, nix, env artifacts.
  src = lib.cleanSourceWith {
    filter = path: type:
      let
        base = builtins.baseNameOf path;
      in
      base != ".git"
      && base != "__pycache__"
      && base != ".pytest_cache"
      && base != ".venv"
      && base != "nix"
      && base != ".env"
      && base != ".opencode"
      && base != "*.log";
    src = ./..;
  };

  nativeBuildInputs = with python3.pkgs; [
    setuptools
    wheel
  ];

  propagatedBuildInputs = with python3.pkgs; [
    requests
    python-dotenv
  ];

  # Tests run offline via `nix develop` + pytest; skip testing at package
  # build time to keep the derivation light.
  doCheck = false;

  meta = with lib; {
    description = "researcher-python — Python project scaffold (purpose TBD).";
    license = licenses.mit;
    maintainers = [ ];
    mainProgram = "researcher-python";
  };
}
