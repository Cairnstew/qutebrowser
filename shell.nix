# For `nix-shell` users. `nix develop` reads flake.nix directly.
let
  flake = builtins.getFlake (toString ./.);
in
flake.devShells.${builtins.currentSystem}.default
