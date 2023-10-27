{ config
, pkgs
, lib
, ...
}:
let
  cfg = config.services.buildbot-nix.master;
in
{
  options = {
    services.buildbot-nix.master = {
      enable = lib.mkEnableOption "buildbot-master";
      dbUrl = lib.mkOption {
        type = lib.types.str;
        default = "postgresql://@/buildbot";
        description = "Postgresql database url";
      };
      github = {
        tokenFile = lib.mkOption {
          type = lib.types.path;
          description = "Github token file";
        };
        webhookSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Github webhook secret file";
        };
        oauthSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Github oauth secret file";
        };
        # TODO: make this an option
        # https://github.com/organizations/numtide/settings/applications
        # Application name: BuildBot
        # Homepage URL: https://buildbot.numtide.com
        # Authorization callback URL: https://buildbot.numtide.com/auth/login
        # oauth_token:  2516248ec6289e4d9818122cce0cbde39e4b788d
        oauthId = lib.mkOption {
          type = lib.types.str;
          description = "Github oauth id. Used for the login button";
        };
        # Most likely you want to use the same user as for the buildbot
        user = lib.mkOption {
          type = lib.types.str;
          description = "Github user that is used for the buildbot";
        };
        admins = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          description = "Users that are allowed to login to buildbot, trigger builds and change settings";
        };
        topic = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = "build-with-buildbot";
          description = ''
            Projects that have this topic will be built by buildbot.
            If null, all projects that the buildbot github user has access to, are built.
          '';
        };
      };
      workersFile = lib.mkOption {
        type = lib.types.path;
        description = "File containing a list of nix workers";
      };
      buildSystems = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ pkgs.hostPlatform.system ];
        description = "Systems that we will be build";
      };
      evalMaxMemorySize = lib.mkOption {
        type = lib.types.str;
        default = "2048";
        description = ''
          Maximum memory size for nix-eval-jobs (in MiB) per
          worker. After the limit is reached, the worker is
          restarted.
        '';
      };
      domain = lib.mkOption {
        type = lib.types.str;
        description = "Buildbot domain";
        example = "buildbot.numtide.com";
      };
    };
  };
  config = lib.mkIf cfg.enable {
    services.buildbot-master = {
      enable = true;
      extraImports = ''
        import sys
        sys.path.append("${../buildbot_nix}")
        from datetime import timedelta
        from buildbot_nix import GithubConfig, NixConfigurator
      '';
      extraConfig = ''
        c["www"]["plugins"] = c["www"].get("plugins", {})
        c["www"]["plugins"].update(
            dict(base_react={}, waterfall_view={}, console_view={}, grid_view={})
        )
      '';
      configurators = [
        ''
          util.JanitorConfigurator(logHorizon=timedelta(weeks=4), hour=12, dayOfWeek=6)
        ''
        ''
          NixConfigurator(
              github=GithubConfig(
                  oauth_id=${builtins.toJSON cfg.github.oauthId},
                  admins=${builtins.toJSON cfg.github.admins},
                  buildbot_user=${builtins.toJSON cfg.github.user},
                  topic=${builtins.toJSON cfg.github.topic},
              ),
              url=${builtins.toJSON config.services.buildbot-master.buildbotUrl},
              nix_eval_max_memory_size=${builtins.toJSON cfg.evalMaxMemorySize},
              nix_supported_systems=${builtins.toJSON cfg.buildSystems},
          )
        ''
      ];
      buildbotUrl =
        let
          host = config.services.nginx.virtualHosts.${cfg.domain};
          hasSSL = host.forceSSL || host.addSSL;
        in
        "${if hasSSL then "https" else "http"}://${cfg.domain}/";
      dbUrl = config.services.buildbot-nix.master.dbUrl;
      package = (pkgs.buildbot.overrideAttrs (old: {
        patches = old.patches ++ [ ./0001-allow-secrets-to-be-group-readable.patch ];
      }));
      pythonPackages = ps: [
        ps.requests
        ps.treq
        ps.psycopg2
        (ps.toPythonModule pkgs.buildbot-worker)
        ps.setuptools
        pkgs.buildbot-plugins.www
        pkgs.buildbot-plugins.www-react
        pkgs.buildbot-plugins.console-view
        pkgs.buildbot-plugins.waterfall-view
        pkgs.buildbot-plugins.grid-view
        pkgs.buildbot-plugins.wsgi-dashboards
        pkgs.buildbot-plugins.badges
      ];
    };

    systemd.services.buildbot-master = {
      serviceConfig = {
        # in master.py we read secrets from $CREDENTIALS_DIRECTORY
        LoadCredential = [
          "github-token:${cfg.github.tokenFile}"
          "github-webhook-secret:${cfg.github.webhookSecretFile}"
          "github-oauth-secret:${cfg.github.oauthSecretFile}"
          "buildbot-nix-workers:${cfg.workersFile}"
        ];
      };
    };

    services.postgresql = {
      enable = true;
      ensureDatabases = [ "buildbot" ];
      ensureUsers = [
        {
          name = "buildbot";
          ensurePermissions."DATABASE buildbot" = "ALL PRIVILEGES";
        }
      ];
    };

    services.nginx.enable = true;
    services.nginx.virtualHosts.${cfg.domain} = {
      locations."/".proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/";
      locations."/sse" = {
        proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/sse";
        # proxy buffering will prevent sse to work
        extraConfig = "proxy_buffering off;";
      };
      locations."/ws" = {
        proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/ws";
        proxyWebsockets = true;
        # raise the proxy timeout for the websocket
        extraConfig = "proxy_read_timeout 6000s;";
      };

      # In this directory we store the lastest build store paths for nix attributes
      locations."/nix-outputs".root = "/var/www/buildbot/";
    };

    # Allow buildbot-master to write to this directory
    systemd.tmpfiles.rules = [
      "d /var/www/buildbot/nix-outputs 0755 buildbot buildbot - -"
    ];

  };
}
