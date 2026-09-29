{
  description = "researcher-python — package and dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # Exposes the CLI as `nix build .#researcher-python`
      # and as the default package.
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pkg = pkgs.callPackage ./nix/default.nix { };
        in
        {
          default = pkg;
          researcher-python = pkg;
        }
      );

      # ── Dev Shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with Python >= 3.11, the runtime
      # deps, and pytest on PATH.  The package directory sits on PYTHONPATH so
      # `python cli.py` / `import researcher_python` work straight from the
      # checkout.
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: with ps; [
            requests
            python-dotenv
            pytest
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [ python ];
            shellHook = ''
              export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
              echo "researcher-python dev shell"
              python -c 'import researcher_python; print("researcher_python:", researcher_python.__version__)'
              python -c 'import requests, dotenv, pytest; print("deps: requests, python-dotenv, pytest")'
            '';
          };
        }
      );
    };
}
