# Remote-access software pins

Reviewed: 2026-10-24

## `rdc` desktop control

| Item | Pinned value |
|---|---|
| Release | `v0.4.0` |
| Source commit | `e66001f` (GitHub reports the release commit as verified and the release as immutable) |
| Windows asset | `rdc-windows-x86_64.zip` |
| SHA-256 | `521dbbb18d93b619e1e70692bf28fd8ff0ebf6d2ec18334b005806e546415fd0` |
| Download | `https://github.com/bscott/rdc/releases/download/v0.4.0/rdc-windows-x86_64.zip` |
| Checksums | `https://github.com/bscott/rdc/releases/download/v0.4.0/SHA256SUMS` |

The digest was checked against the release's `SHA256SUMS` entry before it was copied here. The installer verifies the entire ZIP digest and only reads the root `rdc.exe` entry. The binary is unsigned; Windows SmartScreen may show its first-run warning. Review the release page and checksum file again immediately before any future install. Deliberately update the version, commit, URL, and digest together if a newer reviewed release is selected.

The pinned release documents a per-user logon task at standard (`LeastPrivilege`) integrity by default. `rdc` must run in the signed-in interactive desktop; its task is unavailable at sign-out and cannot interact with secure-desktop UAC prompts. The release's Windows guide was verified by its maintainer on Windows 11 Home build 26200 with one display at 150% scaling; multiple displays remain unverified.

The installer writes an explicit `view` + `input` grant for each input-capable identity, a `view`-only grant for each view identity, and enables the local audit log. It does not grant `clipboard`, wildcard access, or edit Tailscale policy. Its Windows firewall rule is scoped to the selected local Tailscale address, Tailscale interface, tailnet source prefixes, the pinned binary, and the selected TCP port.

Sources:

- [Immutable v0.4.0 release, notes, and verified source commit](https://github.com/bscott/rdc/releases/tag/v0.4.0)
- [v0.4.0 checksum file](https://github.com/bscott/rdc/releases/download/v0.4.0/SHA256SUMS)
- [v0.4.0 Windows setup guide](https://github.com/bscott/rdc/blob/v0.4.0/docs/setup-windows.md)
- [v0.4.0 configuration guide](https://github.com/bscott/rdc/blob/v0.4.0/docs/configuration.md)
