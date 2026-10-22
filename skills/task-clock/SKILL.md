---
name: task-clock
description: "Open the local task clock when the user asks to see Codex elapsed time, remaining time, or task progress. Update a task estimate when requested."
---

Use `eta_panel` and open its returned URL with `open_in_codex`, target type `browser`, placement `right`. Keep the panel open; it updates automatically. If the tools are not loaded yet, execute `python <plugin-root>/runtime.py panel` and open the returned URL.

To supply a task estimate, call `eta_status`, identify the current session by its exact ID and title, and call `eta_set_estimate` with a realistic remaining range in seconds and a short basis. When multiple sessions could match, resolve the identity before writing. Use only the current task's scope and evidence. Revise the estimate when that scope changes; describe it as an estimate. If no defensible range exists, leave the estimate unknown.

This panel tracks local Codex turns. Completion of a turn is not proof that an entire project or external process finished. Log detection is a best-effort fallback. Hook tracking requires the user to review and trust the plugin's hook definition in Codex settings. Do not bypass hook trust or modify trust records.
