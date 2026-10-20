{
  description = "researcher-python — unified scholarly research pipeline (search, screen, cite, read)";

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

      # ── Modules ───────────────────────────────────────────────────────────
      # Optional global opencode registration. Importing a module never applies
      # it; a host must set `my.programs.researcher-python.enable = true`.
      # Home Manager is the right target: it registers a user-level MCP server
      # and reads config.home.homeDirectory.
      homeManagerModules.default = import ./nix/opencode-module.nix;

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
            arxiv
            habanero
            httpx
            biopython
            pytest
            mcp
            pypdf
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [ python ];
            # Every line here goes to **stderr** on purpose.  `nix develop
            # --command CMD` inherits the shellHook's stdout, and a stdio MCP
            # server (`researcher-python-mcp`) speaks JSON-RPC on stdout — a
            # stray banner there corrupts the wire and the client drops the
            # connection.  Diagnostics belong on stderr.
            shellHook = ''
              export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
              {
                echo "researcher-python dev shell"
                python -c 'import researcher_python; print("researcher_python:", researcher_python.__version__)'
                python -c 'import requests, dotenv, arxiv, habanero, httpx, Bio, pytest, mcp, pypdf; print("deps: requests python-dotenv arxiv habanero httpx biopython pytest mcp pypdf")'
              } >&2
            '';
          };
        }
      );
    };
}
