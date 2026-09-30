{
  description = "zeit – CLI für die SAP-Zeiterfassung (CATS) mit Kerberos-SSO";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    # Python-Abhängigkeiten kommen aus der uv.lock (gleiche Versionen und Hashes wie bei `uv sync`).
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

  outputs = { self, nixpkgs, flake-utils, pyproject-nix, uv2nix, pyproject-build-systems }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        inherit (pkgs) lib;
        python = pkgs.python3;

        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

        # Wheels bevorzugen; gssapi hat für Linux nur ein sdist und wird gebaut.
        overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

        # Nachbesserungen für Pakete, die unter Nix nicht ohne Weiteres laufen/bauen.
        pyprojectOverrides = final: prev: {
          sap-zeit = prev.sap-zeit.overrideAttrs (_: {
            src = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [ ./pyproject.toml ./zeit.py ];
            };
          });

          gssapi = prev.gssapi.overrideAttrs (old: {
            # Wie in nixpkgs: Header liegen in krb5.dev, nicht unter `krb5-config --prefix`.
            postPatch = (old.postPatch or "") + ''
              substituteInPlace setup.py \
                --replace-fail 'get_output(f"{kc} gssapi --prefix")' '"${lib.getDev pkgs.krb5}"'
            '';
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.krb5.dev ]
              ++ final.resolveBuildSystem { setuptools = [ ]; cython = [ ]; };
            buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.krb5 ];
          });
        } // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # manylinux-Wheels mit nativen Binaries (greenlet, Playwright-Node-Treiber) für NixOS patchen.
          greenlet = prev.greenlet.overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.autoPatchelfHook ];
            buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.stdenv.cc.cc.lib ];
          });
          playwright = prev.playwright.overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.autoPatchelfHook ];
            buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.stdenv.cc.cc.lib ];
          });
        };

        pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope
          (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]);

        venv = pythonSet.mkVirtualEnv "sap-zeit-env" workspace.deps.default;

        # Die von PyPI geladenen Playwright-Browser laufen unter NixOS nicht, und die aus nixpkgs
        # gehören zu einer anderen Playwright-Version (andere Revisionen). Daher die Verzeichnisse
        # anlegen, die die gelockte Playwright-Version laut browsers.json erwartet, und auf die
        # nixpkgs-Browser verlinken – so passt es unabhängig davon, was in der uv.lock steht.
        playwrightBrowsers = pkgs.runCommand "playwright-browsers-${pythonSet.playwright.version}"
          { nativeBuildInputs = [ pkgs.jq ]; }
          ''
            mkdir -p $out
            browsersJson=$(echo ${pythonSet.playwright}/lib/python*/site-packages/playwright/driver/package/browsers.json)
            jq -r '.browsers[] | select(.name == "chromium" or .name == "chromium-headless-shell" or .name == "ffmpeg")
                   | "\(.name | gsub("-"; "_")) \(.revision)"' "$browsersJson" |
            while read -r name revision; do
              src=$(echo ${pkgs.playwright-driver.browsers}/"$name"-*)
              [ -e "$src" ] || { echo "kein $name in playwright-driver.browsers" >&2; exit 1; }
              ln -s "$src" "$out/$name-$revision"
            done
          '';

        # Nur das `zeit`-Programm nach außen geben, nicht das komplette venv.
        # `zeitSystem` legt das Standard-System fest (setzt ZEIT_SYSTEM, `-s` gewinnt weiterhin),
        # z. B. `zeit.override { zeitSystem = "btp"; }`.
        zeit = lib.makeOverridable ({ zeitSystem ? null }: pkgs.runCommand "sap-zeit-${pythonSet.sap-zeit.version}"
          {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            meta = {
              description = "CLI für die SAP-Zeiterfassung (CATS) mit Kerberos-SSO";
              mainProgram = "zeit";
              platforms = lib.platforms.unix;
            };
          }
          ''
            # Browser-Anmeldung: Playwright-Chromium aus nixpkgs als Fallback, falls weder Edge
            # noch Chrome installiert ist.
            makeWrapper ${venv}/bin/zeit $out/bin/zeit \
              --set-default PLAYWRIGHT_BROWSERS_PATH ${playwrightBrowsers} \
              --set-default PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS true \
              ${lib.optionalString (zeitSystem != null) "--set-default ZEIT_SYSTEM ${lib.escapeShellArg zeitSystem}"}
          '') { };
      in
      {
        packages = {
          default = zeit;
          zeit = zeit;
        };

        apps.default = flake-utils.lib.mkApp { drv = zeit; };

        checks.default = pkgs.runCommand "zeit-import-check" { } ''
          ${venv}/bin/python -c "import zeit" && touch $out
        '';

        devShells.default = pkgs.mkShell {
          packages = [ pkgs.uv python ];
          env = {
            UV_PYTHON = python.interpreter;
            UV_PYTHON_DOWNLOADS = "never";
          };
        };
      })
    // {
      overlays.default = final: _prev: {
        zeit = self.packages.${final.stdenv.hostPlatform.system}.default;
      };
    };
}
