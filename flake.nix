{
  description = "qutebrowser (Cairnstew fork) with native 1Password integration";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Runtime dependencies + the plugins listed in pytest.ini's
      # required_plugins, so `pytest tests/unit` works out of the box.
      python = pkgs: pkgs.python3.withPackages (ps: with ps; [
        # qutebrowser runtime
        pyqt6
        pyqt6-webengine
        jinja2
        pyyaml
        pygments
        packaging
        adblock
        # test suite
        pytest
        pytest-qt
        pytest-bdd
        pytest-benchmark
        pytest-instafail
        pytest-mock
        pytest-rerunfailures
        pytest-xvfb
        hypothesis
        beautifulsoup4
        requests
        cheroot
        flask
        werkzeug
        parse
        parse-type
        mako
        manhole
        tldextract
      ]);
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.qutebrowser.overrideAttrs (_old: {
          src = self.outPath;
        });
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (python pkgs)
            pkgs.git
            # Only needed for manual end-to-end testing of the userscript
            # fallback; the component itself does not shell out to it.
            pkgs.wofi
          ];

          env = {
            QTWEBENGINE_RESOURCES_PATH = "${pkgs.qt6.qtwebengine}/resources";
            QTWEBENGINE_LOCALES_PATH = "${pkgs.qt6.qtwebengine}/qtwebengine_locales";
            QT_PLUGIN_PATH = pkgs.lib.makeSearchPathOutput "out" "lib/qt-6/plugins" [
              pkgs.qt6.qtbase
              pkgs.qt6.qtwebengine
              pkgs.qt6.qtsvg
            ];
            NIXPKGS_QT6_QML_IMPORT_PATH = pkgs.lib.makeSearchPath "lib/qt-6/qml" [
              pkgs.qt6.qtdeclarative
              pkgs.qt6.qtwayland
            ];
          };

          shellHook = ''
            echo "qutebrowser dev shell — run the browser from the checkout with:"
            echo "  python qutebrowser.py --temp-basedir"
            echo "run the unit tests with:"
            echo "  pytest tests/unit/components/test_onepassword.py"
          '';
        };
      });
    };
}
