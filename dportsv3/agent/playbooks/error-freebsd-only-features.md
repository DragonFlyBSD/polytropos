---
triggers:
  classifications: [compile-error, link-error, configure-error, missing-dep]
  flows: [triage, patch]
tags: [freebsd-only, blacklist, capsicum, audit, sctp]
priority: 100
---

# Known Issue: FreeBSD-Only Base Features

## Pattern
- `fatal error: blacklist.h: No such file or directory`
- `fatal error: sys/audit.h: No such file or directory`
- `fatal error: sys/capsicum.h: No such file or directory`
- `-DUSE_BLACKLIST` in compile command with missing `blacklist.h`
- `undefined reference to 'blacklist_open'`
- `undefined reference to 'cap_enter'`
- Build environment shows `USE_LIBBLACKLIST=yes` or similar FreeBSD-only option enabled

## Cause
FreeBSD base system includes several facilities that do not exist in DragonFlyBSD:

| Feature | FreeBSD Header/Lib | Description |
|---------|-------------------|-------------|
| blacklistd | `blacklist.h`, `libblacklist` | Connection blacklisting daemon |
| Capsicum | `sys/capsicum.h` | Capability-based sandboxing |
| BSM Audit | `sys/audit.h`, `libbsm` | Basic Security Module auditing |
| SCTP | `netinet/sctp.h` | Stream Control Transmission Protocol |
| utmpx | `utmpx.h` | Extended user accounting (partial) |

When a port has an OPTION that enables these features (often ON by default in FreeBSD ports), the build fails on DragonFly because the headers/libraries are missing.

## Fix — author `overlay.dops` (never `Makefile.DragonFly`, which is refused)

### Option 1: Disable the option (Preferred)

If the port defines an OPTION for the feature, drop it from `OPTIONS_DEFAULT`
with the `:N` filter. It's a self-referential `:=` → use **`mk eval`** (a plain
`mk set` would render a fatal recursive `=`):

```dops
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:NLIBBLACKLIST}"
```

The `:N` modifier removes matching items from the list. Common option names:

| Missing Feature | Likely Option Name(s) |
|-----------------|----------------------|
| blacklistd | `BLACKLIST`, `LIBBLACKLIST` |
| Capsicum | `CAPSICUM` |
| BSM Audit | `AUDIT`, `BSM` |
| SCTP | `SCTP` |
| PulseAudio | `PULSEAUDIO` |
| udev/libudev | `UDEV` |
| systemd/basu | `BASU`, `SYSTEMD` |

Multiple options in one filter:

```dops
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:NPULSEAUDIO:NUDEV}"
```

### Option 2: Add replacement option (when alternative exists)

Filter out the bad option and append the good one in the same immediate value:

```dops
# Remove MIT krb5 from base, use port instead
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:NGSSAPI_BASE} GSSAPI_MIT"
# Use ALSA instead of PulseAudio
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:NPULSEAUDIO} ALSA"
```

### Option 3: Patch source to make feature optional (Last Resort)

If the port hardcodes the feature without an OPTION, stage a DragonFly source
patch under `dragonfly/` (`file materialize dragonfly/patch-X -> dragonfly/patch-X`)
that wraps the code in `#ifdef` guards or drops the problematic includes — never
`patch apply` a `dragonfly/*` patch (no extracted source at compose time).

## The `OPTIONS_DEFAULT:NFOO` filter → `mk eval`

Turning an option off is one line, whatever the option is called:

```dops
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:N<OPTION>}"
```

`<OPTION>` is the name as it appears in the port's `OPTIONS_DEFINE` /
`OPTIONS_DEFAULT` — read it there rather than guessing from the feature name.
Filters chain, so several go in one op:

```dops
mk eval OPTIONS_DEFAULT "${OPTIONS_DEFAULT:N<FIRST>:N<SECOND>}"
```

This is the whole fix for a FreeBSD-only feature the port already gates behind
an option. Reach for Option 3 only when there is no option to turn off.

## Triage Classification
This error type is **patchable** when:
1. The error is a missing header/library for a known FreeBSD-only feature
2. The port has an OPTION that controls the feature
3. The feature is not essential to the port's core functionality

Classify as `freebsd-feature` with `high` confidence when the pattern matches.

## Detection Hints
To identify the correct OPTION to disable:
1. Check errors.txt for the missing header (e.g., `blacklist.h`)
2. Search the port's Makefile for related OPTION definitions
3. Look for `-DUSE_*` flags in the failing compile command
4. Check if DeltaPorts already has a fix for similar ports
