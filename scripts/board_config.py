"""Select per-board build inputs while keeping shared configuration in one file."""

from copy import deepcopy


def board_config(config, board):
    """Overlay the selected product's inputs and board settings on shared defaults."""
    profiles = config.get("boards")
    if profiles is None:
        return config
    if not board or board not in profiles:
        raise ValueError("Select a configured board with --board")
    if "hardware" in config or "uefi_board" in config:
        raise ValueError("Set hardware and uefi_board in each board profile")
    profile = profiles[board]
    unknown = profile.keys() - {"hardware", "uefi_board", "inputs", "board"}
    if unknown:
        raise ValueError("Unknown board profile fields: " + ", ".join(sorted(unknown)))
    selected = deepcopy(config)
    del selected["boards"]
    for key in ("hardware", "uefi_board"):
        if key in profile:
            selected[key] = profile[key]
    selected["inputs"] = {**selected.get("inputs", {}), **profile.get("inputs", {})}
    defaults = selected.get("board", {})
    overrides = profile.get("board", {})
    selected["board"] = {**defaults, **overrides}
    signer = overrides.get("signing")
    if isinstance(signer, dict) and signer.keys() == {"sequence"}:
        # Only a sequence override reuses the shared signer. A complete
        # per-board policy keeps its own key pair and compatibility range.
        selected["board"]["signing"] = {**defaults.get("signing", {}), **signer}
    return selected
