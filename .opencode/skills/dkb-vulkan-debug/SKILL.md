---
name: dkb-vulkan-debug
description: Diagnose NVN 2 to Vulkan translation, shaders, rendering bugs, and GPU synchronization.
---

# DKB Vulkan Debugging

1. Read AGENTS.md and repository architecture docs.
2. Identify a reproducible rendering problem.
3. Inspect the relevant runtime and NVN 2 translation code.
4. Examine Vulkan validation output and runtime logs.
5. Use REA/Ghidra only when original ARM64 behavior is unclear.
6. Propose the smallest source-code fix.
7. Rebuild and execute relevant tests.

Never edit generated C code directly.
Never commit proprietary game files.
Clearly report verified results and remaining problems.
