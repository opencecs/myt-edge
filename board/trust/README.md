# Signing trust anchors

The raw Ed25519 public keys in this directory identify the production MCU package and system OTA signing authorities. The board build uses the appropriate trust key to validate an MCU package, and the installed system uses its OTA trust key during an update.

Private signing keys belong in the release operator's protected signing environment. Pass the signing key explicitly to the package builder. If the signing authority changes, coordinate the trust-key change on the target and preserve each board's upgrade sequence.
