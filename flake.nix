{
  description = "researcher-python — unified scholarly research pipeline (search, screen, cite, read)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = lib.genAttrs supportedSystems;

      # `pyproject.toml` + `uv.lock` are the only dependency source.  Both the
      # package venv and the dev shell below are derived from this workspace;
      # there is deliberately no dependency list in this file.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      # Runtime extras (mcp, pdf) are opt-in in `pyproject.toml`; the Nix
      # package ships all of them, matching the previous build.
      appDeps = workspace.deps.optionals;

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # 3.14 today.  If a dependency has no wheel yet, pin `python314`
          # (or `python313`) explicitly rather than relying on `python3`.
          python = pkgs.python3;

          baseSet = pkgs.callPackage pyproject-nix.build.packages {
            inherit python;
          };

          basePythonSet = baseSet.overrideScope (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
            ]
          );

          # Run the offline suite inside the sandbox.  Python packages do not
          # have a checkPhase: pyproject.nix/nixpkgs rename `checkPhase` to
          # `installCheckPhase` when the derivation is created, so an
          # `overrideAttrs { checkPhase = ...; }` would be silently ignored.
          #
          # The check environment is a venv built from the *dependency closure*
          # of `workspace.deps.all` (pytest + mcp + pypdf + transitives), not a
          # hand-maintained list.  `query.deps` deliberately excludes the
          # queried package itself, so this does not create a cycle.  The
          # source tree is put first on PYTHONPATH so the tests exercise the
          # checkout (and `test_package.py` can find pyproject.toml).
          testOverlay =
            final: prev:
            let
              checkEnv = final.mkVirtualEnv "researcher-python-check-env" (
                pyproject-nix.build.lib.query.deps { } final workspace.deps.all
              );
            in
            {
              researcher-python = prev.researcher-python.overrideAttrs (old: {
                doInstallCheck = true;
                installCheckPhase = ''
                  runHook preInstallCheck
                  export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
                  ${checkEnv}/bin/python -m pytest tests/ -q
                  runHook postInstallCheck
                '';
              });
            };

          pythonSet = basePythonSet.overrideScope testOverlay;
          editablePythonSet = basePythonSet.overrideScope editableOverlay;
        in
        {
          inherit
            pythonSet
            editablePythonSet
            python
            pkgs
            ;
        }
      );
    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # Exposes the CLI as `nix build .#researcher-python`
      # and as the default package.
      packages = forAllSystems (
        system:
        let
          p = pythonSets.${system};
          pkg = p.pkgs.callPackage ./nix/default.nix {
            inherit (p) python pythonSet;
            inherit pyproject-nix appDeps;
          };
        in
        {
          default = pkg;
          researcher-python = pkg;
        }
      );

      # ── Modules ───────────────────────────────────────────────────────────
      # Optional global opencode registration. Importing a module never applies
      # it; a host must set `my.programs.researcher-python.enable = true`.
      # Home Manager is the right target: it registers a user-level MCP server
      # and reads config.home.homeDirectory.
      homeManagerModules.default = import ./nix/opencode-module.nix;

      # ── Dev Shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into an editable uv2nix venv with the runtime
      # extras and the `dev` dependency group (pytest) already installed.
      # `REPO_ROOT` is what the editable overlay points at; the banner stays on
      # stderr because a stdio MCP server speaks JSON-RPC on stdout.
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          p = pythonSets.${system};
          virtualenv = p.editablePythonSet.mkVirtualEnv
            "researcher-python-dev-env"
            workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
              pkgs.git
            ];

            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = "${p.python.interpreter}";
              UV_PYTHON_DOWNLOADS = "never";
            };

            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
              {
                echo "researcher-python dev shell (uv2nix)"
                python -c 'import researcher_python, mcp, pypdf, pytest; print("deps ok")'
              } >&2
            '';
          };

          # Intentionally independent of `workspace`: this shell still works
          # before the first `uv lock` (when the rest of the flake cannot
          # evaluate yet).
          bootstrap = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
            ];
          };
        }
      );
    };
}
