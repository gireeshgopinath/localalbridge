# Validation record

Validated on 2026-11-02, Europe/Berlin, on this Windows machine.

## Results

- **55 tests passed** in 26.75 seconds on the portability release rerun. See `test-results.txt` and `tests/test_bridge.py`.
- **10/10 pilot tasks accepted**, against the planned threshold of 8/10.
- **10 total task attempts; zero repairs in the final pilot.**
- All three implementation batches passed their independent integration checks and received both review decisions.
- **7 explicit main-orchestrator decisions**: one exploration acceptance and two decisions for each implementation batch.
- **31 local tool calls** and **940.92 seconds** of local execution/verification time, including integration.
- **121 MCP interactions** in the verification harness, including repeated evidence reads and status checks during integration debugging.
- The final-build smoke task completed both gates in **28.28 seconds** of local execution. The client observed exactly two meaningful notifications: `needs_review` and `final_review`.
- The original synthetic repository retained its captured HEAD and clean checkout.

The pilot covered exploration, addition, bounds repair, slug generation, boolean parsing, stable deduplication, arithmetic mean/error handling, unittest authoring, documentation, and filename extensions. Full patches, source context, exact commands/results, and local reports were inspected before each decision.

## What was tested

The automated suite exercises task/path validation, Windows junction rejection, durable SQLite state, concurrent updates, crash recovery, ownership locks, artifact pagination and redaction, evidence read gates, independent checks overriding false worker success, explicit two-stage acceptance, cancellation, model-unavailable stops, malformed/permission/budget failures, process-tree cleanup, full-response retention, structured MCP tool metadata, escalation, and meaningful notifications.

The live pilot used OpenCode 2.0.18, the installed Qwen 27B model through LM Studio on port 1234, a 16K context, one active inference slot, and separate implementation/review sessions. Normal project edits were performed by the local model. Codex implemented the bridge infrastructure after the user explicitly approved that setup method.

## Interpretation and limits

This is a small synthetic coding pilot, not a benchmark of arbitrary repositories or proof of general model correctness. Local reviews contained some inaccurate examples and assumptions; the main orchestrator evaluated the code and independent evidence directly. It retained every acceptance decision.

Cloud token usage is unavailable, and OpenCode step-token telemetry can be incomplete. No cloud-token or cost-savings percentage is claimed. The design reduces routine orchestration traffic through batching, compact status packets, local execution/repairs, concise captured-source reviews, and meaningful notifications. Necessary code/evidence inspection still consumes orchestrator context.

Initial calibration found and repaired persistence, subprocess/stdio, timeout, Windows path-length, and reviewer-scope defects. The final ten-task results above are from the isolated final pilot state, separate from calibration runs. The additional final-build smoke verifies the latest notification, response-retention, and concise-review code paths.

## Evidence and operation

- `pilot-results.json`: measured results and task outcomes.
- `review-decisions.json`: criteria, explicit decisions, and evidence-read receipts.
- `final-smoke-results.json`: latest-build result and actual received MCP notifications.
- `README.md`: setup, operating instructions, and practical limits.

The MCP server is registered as **local-workers**. Restart Codex or open a new chat to load it. The registered runtime uses the existing Python environment under this workspace's `work/venv`; `setup.ps1` can create a fresh environment and register a relocated copy.

The subsequent 16K runtime tuning and additional accepted OpenCode/MCP smoke task are recorded in [PERFORMANCE.md](PERFORMANCE.md). The ten-task pilot above was not rerun after changing the cache/load settings.
