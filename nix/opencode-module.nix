# Home Manager module: register the researcher MCP server globally.
#
# This is the *global* counterpart to the repo-local `.opencode/opencode.json`.
# The repo-local entry only works when opencode is opened in this checkout
# (`"cwd": "."`); a workspace in an Obsidian vault needs an absolute command and
# an absolute env-file, which is what this module provides.
#
# It is deliberately NOT applied by this repository. To use it, add the flake
# input in nixos-config and import the module, then enable:
#
#   my.programs.researcher-python = {
#     enable = true;
#     package = inputs.researcher-python.packages.${pkgs.system}.default;
#     envFile = "${config.home.homeDirectory}/.config/researcher-python/.env";
#     home = "${config.home.homeDirectory}/.local/share/researcher-python";
#   };
#
# `home` is the root for relative workspace names (RESEARCHER_HOME); absolute
# workspace paths ignore it. `envFile` is optional but recommended: a globally
# launched stdio server has no useful cwd for finding `.env`.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.programs.researcher-python;
in
{
  options.my.programs.researcher-python = {
    enable = lib.mkEnableOption "the researcher-python MCP server in opencode";

    package = lib.mkOption {
      type = lib.types.package;
      description = ''
        The researcher-python package. In nixos-config this is usually
        `inputs.researcher-python.packages.${pkgs.system}.default`.
      '';
    };

    home = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/.local/share/researcher-python";
      description = ''
        Root for named workspaces (RESEARCHER_HOME). A relative
        `workspace="cw1"` resolves to <home>/cw1; absolute paths ignore it.
      '';
    };

    envFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/you/.config/researcher-python/.env";
      description = ''
        Absolute path to the .env holding RESEARCHER_EMAIL and optional API
        keys (RESEARCHER_ENV_FILE). Keep it out of the Nix store — use agenix
        or a home.file copy, not a committed secret.
      '';
    };

    workspace = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Optional default workspace (RESEARCHER_WORKSPACE). An MCP
        open_workspace call overrides it for the session.
      '';
    };

    timeout = lib.mkOption {
      type = lib.types.int;
      default = 120000;
      description = "MCP startup timeout in milliseconds (cold starts are slow).";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.envFile != null && lib.hasPrefix "/nix/store/" cfg.envFile);
        message = ''
          my.programs.researcher-python.envFile must not point into the Nix
          store; put the .env in your home directory (or supply it via agenix).
        '';
      }
    ];

    my.programs.opencode.mcp.researcher = {
      enabled = true;
      type = "local";
      command = [ "${cfg.package}/bin/researcher-python-mcp" ];
      environment = {
        RESEARCHER_HOME = cfg.home;
      } // lib.optionalAttrs (cfg.envFile != null) {
        RESEARCHER_ENV_FILE = cfg.envFile;
      } // lib.optionalAttrs (cfg.workspace != null) {
        RESEARCHER_WORKSPACE = cfg.workspace;
      };
      timeout = cfg.timeout;
    };
  };
}
