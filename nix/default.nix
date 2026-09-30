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
      &&       base != ".env"
      && base != ".opencode";
    src = ./..;
  };

  nativeBuildInputs = with python3.pkgs; [
    setuptools
    wheel
    pytest
  ];

  propagatedBuildInputs = with python3.pkgs; [
    requests
    python-dotenv
    arxiv
    habanero
    httpx
    biopython
  ];

  # Run the offline suite inside the Nix sandbox: it is stricter than
  # `nix develop` (no PYTHONPATH, no network, only files in `src`), so a green
  # build here proves the source filter carries README.md/.env.example/scripts/
  # and that nothing quietly depends on a checkout-only path.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    pytest tests/ -q
    runHook postCheck
  '';

  meta = with lib; {
    description = "Unified scholarly search pipeline across arXiv, OpenAlex, Semantic Scholar, Crossref, and PubMed.";
    license = licenses.mit;
    maintainers = [ ];
    mainProgram = "researcher-python";
  };
}
